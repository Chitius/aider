# flake8: noqa: E501


# COMMIT
commit_system = {
    "en": """You are an expert software engineer.
Review the provided context and diffs which are about to be committed to a git repo.
Generate a *SHORT* 1 line, 1 sentence commit message that describes the purpose of the changes.
The commit message MUST be in the past tense.
It must describe the changes *which have been made* in the diffs!
Reply with JUST the commit message, without quotes, comments, questions, etc!
""",
    "zh": """您是一位资深软件工程师. 
请审查即将提交到 git 仓库的上下文和 diffs.
生成一个 *简短* 的、描述变更目的的一句话的、一行的提交信息. 
提交信息尽量使用过去时态. 
它必须描述在 diffs 中 *已经做出* 的变更!
回复时只提供提交信息, 不要添加引号、注释、问题等!
"""
}

# COMMANDS
undo_command_reply = {
    "en": (
        "I did `git reset --hard HEAD~1` to discard the last edits. Please wait for further"
        " instructions before attempting that change again. Feel free to ask relevant questions about"
        " why the changes were reverted."
    ),
    "zh": (
        """我执行了 `git reset --hard HEAD~1` 以撤销最后一次编辑. 请在再次尝试该更改前等待进一步指示. 你随时可以提出关于为何更改被撤销的相关问题. """
    )
}

added_files = {
    "en": """I added these files to the chat: {fnames}.

If you need to propose edits to other existing files not already added to the chat, you *MUST* tell the me their full path names and ask me to *add the files to the chat*. End your reply and wait for my approval. You can keep asking if you then decide you need to edit more files.""",
    "zh": """我向对话中添加了这些文件：{fnames}. 

如果您需要对其他未加入对话的文件进行编辑, 您 *必须* 告诉我它们的完整路径名称, 并要求我 *将文件添加到对话中*. 结束您的回复并等待我的批准. 如果您需要编辑更多文件, 您可以继续询问.
"""
}


run_output = {
    "en": """I ran this command:

{command}

And got this output:

{output}
""",
    "zh": """我运行了这个命令:

{command}

得到了以下输出:

{output}
"""
}

# CHAT HISTORY
summarize = {
    "en": """*Briefly* summarize this partial conversation about programming.
Include less detail about older parts and more detail about the most recent messages.
Start a new paragraph every time the topic changes!

This is only part of a longer conversation so *DO NOT* conclude the summary with language like "Finally, ...". Because the conversation continues after the summary.
The summary *MUST* include the function names, libraries, packages that are being discussed.
The summary *MUST* include the filenames that are being referenced by the assistant inside the ```...``` fenced code blocks!
The summaries *MUST NOT* include ```...``` fenced code blocks!

Phrase the summary with the USER in first person, telling the ASSISTANT about the conversation.
Write *as* the user.
The user should refer to the assistant as *you*.
Start the summary with "I asked you...".
""",
    "zh": """*简要* 对关于编程的这部分对话进行总结. 
对于较早的部分, 请少些细节; 对于最近的消息, 请多些细节.
每当编程的主题改变时, 开启一个新的段落!

这只是更长对话的一部分, 因此 *不要* 用 "最后, ..." 之类的语言来结束总结. 因为对话在总结之后还会继续. 
摘要 *必须* 包括正在讨论的函数, 库, 包的名称. 
摘要 *必须* 包括 assistant 在 ```...``` 围栏代码块中引用的文件名!
摘要 *不能* 包含用 ```...``` 围起来的代码块!

以 User 的第一人称向 assistant 叙述总结. 
以 User 的身份写作. 
User 应该将 assistant 称为 *你*.
从 "我要求你 ..." 开始总结. 
"""
}

summary_prefix = {
    "en": "I spoke to you previously about a number of things.\n",
    "zh": "我之前与你谈过很多事. \n"
}
