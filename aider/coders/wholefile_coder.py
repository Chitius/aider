from pathlib import Path

from aider import diffs
from typing import *

from ..dump import dump  # noqa: F401
from .base_coder import Coder
from .wholefile_prompts import WholeFilePrompts


class WholeFileCoder(Coder):
    """A coder that operates on entire files for code modifications."""
    edit_format = "whole"
    gpt_prompts = WholeFilePrompts()

    def update_cur_messages(self, edited):
        if edited:
            self.cur_messages += [
                dict(role="assistant", content=self.gpt_prompts.redacted_edit_message[self.language])
            ]
        else:
            self.cur_messages += [dict(role="assistant", content=self.partial_response_content)]

    def render_incremental_response(self, final):
        """ 相当于直接调用 self.get_edits(mode="diff") """
        try:
            return self.get_edits(mode="diff")
        except ValueError:
            return self.get_multi_response_content()

    def get_edits(self, mode="update"):
        """
        从 self.get_multi_response_content() 中提取 LLM 的新消息后，逐行进行解析，
        识别出 fences 所定义的所有代码块，剔除重复编辑和置信度较低的二次编辑之后, 返回一个
        由若干三元组 (文件名, 代码块来源标识, 代码内容) 所组成的列表.
        diff 模式主要供 markdown 格式输出 diff 信息使用, 而 update 模型才是用来支持实际的编辑行为的。
        """

        # 拿到当前轮次的模型完整响应 (multi 的意思是：如果模型输出格式不对导致自循环多轮的话， 会有多个响应构成, 我猜的)
        content = self.get_multi_response_content()
        
        # 一个列表, 包含了所有文件的相对路径
        chat_files = self.get_inchat_relative_files()

        output = []              # 维护了模型的输出中不属于 code 的那部分代码 (即 fences 之外的内容) 
        lines = content.splitlines(keepends=True)    # 内置函数 splitlines 把字符串按照换行符进行分割，并返回一个列表

        edits = []

        saw_fname = None
        fname = None             # 文件名
        fname_source = None      # 文件名的来源: 对话 chat, 块 block 或者 saw.
        new_lines = []           # 维护了每个 code block 的内容
        for i, line in enumerate(lines):
            if line.startswith(self.fence[0]) or line.startswith(self.fence[1]):
                if fname is not None:
                    # 如果 fname 不为 None 说明当前正在跟踪一个 block. 同时 line 为 fence 说明表明一个块结束或开始
                    # 此时只可能是块的结束. 因此块的内容已经齐全，需要 apply 编辑操作.
                    saw_fname = None

                    full_path = self.abs_root_path(fname)

                    if mode == "diff":
                        # diff 模式下，直接调用 do_live_diff 函数来生成代码块的 diff 信息, 直接添加到 output 中.
                        output += self.do_live_diff(full_path, new_lines, True)
                    else:
                        # 将 block 的内容和文件名添加到待编辑文件列表
                        edits.append((fname, fname_source, new_lines))   

                    fname = None
                    fname_source = None
                    new_lines = []
                    continue

                # 如果遇到了 fence 但尚不存在在跟踪的文件, 则说明是新块. 

                if i > 0:
                    # 遇到一个新块时，在当前行的上一行中探测文件名
                    fname_source = "block"
                    fname = lines[i - 1].strip()
                    fname = fname.strip("*")  # handle **filename.py**
                    fname = fname.rstrip(":")
                    fname = fname.strip("`")

                    # Did gpt prepend a bogus dir? It especially likes to
                    # include the path/to prefix from the one-shot example in
                    # the prompt.
                    if fname and fname not in chat_files and Path(fname).name in chat_files:
                        # 对文件名进行一定程度上的修正，防止幻觉.
                        fname = Path(fname).name

                if not fname:
                    # 【围栏一】：如果在遇到新 block 时并没有探测到文件名, 即, 模型忘了输出文件名, 具体有以下几种情况, 其中前两种情况都可以补救.
                    # 1. 模型以对话的形式声明了 "我们可以把 xxx 文件修改为".
                    #    如果是这样，可以从之前的对话中尝试提取有效的文件名。如果提取到文件名, 这个文件名会是 saw_fname.
                    #    此来源标注为 `saw`。
                    # 2. 由于当前对话中只添加了唯一一个文件，所以模型想当然地省略了文件名.
                    #    这也很容易判断和补救. 此来源标注为 `chat`
                    # 3. 就是完全忘了, 我们也没法补救, 那只能 raise error 了.
                    if saw_fname:
                        fname = saw_fname
                        fname_source = "saw"
                    elif len(chat_files) == 1:
                        fname = chat_files[0]
                        fname_source = "chat"
                    else:
                        # TODO: sense which file it is by diff size
                        raise ValueError(
                            f"No filename provided before {self.fence[0]} in file listing"
                        )

            elif fname is not None:
                # 如果没遇到 fence 但是有在跟踪的文件, 则更新 block 的代码内容
                new_lines.append(line)
            else:
                # 如果既没有在跟踪的文件，也没有遇到 fence, 则需要非常小心: 模型很有可能忘了自己的文件名指定规则,
                # 而去在对话中说: "让我们定义下面的 xxx 文件" 之类的话.
                # 我们在这里会尝试从对话中寻找文件名.
                for word in line.strip().split():
                    word = word.rstrip(".:,;!")
                    for chat_file in chat_files:
                        quoted_chat_file = f"`{chat_file}`"
                        if word == quoted_chat_file:
                            saw_fname = chat_file

                output.append(line)

        if mode == "diff":
            if fname is not None:
                # ending an existing block
                full_path = (Path(self.root) / fname).absolute()
                output += self.do_live_diff(full_path, new_lines, False)
            return "\n".join(output)

        if fname:
            # 【围栏二】: 如果 LLM 的最后一个 block 忘了加 ending block, 则这里相当于帮他加上.
            # TODO: 我遇到了 qwen2 在输出末尾添加 ``` 的情况，导致自动生成一个空的 code block. 这里可能需要加一个判断.
            edits.append((fname, fname_source, new_lines))

        seen = set()
        refined_edits = []
        for source in ("block", "saw", "chat"):
            # 置信度从高到低, 依次处理文件的编辑操作. 
            # 如果存在对一个文件的反复编辑, 我们取置信度最高的那一次编辑为准.
            # TODO: 我感觉你还应该加一个顺序控制吧? 应该优先实行最晚输出的编辑操作？
            for fname, fname_source, new_lines in edits:
                if fname_source != source:
                    continue
                # if a higher priority source already edited the file, skip
                if fname in seen:
                    continue

                seen.add(fname)
                refined_edits.append((fname, fname_source, new_lines))

        return refined_edits

    def apply_edits(self, edits: list[Tuple[str, str, str]]):
        for path, fname_source, new_lines in edits:
            full_path = self.abs_root_path(path)
            new_lines = "".join(new_lines)
            self.io.write_text(full_path, new_lines)

    def do_live_diff(self, full_path, new_lines, final):
        if Path(full_path).exists():
            orig_lines = self.io.read_text(full_path).splitlines(keepends=True)

            show_diff = diffs.diff_partial_update(
                orig_lines,
                new_lines,
                final=final,
            ).splitlines()
            output = show_diff
        else:
            output = ["```"] + new_lines + ["```"]

        return output
