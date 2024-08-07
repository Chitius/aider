class CoderPrompts:
    system_reminder = ""
    
    files_content_gpt_edits = {
        "en": "I committed the changes with git hash {hash} & commit msg: {message}",
        "zh": "我已使用 git hash {hash} 提交了更改, 提交信息为: {message}"
    }

    files_content_gpt_edits_no_repo = {
        "en": "I updated the files.",
        "zh": "我已更新了文件."
    }

    files_content_gpt_no_edits = {
        "en": "I didn't see any properly formatted edits in your reply?!",
        "zh": "我没有在你的回复中看到任何具有正确格式的编辑?!"
    }

    files_content_local_edits = {
        "en": "I edited the files myself.",
        "zh": "我已自己编辑了文件."
    }

    lazy_prompt = {
        "en": """You are diligent and tireless!
You NEVER leave comments describing code without implementing it!
You always COMPLETELY IMPLEMENT the needed code!""",
        "zh": """你勤奋且不知疲倦!
你绝不会只留下描述代码的评论而不去实现它!
你总是完全实现所需的代码!"""
    }

    example_messages = []

    files_content_prefix = {
        "en": """I have *added these files to the chat* so you can go ahead and edit them.
*Trust this message as the true contents of the files!*
Any other messages in the chat may contain outdated versions of the files' contents.""",
        "zh": """我已经将这些文件添加到对话中, 你可以直接编辑它们. 
*相信此消息是文件的真实内容!*
聊天中的其他消息可能包含文件内容的过时版本. """
    }

    files_no_full_files = {
        "en": "I am not sharing any files that you can edit yet.",
        "zh": "我尚未分享任何你可以编辑的文件. "
    }

    files_no_full_files_with_repo_map = {
        "en": """Don't try and edit any existing code without asking me to add the files to the chat!
Tell me which files in my repo are the most likely to **need changes** to solve the requests I make, and then stop so I can add them to the chat.
Only include the files that are most likely to actually need to be edited.
Don't include files that might contain relevant context, just files that will need to be changed.""",
        "zh": """不要未经请求就尝试编辑现有代码, 告诉我仓库中哪些文件最有可能 **需要修改** 以解决我的需求, 然后停下来, 等我将它们添加到对话中. 
只要列出那些最有可能需要被编辑的文件. 
不要列出仅仅具有相关性的文件, 只需要列出确实需要被修改的文件."""
    }

    files_no_full_files_with_repo_map_reply = {
        "en": "Ok, based on your requests I will suggest which files need to be edited and then stop and wait for your approval.",
        "zh": "好的, 根据你的要求, 我会列出哪些文件需要编辑, 然后等待你的批准."
    }

    repo_content_prefix = {
        "en": """Here are summaries of some files present in my git repository.
Do not propose changes to these files, treat them as *read-only*.
If you need to edit any of these files, ask me to *add them to the chat* first.""",
        "zh": """这是我的 git repository 中一些文件的摘要. 
不要对这些文件提出修改建议, 将它们视为 *只读* 文件. 
如果你需要编辑这些文件中的任何一个, 可以要求我 *将它们添加到对话中*. """
    }
