#!/usr/bin/env python

import hashlib
import json
import mimetypes
import os
import platform
import re
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime
from json.decoder import JSONDecodeError
from pathlib import Path

import git
from rich.console import Console, Text
from rich.markdown import Markdown

from aider import __version__, models, prompts, urls, utils
from aider.commands import Commands
from aider.history import ChatSummary
from aider.io import InputOutput
from aider.linter import Linter
from aider.llm import litellm
from aider.mdstream import MarkdownStream
from aider.repo import GitRepo
from aider.repomap import RepoMap
from aider.sendchat import send_with_retries
from aider.utils import format_content, format_messages, is_image_file

from typing import *
from ..dump import dump  # noqa: F401


class MissingAPIKeyError(ValueError):
    pass


class FinishReasonLength(Exception):
    pass


def wrap_fence(name):
    return f"<{name}>", f"</{name}>"


class Coder:
    abs_fnames = None
    repo = None
    last_aider_commit_hash = None
    aider_commit_hashes = set()
    aider_edited_files = None
    last_asked_for_commit_time = 0
    repo_map = None
    functions = None
    total_cost = 0.0
    num_exhausted_context_windows = 0
    num_malformed_responses = 0
    last_keyboard_interrupt = None
    num_reflections = 0
    max_reflections = 3
    edit_format = None
    yield_stream = False
    temperature = 0
    auto_lint = True
    auto_test = False
    test_cmd = None
    lint_outcome = None
    test_outcome = None
    multi_response_content = ""

    @classmethod
    def create(
        self,
        main_model: models.Model = None,
        edit_format=None,
        io=None,
        from_coder=None,
        summarize_from_coder=True,
        **kwargs,
    ):
        """ 创建并返回 Agent """
        from . import (
            EditBlockCoder,
            EditBlockFencedCoder,
            HelpCoder,
            UnifiedDiffCoder,
            WholeFileCoder,
        )

        if not main_model:
            if from_coder:
                main_model = from_coder.main_model
            else:
                main_model = models.Model(models.DEFAULT_MODEL_NAME)

        if edit_format is None:
            if from_coder:
                edit_format = from_coder.edit_format
            else:
                edit_format = main_model.edit_format

        if not io and from_coder:
            io = from_coder.io

        if from_coder:
            use_kwargs = dict(from_coder.original_kwargs)  # copy orig kwargs

            # If the edit format changes, we can't leave old ASSISTANT
            # messages in the chat history. The old edit format will
            # confused the new LLM. It may try and imitate it, disobeying
            # the system prompt.
            done_messages = from_coder.done_messages
            if edit_format != from_coder.edit_format and done_messages and summarize_from_coder:
                done_messages = from_coder.summarizer.summarize_all(done_messages)

            # Bring along context from the old Coder
            update = dict(
                fnames=from_coder.get_inchat_relative_files(),
                done_messages=done_messages,
                cur_messages=from_coder.cur_messages,
                aider_commit_hashes=from_coder.aider_commit_hashes,
            )

            use_kwargs.update(update)  # override to complete the switch
            use_kwargs.update(kwargs)  # override passed kwargs

            kwargs = use_kwargs

        if edit_format == "diff":
            res = EditBlockCoder(main_model, io, **kwargs)
        elif edit_format == "diff-fenced":
            res = EditBlockFencedCoder(main_model, io, **kwargs)
        elif edit_format == "whole":
            res = WholeFileCoder(main_model, io, **kwargs)
        elif edit_format == "udiff":
            res = UnifiedDiffCoder(main_model, io, **kwargs)
        elif edit_format == "help":
            res = HelpCoder(main_model, io, **kwargs)
        else:
            raise ValueError(f"Unknown edit format {edit_format}")

        res.original_kwargs = dict(kwargs)

        return res

    def __init__(
        self,
        main_model,
        io,
        fnames=None,
        git_dname=None,
        pretty=True,
        show_diffs=False,
        auto_commits=True,
        dirty_commits=True,
        dry_run=False,
        map_tokens=1024,
        verbose=False,
        assistant_output_color="light_blue",
        code_theme="default",
        stream=True,
        use_git=True,
        voice_language=None,
        aider_ignore_file=None,
        cur_messages=None,
        done_messages=None,
        max_chat_history_tokens=None,
        restore_chat_history=False,
        auto_lint=True,
        auto_test=False,
        lint_cmds=None,
        test_cmd=None,
        attribute_author=True,
        attribute_committer=True,
        attribute_commit_message=False,
        aider_commit_hashes=None,
        map_mul_no_files=8,
        verify_ssl=True,
        language="en",
    ):
        """
        部分参数解释: 

        - main_model: 一个 models.Model 对象，表示 LLM 模型的配置 (不含 API 信息), 
                      例如 GPT-4o 默认使用什么编辑模式, 是否能接收图像，默认设置多少最大 token, 之类的.

            P.S. 要和 llm 进行实际的交互，其实是通过在 aider.sendchat 里调用 litellm.completion 函数来实现的，并没有一个专门的 LLM Client 对象.

        - io: aider.io.InputOutput 对象，用于输入输出. 和用户的所有交互都要过这个层.
        """
        if not fnames:
            fnames = []

        if io is None:
            io = InputOutput()

        if aider_commit_hashes:
            self.aider_commit_hashes = aider_commit_hashes
        else:
            self.aider_commit_hashes = set()

        self.chat_completion_call_hashes = []
        self.chat_completion_response_hashes = []
        self.need_commit_before_edits = set()

        self.verbose = verbose
        self.abs_fnames = set()

        if cur_messages:
            self.cur_messages = cur_messages
        else:
            self.cur_messages = []

        if done_messages:
            self.done_messages = done_messages
        else:
            self.done_messages = []

        self.io = io
        self.stream = stream

        if not auto_commits:
            dirty_commits = False

        self.auto_commits = auto_commits
        self.dirty_commits = dirty_commits
        self.assistant_output_color = assistant_output_color
        self.code_theme = code_theme

        self.dry_run = dry_run
        self.pretty = pretty

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=False, no_color=True)

        self.main_model = main_model

        self.show_diffs = show_diffs

        if language.lower() in ['zh', 'en']:
            self.language = language
        else:
            self.language = 'en'

        self.commands = Commands(self.io, self, language = self.language, voice_language = voice_language, verify_ssl=verify_ssl)
        
        if use_git:
            try:
                self.repo = GitRepo(
                    self.io,
                    fnames,
                    git_dname,
                    aider_ignore_file,
                    models=main_model.commit_message_models(),
                    language=self.language,
                    attribute_author=attribute_author,
                    attribute_committer=attribute_committer,
                    attribute_commit_message=attribute_commit_message,
                )
                self.root = self.repo.root
            except FileNotFoundError:
                self.repo = None

        for fname in fnames:
            fname = Path(fname)
            if not fname.exists():
                self.io.tool_output(f"Creating empty file {fname}")
                fname.parent.mkdir(parents=True, exist_ok=True)
                fname.touch()

            if not fname.is_file():
                raise ValueError(f"{fname} is not a file")

            fname = str(fname.resolve())

            if self.repo and self.repo.ignored_file(fname):
                self.io.tool_error(f"Skipping {fname} that matches aiderignore spec.")
                continue

            self.abs_fnames.add(fname)
            self.check_added_files()   # 进行文件 message 的添加, 且检查 token 用量

        if not self.repo:
            self.find_common_root()

        max_inp_tokens = self.main_model.info.get("max_input_tokens") or 0
        if main_model.use_repo_map and self.repo and self.gpt_prompts.repo_content_prefix[self.language]:
            self.repo_map = RepoMap(
                map_tokens,
                self.root,
                self.main_model,
                io,
                self.gpt_prompts.repo_content_prefix[self.language],
                self.verbose,
                max_inp_tokens,
                map_mul_no_files=map_mul_no_files,
            )

        if max_chat_history_tokens is None:
            max_chat_history_tokens = self.main_model.max_chat_history_tokens
        self.summarizer = ChatSummary(
            self.main_model.weak_model,
            max_chat_history_tokens,
            self.language
        )

        self.summarizer_thread = None
        self.summarized_done_messages = []

        if not self.done_messages and restore_chat_history:
            history_md = self.io.read_text(self.io.chat_history_file)
            if history_md:
                self.done_messages = utils.split_chat_history_markdown(history_md)
                self.summarize_start()

        # Linting and testing
        self.linter = Linter(root=self.root, encoding=io.encoding)
        self.auto_lint = auto_lint
        self.setup_lint_cmds(lint_cmds)

        self.auto_test = auto_test
        self.test_cmd = test_cmd

        # validate the functions jsonschema
        if self.functions:
            print("self.functions: ", self.functions)
            from jsonschema import Draft7Validator

            for function in self.functions:
                Draft7Validator.check_schema(function)

            if self.verbose:
                self.io.tool_output("JSON Schema:")
                self.io.tool_output(json.dumps(self.functions, indent=4))

    def clone(self, **kwargs):
        return Coder.create(from_coder=self, **kwargs)

    def get_announcements(self):
        lines = []
        lines.append(f"Aider v{__version__}")

        # Model
        main_model = self.main_model
        weak_model = main_model.weak_model
        prefix = "Model:"
        output = f" {main_model.name} with {self.edit_format} edit format"
        if weak_model is not main_model:
            prefix = "Models:"
            output += f", weak model {weak_model.name}"
        lines.append(prefix + output)

        # Repo
        if self.repo:
            rel_repo_dir = self.repo.get_rel_repo_dir()
            num_files = len(self.repo.get_tracked_files())
            lines.append(f"Git repo: {rel_repo_dir} with {num_files:,} files")
            if num_files > 1000:
                lines.append(
                    "Warning: For large repos, consider using an .aiderignore file to ignore"
                    " irrelevant files/dirs."
                )
        else:
            lines.append("Git repo: none")

        # Repo-map
        if self.repo_map:
            map_tokens = self.repo_map.max_map_tokens
            if map_tokens > 0:
                lines.append(f"Repo-map: using {map_tokens} tokens")
                max_map_tokens = 2048
                if map_tokens > max_map_tokens:
                    lines.append(
                        f"Warning: map-tokens > {max_map_tokens} is not recommended as too much"
                        " irrelevant code can confuse GPT."
                    )
            else:
                lines.append("Repo-map: disabled because map_tokens == 0")
        else:
            lines.append("Repo-map: disabled")

        # Files
        for fname in self.get_inchat_relative_files():
            lines.append(f"Added {fname} to the chat.")

        if self.done_messages:
            lines.append("Restored previous conversation history.")

        return lines

    def setup_lint_cmds(self, lint_cmds):
        if not lint_cmds:
            return
        for lang, cmd in lint_cmds.items():
            self.linter.set_linter(lang, cmd)

    def show_announcements(self):
        for line in self.get_announcements():
            self.io.tool_output(line)

    def find_common_root(self):
        if len(self.abs_fnames) == 1:
            self.root = os.path.dirname(list(self.abs_fnames)[0])
        elif self.abs_fnames:
            self.root = os.path.commonpath(list(self.abs_fnames))
        else:
            self.root = os.getcwd()

        self.root = utils.safe_abs_path(self.root)

    def add_rel_fname(self, rel_fname):
        self.abs_fnames.add(self.abs_root_path(rel_fname))
        self.check_added_files()

    def drop_rel_fname(self, fname):
        abs_fname = self.abs_root_path(fname)
        if abs_fname in self.abs_fnames:
            self.abs_fnames.remove(abs_fname)
            return True

    def abs_root_path(self, path):
        res = Path(self.root) / path
        return utils.safe_abs_path(res)

    fences = [
        ("``" + "`", "``" + "`"),
        wrap_fence("source"),
        wrap_fence("code"),
        wrap_fence("pre"),
        wrap_fence("codeblock"),
        wrap_fence("sourcecode"),
    ]
    fence = fences[0]

    def show_pretty(self):
        if not self.pretty:
            return False

        # only show pretty output if fences are the normal triple-backtick
        if self.fence != self.fences[0]:
            return False

        return True

    def get_abs_fnames_content(self):
        for fname in list(self.abs_fnames):
            content = self.io.read_text(fname)

            if content is None:
                relative_fname = self.get_rel_fname(fname)
                self.io.tool_error(f"Dropping {relative_fname} from the chat.")
                self.abs_fnames.remove(fname)
            else:
                yield fname, content

    def choose_fence(self):
        """ 从 self.fences 中选择一个合适的 fencing 策略，然后更新 self.fence """
        all_content = ""
        for _fname, content in self.get_abs_fnames_content():
            all_content += content + "\n"

        good = False
        for fence_open, fence_close in self.fences:
            if fence_open in all_content or fence_close in all_content:
                continue
            good = True
            break

        if good:
            self.fence = (fence_open, fence_close)
        else:
            self.fence = self.fences[0]
            self.io.tool_error(
                "Unable to find a fencing strategy! Falling back to:"
                f" {self.fence[0]}...{self.fence[1]}"
            )

        return

    def get_files_content(self, fnames=None):
        if not fnames:
            fnames = self.abs_fnames

        prompt = ""
        for fname, content in self.get_abs_fnames_content():
            if not is_image_file(fname):
                relative_fname = self.get_rel_fname(fname)
                prompt += "\n"
                prompt += relative_fname
                prompt += f"\n{self.fence[0]}\n"

                prompt += content

                # lines = content.splitlines(keepends=True)
                # lines = [f"{i+1:03}:{line}" for i, line in enumerate(lines)]
                # prompt += "".join(lines)

                prompt += f"{self.fence[1]}\n"

        return prompt

    def get_cur_message_text(self):
        text = ""
        for msg in self.cur_messages:
            text += msg["content"] + "\n"
        return text

    def get_ident_mentions(self, text):
        # Split the string on any character that is not alphanumeric
        # \W+ matches one or more non-word characters (equivalent to [^a-zA-Z0-9_]+)
        words = set(re.split(r"\W+", text))
        return words

    def get_ident_filename_matches(self, idents):
        all_fnames = defaultdict(set)
        for fname in self.get_all_relative_files():
            base = Path(fname).with_suffix("").name.lower()
            if len(base) >= 5:
                all_fnames[base].add(fname)

        matches = set()
        for ident in idents:
            if len(ident) < 5:
                continue
            matches.update(all_fnames[ident.lower()])

        return matches

    def get_repo_map(self):
        if not self.repo_map:
            return

        cur_msg_text = self.get_cur_message_text()
        mentioned_fnames = self.get_file_mentions(cur_msg_text)
        mentioned_idents = self.get_ident_mentions(cur_msg_text)

        mentioned_fnames.update(self.get_ident_filename_matches(mentioned_idents))

        other_files = set(self.get_all_abs_files()) - set(self.abs_fnames)
        repo_content = self.repo_map.get_repo_map(
            self.abs_fnames,
            other_files,
            mentioned_fnames=mentioned_fnames,
            mentioned_idents=mentioned_idents,
        )

        # fall back to global repo map if files in chat are disjoint from rest of repo
        if not repo_content:
            repo_content = self.repo_map.get_repo_map(
                set(),
                set(self.get_all_abs_files()),
                mentioned_fnames=mentioned_fnames,
                mentioned_idents=mentioned_idents,
            )

        # fall back to completely unhinted repo
        if not repo_content:
            repo_content = self.repo_map.get_repo_map(
                set(),
                set(self.get_all_abs_files()),
            )

        return repo_content

    def get_files_messages(self):
        files_messages = []

        repo_content = self.get_repo_map()
        if repo_content:
            repo_content_reply = {
                "en": "Ok, I won't try and edit those files without asking first.",
                "zh": "好的, 我会在尝试编辑这些文件之前先询问用户."
            }
            files_messages += [
                dict(role="user", content=repo_content),
                dict(
                    role="assistant",
                    content=repo_content_reply[self.language],
                ),
            ]

        if self.abs_fnames:
            files_content = self.gpt_prompts.files_content_prefix[self.language]
            files_content += self.get_files_content()
            files_reply = {
                "en": "Ok, any changes I propose will be to those files.",
                "zh": "好的, 我所提出的任何更改都会被应用于这些文件."
            }[self.language]
        elif repo_content:
            files_content = self.gpt_prompts.files_no_full_files_with_repo_map[self.language]
            files_reply = self.gpt_prompts.files_no_full_files_with_repo_map_reply[self.language]
        else:
            files_content = self.gpt_prompts.files_no_full_files[self.language]
            files_reply = "Ok."

        if files_content:
            files_messages += [
                dict(role="user", content=files_content),
                dict(role="assistant", content=files_reply),
            ]

        images_message = self.get_images_message()
        if images_message is not None:
            files_messages += [
                images_message,
                dict(role="assistant", content="Ok."),
            ]

        return files_messages

    def get_images_message(self):
        if not self.main_model.accepts_images:
            return None

        image_messages = []
        for fname, content in self.get_abs_fnames_content():
            if is_image_file(fname):
                mime_type, _ = mimetypes.guess_type(fname)
                if mime_type and mime_type.startswith("image/"):
                    image_url = f"data:{mime_type};base64,{content}"
                    image_messages.append(
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}
                    )

        if not image_messages:
            return None

        return {"role": "user", "content": image_messages}

    def run_stream(self, user_message):
        self.io.user_input(user_message)
        self.init_before_message()
        yield from self.send_new_user_message(user_message)

    def init_before_message(self):
        self.reflected_message = None
        self.num_reflections = 0
        self.lint_outcome = None
        self.test_outcome = None
        self.edit_outcome = None

    def run(self, with_message = None):
        """
        Agent 运行的入口.

        依次做几件事:

        1. 调用 self.run_loop() 来获取用户输入（但这个函数里并没有 loop 结构）. 并对用户的输入进行解析. 
           如果用户输入的是 
                /add, /run, /clear, /tokens, /undo, /diff, /drop, /test, /exit,
                /quit, /ls, /help, /voice, /model, /models, /web, /lint, /commit
           这几个指令之一, 则还要执行指令.
           如果用户没有输入指令而是输入了请求，但是包含以下两种特殊内容，也会被立即处理:
                
            - 提及到某个文件名. 则会立刻征求是否添加到对话, 并进行添加操作.

            - 提及到某个网址, 则会视作用户的请求中夹杂了一个 /web 指令, 并会将网址上的内容进行爬取并添加到记忆中.

        2. 如果用户输入的不是指令或者指令产生了需要 LLM 响应的内容 (当做 role = user 的消息), 
           则调用 self.send_new_user_message(), 来获取并处理模型输出，
           包括文件编辑和自动提交. (此函数调用一次过程中，只涉及一轮和 LLM 的沟通).

        3. 如果反思信息不为空, 说明遇到错误或编辑尚未结束, 则回到第 2 步.

        4. 如果反思信息为空或达到最大反思轮数, 则回到第 1 步, 等待用户的新输入。

        # TODO: 多轮对话中，旧的对话信息是在哪里被压缩的？
        
        @param with_message: 可选参数，如果提供，将直接处理该消息而不等待用户输入。
        """
        while True:
            self.init_before_message()  # 初始化一些变量为 None 或 0.

            try:
                if with_message:
                    # 如果 with_message 不为空，则直接把 with_message 当做用户发送的第一条消息
                    new_user_message = with_message
                    self.io.user_input(with_message)  
                else:
                    # 否则获取用户输入的消息, 其中还涉及处理用户的指令, 例如 /add
                    new_user_message = self.run_loop()  

                while new_user_message:
                    self.reflected_message = None

                    # 【核心函数】
                    # 每次接收到用户的请求后，都调用一次本函数来获取并处理模型输出，包括文件编辑和自动提交. 此函数调用一次过程中，也只涉及一轮和 LLM 的沟通.
                    list(self.send_new_user_message(new_user_message)) 

                    new_user_message = None
                    if self.reflected_message:
                        # 如果反思信息不为 None, 说明 LLM 的编辑任务中遇到了错误或者编辑尚未完成。
                        # 例如遇到文件读写错误，linting 错误, 或者是模型要求添加新文件以供编辑.
                        if self.num_reflections < self.max_reflections:
                            # 只要自我反思轮数没有超过上线，我们就向 LLM 发送一个 
                            # {user: <反思信息>}
                            # 的 message 来要求 LLM 进行进一步的编辑.
                            self.num_reflections += 1
                            new_user_message = self.reflected_message  
                        else:
                            self.io.tool_error(
                                f"Only {self.max_reflections} reflections allowed, stopping."
                            )  

                    # 注意！反思过程不会对 self.self.partial_response_content 和 self.partial_response_function_call 这些中间缓存做任何修改！
                    # TODO: 该研究 partial 和 multi 在这里的区别了.

                if with_message:
                    # 如果调用时自带 message, 说明是单轮对话，需要直接返回响应.
                    return self.partial_response_content  #

            except KeyboardInterrupt:
                self.keyboard_interrupt()  # 捕获键盘中断异常，进行相应处理
            except EOFError:
                return  # 捕获EOFError，结束循环

    def run_loop(self):
        """
        获取用户输入（但这个函数里并没有 loop 结构）. 并对用户的输入进行解析. 
        如果用户输入的是 
            /add, /run, /clear, /tokens, /undo, /diff, /drop, /test, /exit,
            /quit, /ls, /help, /voice, /model, /models, /web, /lint, /commit
        这几个指令之一, 则还要执行指令.
        如果用户没有输入指令而是输入了请求，但是包含以下两种特殊内容，也会被立即处理:
            
        - 提及到某个文件名. 则会立刻征求是否添加到对话, 并进行添加操作.

        - 提及到某个网址, 则会视作用户的请求中夹杂了一个 /web 指令, 并会将网址上的内容进行爬取并添加到记忆中.
        """
        inp = self.io.get_input(self.commands).strip()

        if not inp:
            return

        if self.commands.is_command(inp):
            return self.commands.run(inp)

        # 如果用户的输入中
        self.check_for_file_mentions(inp)
        inp = self.check_for_urls(inp)

        return inp

    def check_for_urls(self, inp):
        url_pattern = re.compile(r"(https?://[^\s/$.?#].[^\s]*[^\s,.])")
        urls = url_pattern.findall(inp)
        for url in urls:
            if self.io.confirm_ask(f"Add {url} to the chat?"):
                inp += "\n\n"
                inp += self.commands.cmd_web(url)

        return inp

    def keyboard_interrupt(self):
        now = time.time()

        thresh = 2  # seconds
        if self.last_keyboard_interrupt and now - self.last_keyboard_interrupt < thresh:
            self.io.tool_error("\n\n^C KeyboardInterrupt")
            sys.exit()

        self.io.tool_error("\n\n^C again to exit")

        self.last_keyboard_interrupt = now

    def summarize_start(self):
        if not self.summarizer.too_big(self.done_messages):
            return

        self.summarize_end()

        if self.verbose:
            self.io.tool_output("Starting to summarize chat history.")

        self.summarizer_thread = threading.Thread(target=self.summarize_worker)
        self.summarizer_thread.start()

    def summarize_worker(self):
        try:
            self.summarized_done_messages = self.summarizer.summarize(self.done_messages)
        except ValueError as err:
            self.io.tool_error(err.args[0])

        if self.verbose:
            self.io.tool_output("Finished summarizing chat history.")

    def summarize_end(self):
        """ 等待 summarize 线程将历史信息的摘要添加到对话中, 摘要会被更新为 self.done_messages 中 
            TODO: 研究这个摘要线程在干嘛 """
        if self.summarizer_thread is None:
            return

        self.summarizer_thread.join()
        self.summarizer_thread = None

        self.done_messages = self.summarized_done_messages
        self.summarized_done_messages = []

    def move_back_cur_messages(self, message):
        """
        将当前轮次的 self.cur_messages 移入历史信息部分, 并开始 summarize. 
        这里的 message 实际上是 self.auto_commit() 所返回的信息, 例如:
        "我已使用 git hash c725447 提交了更改, 提交信息为: Added scoreboard and timer to the snake game."
        """
        self.done_messages += self.cur_messages
        self.summarize_start()

        # TODO check for impact on image messages
        if message:
            # 把 LLM 所做的改动当成是 user 的消息
            self.done_messages += [
                dict(role="user", content=message),
                dict(role="assistant", content="Ok."),
            ]
        self.cur_messages = []

    def fmt_system_prompt(self, prompt):
        lazy_prompt = self.gpt_prompts.lazy_prompt[self.language] if self.main_model.lazy else ""

        platform_text = f"- The user's system: {platform.platform()}\n"
        if os.name == "nt":
            var = "COMSPEC"
        else:
            var = "SHELL"

        val = os.getenv(var)
        platform_text += f"- The user's shell: {var}={val}\n"
        dt = datetime.now().isoformat()
        platform_text += f"- The current date/time: {dt}"

        prompt = prompt.format(
            fence=self.fence,
            lazy_prompt=lazy_prompt,
            platform=platform_text,
        )
        return prompt

    def format_messages(self):
        """
        返回要传递给 LLM 的完整 messages.

        messages 依次由如下的若干部分组成:

        1. 系统 message: 说明 agent 的人设、基本思维方式, 以及告诫 agent 你很勤奋.
        2. Few-shot 示例: 通常情况下, 由若干条 user 和 assistant 交替的预设对话历史组成. 但也可以设置为集成到上一条 system message 中.
        3. 示例终止 message: 即 {user: "我切换到了一个新的代码仓. 请不要再考虑上述的文件", assistant: "OK."}
        4. 历史对话摘要 message: 由摘要线程单独提供, 由 weak_model 提供支持. 具体内容和对象有待研究 TODO.
        5. 仓库结构信息 message (可选): 即 repo-map 信息.
        6. 文件信息 message: 已经被人为添加到对话中了的所有文件. 无论有多少文件，都放在一条信息中.
                            如果没有任何文件, 也会添加一段 "user: 我尚未添加任何文件" 之类的话.
        7. 用户当前轮次的请求信息 messages: 这可能会一次性添加多个 message, 为什么会多个，还有待研究 TODO.
        8. 返回格式提示 message: 提示 Agent 应该以什么格式输出接下来的内容, 对后面能否正确解析对文件的改动非常重要。
        """
        self.choose_fence()     # 更新 self.fence

        # 主要的系统 prompt. fmt_system_prompt 的过程是为了将 prompt 中的花括号块格式化.
        main_sys: str = self.fmt_system_prompt(self.gpt_prompts.main_system[self.language])
        
        # prompt 的 few-shot 部分
        example_messages = []

        if self.main_model.examples_as_sys_msg:
            # 如果在 model 的配置中, 要求了将 示例 prompt 的内容集成地塞在首条 system message 之中
            if self.gpt_prompts.example_messages[self.language]:
                main_sys += "\n# Example conversations:\n\n"
            for msg in self.gpt_prompts.example_messages[self.language]:
                role = msg["role"]
                content = self.fmt_system_prompt(msg["content"])
                main_sys += f"## {role.upper()}: {content}\n\n"
            main_sys = main_sys.strip()
        else:
            # 否则, 将每条 示例 prompt 都作为单独的 message.
            for msg in self.gpt_prompts.example_messages[self.language]:
                example_messages.append(
                    dict(
                        role=msg["role"],
                        content=self.fmt_system_prompt(msg["content"]),
                    )
                )

            if self.gpt_prompts.example_messages[self.language]:
                # 在示例 prompt 的最后添加一条额外 message, 来提示模型现在处在新的代码仓, 从而让模型不要把示例中的代码当前现在真实环境下的代码.
                example_messages_hint = {
                    "en": (
                        "I switched to a new code base. Please don't consider the above files"
                        " or try to edit them any longer."
                    ),
                    "zh": (
                        "我切换到了一个新的代码仓. 请不要再考虑上述的文件, 也不要再尝试编辑它们."
                    )
                }[self.language]
                example_messages += [
                    dict(
                        role="user",
                        content=example_messages_hint,
                    ),
                    dict(role="assistant", content="Ok."),
                ]

        ### 接下来是构筑 messages 的过程
        # 首先添加首条 system message
        main_sys += "\n" + self.fmt_system_prompt(self.gpt_prompts.system_reminder[self.language])
        messages = [
            dict(role="system", content=main_sys),
        ]

        # 接着添加示例 message
        messages += example_messages

        # 添加对 之前对话历史 的摘要 message
        self.summarize_end()
        messages += self.done_messages

        # 添加最新的文件的信息到 messages 中. 主要包含 2 部分
        # 第一部分（似乎默认模式不开启）是 user: repo map info+ assistant： "好的, ...."
        # 第二部分是 user: 我已经 XXX 文件添加到对话 <文件内容> + assistant: "好的, 我所提出的任何更改都会被应用于这些文件."
        messages += self.get_files_messages()

        reminder_message = [
            dict(role="system", content=self.fmt_system_prompt(self.gpt_prompts.system_reminder[self.language])),
        ]

        # 计算 token, 我不用担心
        # TODO review impact of token count on image messages
        messages_tokens = self.main_model.token_count(messages)
        reminder_tokens = self.main_model.token_count(reminder_message)
        cur_tokens = self.main_model.token_count(self.cur_messages)

        if None not in (messages_tokens, reminder_tokens, cur_tokens):
            total_tokens = messages_tokens + reminder_tokens + cur_tokens
        else:
            # add the reminder anyway
            total_tokens = 0

        # 添加当前轮次的所有未过期的用户请求信息
        messages += self.cur_messages

        # 最新的用户请求信息
        final = messages[-1]

        max_input_tokens = self.main_model.info.get("max_input_tokens")

        # Add the reminder prompt if we still have room to include it.
        # 添加 返回格式说明 prompt.
        if max_input_tokens is None or total_tokens < max_input_tokens:
            if self.main_model.reminder_as_sys_msg:
                # 如果模型要求将返回格式说明 prompt 集成到 system message 中, 则直接添加到 messages 中
                messages += reminder_message
            elif final["role"] == "user":
                # 否则, 把这段提示当成是 user 在提示模型.
                new_content = (
                    final["content"]
                    + "\n\n"
                    + self.fmt_system_prompt(self.gpt_prompts.system_reminder[self.language])
                )
                messages[-1] = dict(role=final["role"], content=new_content)

        return messages

    def send_new_user_message(self, inp):
        """
        【核心函数】每次接收到用户的请求后，都调用一次本函数来获取并处理模型输出，包括文件编辑和自动提交. 此函数调用一次过程中，也只涉及一轮和 LLM 的沟通.

        做几件事:

        1. 格式化出待发送给 llm 的 messages.

        2. 向 llm 发送 messages, 拿到响应并将响应输出到用户端, 并做好日志, 核心函数是 self.send(). 响应会被更新到 self.partial_response_content 和 self.partial_response_function_call 中.

        3. 从 LLM 响应中提取代码块编辑信息, 并将它们应用于本地代码 (但不进行 git commit), 并在控制台告知用户。

        4. (如果设置了 auto_lint) 做 linting 检查，如果遇到 lint 错误也直接返回. 错误信息更新到反思信息中.

        5. (如果设置了自测脚本路径) 运行自测. 错误信息更新到反思信息中。自测不通过也直接返回。
        
        6. 以上无错误，则进行 git commit.

        7. 检查模型在输出内容中是否提及了要添加新文件，如果要的话, 就向用户征求许可并添加, 并更新反思信息。
        
        :param inp: 用户输入的消息内容。
        """
        # 初始化编辑过的文件标记为None
        self.aider_edited_files = None

        # 将用户消息添加到当前消息列表中
        self.cur_messages += [
            dict(role="user", content=inp),
        ]

        #【重要※】格式化消息, 内部逻辑复杂. 最终返回要传递给 LLM 的完整 messages.
        messages = self.format_messages()

        # 如果在 verbose 模式下，显示消息
        if self.verbose:
            utils.show_messages(messages, functions=self.functions)

        # 初始化多响应内容
        self.multi_response_content = ""

        # 根据配置初始化 Markdown 流
        if self.show_pretty() and self.stream:
            use_color_hex = "#0088ff"
            if self.assistant_output_color == "light_blue":
                use_color_hex = "#0088ff"
            mdargs = dict(style=use_color_hex, code_theme=self.code_theme)
            self.mdstream = MarkdownStream(mdargs=mdargs)
        else:
            self.mdstream = None

        # 初始化中断和超出上下文窗口标记
        exhausted, interrupted = False, False
        try:
            # 持续发送消息直到成功
            while True:
                try:
                    #【重要※】借助 litellm 接口向名称为 model 的 LLM (需要预注册过) 发送 messages 拿到响应并进行输出以及 log. (这个 functions 我怀疑只是留了接口但根本没用)
                    # 如果是流式的话这个 yeild from 就会每次 yield 一段裸 text 信息。
                    yield from self.send(messages, functions=self.functions)
                    break
                except KeyboardInterrupt:
                    # 捕获键盘中断，标记中断状态
                    interrupted = True
                    break
                except litellm.ContextWindowExceededError:
                    # 上下文窗口超出错误，标记超出状态
                    # The input is overflowing the context window!
                    exhausted = True
                    break
                except litellm.exceptions.BadRequestError as br_err:
                    # 请求错误，记录并返回
                    self.io.tool_error(f"BadRequestError: {br_err}")
                    return
                except FinishReasonLength:
                    # 输出长度超出限制，处理多响应内容
                    if not self.main_model.can_prefill:
                        exhausted = True
                        break
                    self.multi_response_content = self.get_multi_response_content()
                    if messages[-1]["role"] == "assistant":
                        messages[-1]["content"] = self.multi_response_content
                    else:
                        messages.append(dict(role="assistant", content=self.multi_response_content))
                except Exception as err:
                    # 捕获其他异常，记录并返回
                    self.io.tool_error(f"Unexpected error: {err}")
                    traceback.print_exc()
                    return

        finally:
            # 最终处理，根据配置释放Markdown流
            if self.mdstream:
                self.live_incremental_response(True)
                self.mdstream = None
            
            self.partial_response_content = self.get_multi_response_content(True)
            self.multi_response_content = ""

        # 如果超出上下文窗口，显示错误并计数
        if exhausted:
            self.show_exhausted_error()
            self.num_exhausted_context_windows += 1
            return
        
        # 如果是调用函数, 把 content 设置为工具调用信息
        # 否则设置为之前的 multi_response_content 内容 (但我还没懂这个 multi_response_content 是啥)
        if self.partial_response_function_call:
            args = self.parse_partial_args()
            if args:
                content = args["explanation"]
            else:
                content = ""
        elif self.partial_response_content:
            content = self.partial_response_content
        else:
            content = ""

        # 输出工具消息
        # TODO: 这他妈是空参数的，岂不是啥都不会干
        self.io.tool_output()

        # 如果发生中断，添加中断消息, 然后直接返回
        if interrupted:
            content += "\n^C KeyboardInterrupt"
            self.cur_messages += [dict(role="assistant", content=content)]
            return

        # 从 LLM 响应中提取代码块编辑信息, 并将它们应用于本地代码 (但不进行 git commit), 并在控制台告知用户。
        # 返回一个包含了所有发生编辑操作了的文件的文件名组成的集合。
        edited: Set[str] = self.apply_updates()   

        if self.reflected_message:
            # 如果编辑操作中遇到了报错，则先不进行自动提交, 直接返回.
            self.edit_outcome = False
            self.update_cur_messages(set())
            return

        if edited:
            # 如果没有报错且确实发生了编辑操作，则我们预定要进行 commit.
            self.edit_outcome = True

        # 首先先做 linting 检查，如果遇到 lint 错误也直接返回. 错误信息更新到反思信息 self.reflected_message 中 
        if edited and self.auto_lint:
            lint_errors = self.lint_edited(edited)
            self.lint_outcome = not lint_errors
            if lint_errors:
                ok = self.io.confirm_ask("Attempt to fix lint errors?")
                if ok:
                    self.reflected_message = lint_errors
                    self.update_cur_messages(set())
                    return

        # 如果用户设置了自测脚本, 则运行自测. 错误信息更新到反思信息 self.reflected_message 中。自测不通过也直接返回。
        if edited and self.auto_test:
            test_errors = self.commands.cmd_test(self.test_cmd)
            self.test_outcome = not test_errors
            if test_errors:
                ok = self.io.confirm_ask("Attempt to fix test errors?")
                if ok:
                    self.reflected_message = test_errors
                    self.update_cur_messages(set())
                    return

        ### 如果编辑无错误, linting 无错误，自测脚本无错误，则进行 commit .
        self.update_cur_messages(edited)
        if edited:
            self.aider_edited_files = edited
            if self.repo and self.auto_commits and not self.dry_run:
                saved_message = self.auto_commit(edited)
            elif hasattr(self.gpt_prompts, "files_content_gpt_edits_no_repo"):
                saved_message = self.gpt_prompts.files_content_gpt_edits_no_repo[self.language]
            else:
                saved_message = None
            self.move_back_cur_messages(saved_message)

        # 检查模型在输出内容中是否提及了要添加新文件，如果要的话, 就向用户征求许可并添加
        add_rel_files_message = self.check_for_file_mentions(content)
        if add_rel_files_message:
            # 如果存在这样的文件请求, 那么就设置 self.reflected_message 使之不为 None, 表明当前轮次的编辑还没完全结束, 还要迭代.
            if self.reflected_message:
                self.reflected_message += "\n\n" + add_rel_files_message
            else:
                self.reflected_message = add_rel_files_message

    def show_exhausted_error(self):
        output_tokens = 0
        if self.partial_response_content:
            output_tokens = self.main_model.token_count(self.partial_response_content)
        max_output_tokens = self.main_model.info.get("max_output_tokens", 0)

        input_tokens = self.main_model.token_count(self.format_messages())
        max_input_tokens = self.main_model.info.get("max_input_tokens", 0)

        total_tokens = input_tokens + output_tokens

        fudge = 0.7

        out_err = ""
        if output_tokens >= max_output_tokens * fudge:
            out_err = " -- possibly exceeded output limit!"

        inp_err = ""
        if input_tokens >= max_input_tokens * fudge:
            inp_err = " -- possibly exhausted context window!"

        tot_err = ""
        if total_tokens >= max_input_tokens * fudge:
            tot_err = " -- possibly exhausted context window!"

        res = ["", ""]
        res.append(f"Model {self.main_model.name} has hit a token limit!")
        res.append("Token counts below are approximate.")
        res.append("")
        res.append(f"Input tokens: ~{input_tokens:,} of {max_input_tokens:,}{inp_err}")
        res.append(f"Output tokens: ~{output_tokens:,} of {max_output_tokens:,}{out_err}")
        res.append(f"Total tokens: ~{total_tokens:,} of {max_input_tokens:,}{tot_err}")

        if output_tokens >= max_output_tokens:
            res.append("")
            res.append("To reduce output tokens:")
            res.append("- Ask for smaller changes in each request.")
            res.append("- Break your code into smaller source files.")
            if "diff" not in self.main_model.edit_format:
                res.append(
                    "- Use a stronger model like gpt-4o, sonnet or opus that can return diffs."
                )

        if input_tokens >= max_input_tokens or total_tokens >= max_input_tokens:
            res.append("")
            res.append("To reduce input tokens:")
            res.append("- Use /tokens to see token usage.")
            res.append("- Use /drop to remove unneeded files from the chat session.")
            res.append("- Use /clear to clear the chat history.")
            res.append("- Break your code into smaller source files.")

        res.append("")
        res.append(f"For more info: {urls.token_limits}")

        res = "".join([line + "\n" for line in res])
        self.io.tool_error(res)

    def lint_edited(self, fnames):
        res = ""
        for fname in fnames:
            errors = self.linter.lint(self.abs_root_path(fname))
            if errors:
                res += "\n"
                res += errors
                res += "\n"

        if res:
            self.io.tool_error(res)

        return res

    def update_cur_messages(self, edited):
        if self.partial_response_content:
            self.cur_messages += [dict(role="assistant", content=self.partial_response_content)]
        if self.partial_response_function_call:
            self.cur_messages += [
                dict(
                    role="assistant",
                    content=None,
                    function_call=self.partial_response_function_call,
                )
            ]

    def get_file_mentions(self, content: str) -> Set[str]:
        """ 提取 conten 中被提到的文件名 """
        words = set(word for word in content.split())

        # drop sentence punctuation from the end
        words = set(word.rstrip(",.!;:") for word in words)

        # strip away all kinds of quotes
        quotes = "".join(['"', "'", "`"])
        words = set(word.strip(quotes) for word in words)

        addable_rel_fnames = self.get_addable_relative_files()

        mentioned_rel_fnames = set()
        fname_to_rel_fnames = {}
        for rel_fname in addable_rel_fnames:
            normalized_rel_fname = rel_fname.replace("\\", "/")
            normalized_words = set(word.replace("\\", "/") for word in words)
            if normalized_rel_fname in normalized_words:
                mentioned_rel_fnames.add(rel_fname)

            fname = os.path.basename(rel_fname)

            # Don't add basenames that could be plain words like "run" or "make"
            if "/" in fname or "\\" in fname or "." in fname or "_" in fname or "-" in fname:
                if fname not in fname_to_rel_fnames:
                    fname_to_rel_fnames[fname] = []
                fname_to_rel_fnames[fname].append(rel_fname)

        for fname, rel_fnames in fname_to_rel_fnames.items():
            if len(rel_fnames) == 1 and fname in words:
                mentioned_rel_fnames.add(rel_fnames[0])

        return mentioned_rel_fnames

    def check_for_file_mentions(self, content: str) -> str:
        """
        检查模型在输出内容中是否提及了要添加新文件，如果要的话, 就向用户征求许可并添加.
        """
        # 提取 conten 中被提到的文件名.
        mentioned_rel_fnames: Set[str] = self.get_file_mentions(content)

        if not mentioned_rel_fnames:
            return

        for rel_fname in mentioned_rel_fnames:
            self.io.tool_output(rel_fname)

        if not self.io.confirm_ask("Add these files to the chat?"):
            return

        for rel_fname in mentioned_rel_fnames:
            self.add_rel_fname(rel_fname)

        return prompts.added_files.format(fnames=", ".join(mentioned_rel_fnames))

    def send(self, messages, model: str = None, functions = None):
        """
        借助 litellm 接口向名称为 model 的 LLM (需要预注册过) 发送 messages 并拿到响应. (这个 functions 我怀疑只是留了接口但根本没用)
        然后做了以下几件事:
        1. 刷新消息缓存 self.partial_response_content 和 self.partial_response_function_call
        2. 调用 litellm.completion() 来向 LLM 发送信息并得到原始响应
        3. 简单格式化后打印模型输出, 并把 content 和函数调用更新到 self.partial_response_content 和 self.partial_response_function_call 中.
        4. 更新 log 和对话历史（不是很重要）.
        """
        if not model:
            model = self.main_model.name

        self.partial_response_content = ""
        self.partial_response_function_call = dict()

        self.io.log_llm_history("TO LLM", format_messages(messages))

        interrupted = False
        try:
            # completion 类型为 litellm.utils.ModelResponse 或 litellm.utils.CustomStreamWrapper
            hash_object, completion = send_with_retries(
                model, messages, functions, self.stream, self.temperature
            )
            self.chat_completion_call_hashes.append(hash_object.hexdigest())

            # 分成流式和非流式, 打印模型的输出. 流式使用 yield 逐个打印.
            if self.stream:
                yield from self.show_send_output_stream(completion)
            else:
                self.show_send_output(completion)

        except KeyboardInterrupt:
            self.keyboard_interrupt()
            interrupted = True
        finally:
            self.io.log_llm_history(
                "LLM RESPONSE",
                format_content("ASSISTANT", self.partial_response_content),
            )
            
            # 如果接收到请求
            if self.partial_response_content:
                # 把响应内容写入对话历史文件 (不进行 stdout 上的输出)
                self.io.ai_output(self.partial_response_content)
            elif self.partial_response_function_call:
                # TODO: push this into subclasses
                args = self.parse_partial_args()
                if args:
                    self.io.ai_output(json.dumps(args, indent=4))

        if interrupted:
            raise KeyboardInterrupt

    def show_send_output(self, completion):
        """ 对非流式的模型输出 (litellm.utils.ModelResponse), 做简单处理并进行输出.
            之后, 更新 self.partial_response_content = content
        """
        if self.verbose:
            print(completion)

        if not completion.choices:
            self.io.tool_error(str(completion))
            return

        show_func_err = None
        show_content_err = None
        try:
            self.partial_response_function_call = completion.choices[0].message.function_call
        except AttributeError as func_err:
            show_func_err = func_err

        try:
            # 把此次响应的内容赋值给 self.partial_response_content
            self.partial_response_content = completion.choices[0].message.content
        except AttributeError as content_err:
            show_content_err = content_err

        resp_hash = dict(
            function_call=self.partial_response_function_call,
            content=self.partial_response_content,
        )
        resp_hash = hashlib.sha1(json.dumps(resp_hash, sort_keys=True).encode())
        self.chat_completion_response_hashes.append(resp_hash.hexdigest())

        if show_func_err and show_content_err:
            self.io.tool_error(show_func_err)
            self.io.tool_error(show_content_err)
            raise Exception("No data found in LLM response!")

        tokens = None
        # 更新 token 用量. 我不用管.
        if hasattr(completion, "usage") and completion.usage is not None:
            prompt_tokens = completion.usage.prompt_tokens
            completion_tokens = completion.usage.completion_tokens

            tokens = f"{prompt_tokens} prompt tokens, {completion_tokens} completion tokens"
            if self.main_model.info.get("input_cost_per_token"):
                cost = prompt_tokens * self.main_model.info.get("input_cost_per_token")
                if self.main_model.info.get("output_cost_per_token"):
                    cost += completion_tokens * self.main_model.info.get("output_cost_per_token")
                tokens += f", ${cost:.6f} cost"
                self.total_cost += cost

        # 下面这个函数会被子 agent 覆盖定义。wholefile_coder 中,
        # 此函数相当于直接调用 self.get_edits(mode="diff"), 做的事情是将 self.get_multi_response_content()
        # 中的代码编辑操作进行解析, 然后返回若干个 (文件名, 代码块的来源, 代码块内容的 diff 信息) 组成的三元组.
        show_resp = self.render_incremental_response(True)      # 
        if self.show_pretty():
            if self.assistant_output_color == "light_blue":
                use_color_hex = "#0088ff"
            # 以 markdown 形式展示所有代码块
            show_resp = Markdown(
                show_resp, style=use_color_hex, code_theme=self.code_theme
            )
            self.io.console.print(show_resp)
        else:
            show_resp = Text(show_resp or "<no response>")
            self.io.print(show_resp, color = self.assistant_output_color)


        if tokens is not None:
            self.io.tool_output(tokens)

        if (
            hasattr(completion.choices[0], "finish_reason")
            and completion.choices[0].finish_reason == "length"
        ):
            raise FinishReasonLength()

    def show_send_output_stream(self, completion):
        for chunk in completion:
            if len(chunk.choices) == 0:
                continue

            if (
                hasattr(chunk.choices[0], "finish_reason")
                and chunk.choices[0].finish_reason == "length"
            ):
                raise FinishReasonLength()

            try:
                func = chunk.choices[0].delta.function_call
                # dump(func)
                for k, v in func.items():
                    if k in self.partial_response_function_call:
                        self.partial_response_function_call[k] += v
                    else:
                        self.partial_response_function_call[k] = v
            except AttributeError:
                pass

            try:
                text = chunk.choices[0].delta.content
                if text:
                    self.partial_response_content += text
            except AttributeError:
                text = None

            if self.show_pretty():
                self.live_incremental_response(False)
            elif text:
                try:
                    sys.stdout.write(text)
                except UnicodeEncodeError:
                    # Safely encode and decode the text
                    safe_text = text.encode(sys.stdout.encoding, errors='backslashreplace').decode(sys.stdout.encoding)
                    sys.stdout.write(safe_text)
                sys.stdout.flush()
                yield text

    def live_incremental_response(self, final):
        show_resp = self.render_incremental_response(final)
        self.mdstream.update(show_resp, final=final)

    def render_incremental_response(self, final):
        return self.get_multi_response_content()

    def get_multi_response_content(self, final=False):
        cur = self.multi_response_content
        new = self.partial_response_content

        if new.rstrip() != new and not final:
            new = new.rstrip()
        return cur + new

    def get_rel_fname(self, fname):
        return os.path.relpath(fname, self.root)

    def get_inchat_relative_files(self):
        """ 返回已经添加到对话的所有文件的相对路径所组成的列表. """
        files = [self.get_rel_fname(fname) for fname in self.abs_fnames]
        return sorted(set(files))

    def is_file_safe(self, fname):
        try:
            return Path(self.abs_root_path(fname)).is_file()
        except OSError:
            return

    def get_all_relative_files(self):
        if self.repo:
            files = self.repo.get_tracked_files()
        else:
            files = self.get_inchat_relative_files()

        files = [fname for fname in files if self.is_file_safe(fname)]
        return sorted(set(files))

    def get_all_abs_files(self):
        files = self.get_all_relative_files()
        files = [self.abs_root_path(path) for path in files]
        return files

    def get_last_modified(self):
        files = [Path(fn) for fn in self.get_all_abs_files() if Path(fn).exists()]
        if not files:
            return 0
        return max(path.stat().st_mtime for path in files)

    def get_addable_relative_files(self):
        return set(self.get_all_relative_files()) - set(self.get_inchat_relative_files())

    def check_for_dirty_commit(self, path):
        if not self.repo:
            return
        if not self.dirty_commits:
            return
        if not self.repo.is_dirty(path):
            return

        # We need a committed copy of the file in order to /undo, so skip this
        # fullp = Path(self.abs_root_path(path))
        # if not fullp.stat().st_size:
        #     return

        self.io.tool_output(f"Committing {path} before applying edits.")
        self.need_commit_before_edits.add(path)

    def allowed_to_edit(self, path: str) -> bool:
        """ 
        给定一个文件路径, 向用户征求编辑许可.
        此外, 如果发现文件是一个新文件且用户允许添加，则立即进行 git add 操作, 且更新 self.abs_fnames.
        """
        full_path = self.abs_root_path(path)
        if self.repo:
            need_to_add = not self.repo.path_in_repo(path)
        else:
            need_to_add = False

        if full_path in self.abs_fnames:
            self.check_for_dirty_commit(path)
            return True

        if not Path(full_path).exists():
            if not self.io.confirm_ask(f"Allow creation of new file {path}?"):
                self.io.tool_error(f"Skipping edits to {path}")
                return

            if not self.dry_run:
                Path(full_path).parent.mkdir(parents=True, exist_ok=True)
                Path(full_path).touch()

                # Seems unlikely that we needed to create the file, but it was
                # actually already part of the repo.
                # But let's only add if we need to, just to be safe.
                if need_to_add:
                    self.repo.repo.git.add(full_path)

            self.abs_fnames.add(full_path)
            self.check_added_files()
            return True

        if not self.io.confirm_ask(
            f"Allow edits to {path} which was not previously added to chat?"
        ):
            self.io.tool_error(f"Skipping edits to {path}")
            return

        if need_to_add:
            self.repo.repo.git.add(full_path)

        self.abs_fnames.add(full_path)
        self.check_added_files()
        self.check_for_dirty_commit(path)

        return True

    warning_given = False

    def check_added_files(self):
        """ 进行文件 message 的添加, 且检查 token 用量 """
        if self.warning_given:
            return

        warn_number_of_files = 4
        warn_number_of_tokens = 20 * 1024

        num_files = len(self.abs_fnames)
        if num_files < warn_number_of_files:
            return

        tokens = 0
        for fname in self.abs_fnames:
            if is_image_file(fname):
                continue
            content = self.io.read_text(fname)
            tokens += self.main_model.token_count(content)

        if tokens < warn_number_of_tokens:
            return

        self.io.tool_error("Warning: it's best to only add files that need changes to the chat.")
        self.io.tool_error(urls.edit_errors)
        self.warning_given = True

    def prepare_to_edit(self, edits: list[Tuple[str, str, str]]) -> list[Tuple[str, str, str]]:
        """ 对每个文件都向用户征求编辑许可, 最后过滤掉不被许可的编辑请求并返回. 
            此外，还进行了 git add 操作和编辑行为之前的 dirty commit 操作。""" 
        res = []
        seen = dict()

        self.need_commit_before_edits = set()

        for edit in edits:
            path = edit[0]
            if path in seen:
                allowed = seen[path]
            else:
                allowed = self.allowed_to_edit(path)   # 向用户征求编辑许可
                seen[path] = allowed

            if allowed:
                res.append(edit)   # 记录所有被许可了的文件

        self.dirty_commit()        # TODO: 意义不明. 为什么这些文件需要在编辑之前进行 commit ?
        self.need_commit_before_edits = set()  

        return res

    def update_files(self) -> Set[str]:
        """ 从 LLM 响应中提取代码块编辑信息, 并将它们应用于本地代码 (但不进行 git commit). 
            最后返回一个包含了所有发生编辑操作了的文件的文件名组成的集合。 """
        edits = self.get_edits()    # 从 LLM 响应中提取代码块编辑信息并以三元组组成的列表的形式返回. 具体参考 wholefile_coder.py 中的注释。
        edits = self.prepare_to_edit(edits)   # 向用户征求许可, 过滤掉不被许可的编辑
        self.apply_edits(edits)     # 将编辑应用到本地文件. 具体参考 wholefile_coder.py 中的注释。就是很简单的 write_text 调用.
        return set(edit[0] for edit in edits)

    def apply_updates(self) -> Set[str]:
        """ 
        从 LLM 响应中提取代码块编辑信息, 并将它们应用于本地代码 (但不进行 git commit), 并在控制台告知用户。
        最后返回一个包含了所有发生编辑操作了的文件的文件名组成的集合。
        如果上述操作遇到错误，会把报错信息放到反思信息 self.reflected_message 中, 后续会让模型进行迭代.
        """
        try:
            edited = self.update_files()
        except ValueError as err:
            self.num_malformed_responses += 1

            err = err.args[0]

            self.io.tool_error("The LLM did not conform to the edit format.")
            self.io.tool_error(urls.edit_errors)
            self.io.tool_error()
            self.io.tool_error(str(err), strip=False)

            self.reflected_message = str(err)
            return

        except git.exc.GitCommandError as err:
            self.io.tool_error(str(err))
            return
        except Exception as err:
            self.io.tool_error("Exception while updating files:")
            self.io.tool_error(str(err), strip=False)

            traceback.print_exc()

            self.reflected_message = str(err)
            return

        for path in edited:
            if self.dry_run:
                self.io.tool_output(f"Did not apply edit to {path} (--dry-run)")
            else:
                self.io.tool_output(f"Applied edit to {path}")

        return edited

    def parse_partial_args(self):
        # dump(self.partial_response_function_call)

        data = self.partial_response_function_call.get("arguments")
        if not data:
            return

        try:
            return json.loads(data)
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + "]}")
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + "}]}")
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + '"}]}')
        except JSONDecodeError:
            pass

    # commits...

    def get_context_from_history(self, history):
        context = ""
        if history:
            for msg in history:
                context += "\n" + msg["role"].upper() + ": " + msg["content"] + "\n"

        return context

    def auto_commit(self, edited) -> str:
        """
        执行 git commit <comment>, comment 内容也由 LLM 生成. 返回一条待安放在 msgs 中有关这次 commit 的消息.
        """
        context = self.get_context_from_history(self.cur_messages)
        res = self.repo.commit(fnames=edited, context=context, aider_edits=True)
        if res:
            commit_hash, commit_message = res
            self.last_aider_commit_hash = commit_hash
            self.aider_commit_hashes.add(commit_hash)
            self.last_aider_commit_message = commit_message
            if self.show_diffs:
                self.commands.cmd_diff()

            return self.gpt_prompts.files_content_gpt_edits[self.language].format(
                hash=commit_hash,
                message=commit_message,
            )

        self.io.tool_output("No changes made to git tracked files.")
        return self.gpt_prompts.files_content_gpt_no_edits[self.language]

    def dirty_commit(self):
        if not self.need_commit_before_edits:
            return
        if not self.dirty_commits:
            return
        if not self.repo:
            return

        self.repo.commit(fnames=self.need_commit_before_edits)

        # files changed, move cur messages back behind the files messages
        # self.move_back_cur_messages(self.gpt_prompts.files_content_local_edits[self.language])
        return True
