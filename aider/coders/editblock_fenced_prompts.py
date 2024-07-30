# flake8: noqa: E501

from .editblock_prompts import EditBlockPrompts


class EditBlockFencedPrompts(EditBlockPrompts):
    example_messages = {
        "en": [
        dict(
            role="user",
            content="Change get_factorial() to use math.factorial",
        ),
        dict(
            role="assistant",
            content="""To make this change we need to modify `mathweb/flask/app.py` to:

1. Import the math package.
2. Remove the existing factorial() function.
3. Update get_factorial() to call math.factorial instead.

Here are the *SEARCH/REPLACE* blocks:

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
from flask import Flask
=======
import math
from flask import Flask
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
def factorial(n):
    "compute factorial"

    if n == 0:
        return 1
    else:
        return n * factorial(n-1)

=======
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
    return str(factorial(n))
=======
    return str(math.factorial(n))
>>>>>>> REPLACE
{fence[1]}
<<<<<<< HEAD
""",
        ),
        dict(
            role="user",
            content="Refactor hello() into its own file.",
        ),
        dict(
            role="assistant",
            content="""To make this change we need to modify `main.py` and make a new file `hello.py`:

1. Make a new hello.py file with hello() in it.
2. Remove hello() from main.py and replace it with an import.

Here are the *SEARCH/REPLACE* blocks:

{fence[0]}
hello.py
<<<<<<< SEARCH
=======
def hello():
    "print a greeting"

    print("hello")
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
main.py
<<<<<<< SEARCH
def hello():
    "print a greeting"

    print("hello")
=======
from hello import hello
>>>>>>> REPLACE
{fence[1]}
""",
        ),
    ],
    "zh": [
        dict(
            role="user",
            content="将 get_factorial() 更改为使用 math.factorial",
        ),
        dict(
            role="assistant",
            content="""为了做出这一更改, 我们需要修改 `mathweb/flask/app.py` 文件:

1. 导入 math 包. 
2. 删除现有的 factorial() 函数. 
3. 将 get_factorial() 更新为调用 math.factorial. 

以下是 *SEARCH/REPLACE* blocks:

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
from flask import Flask
=======
import math
from flask import Flask
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
def factorial(n):
    "compute factorial"

    if n == 0:
        return 1
    else:
        return n * factorial(n-1)

=======
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
mathweb/flask/app.py
<<<<<<< SEARCH
    return str(factorial(n))
=======
    return str(math.factorial(n))
>>>>>>> REPLACE
{fence[1]}
<<<<<<< HEAD
""",
        ),
        dict(
            role="user",
            content="将 hello() 重构为独立的文件.",
        ),
        dict(
            role="assistant",
            content="""为了做出这一更改, 我们需要修改 `main.py` 并创建一个新文件 `hello.py`:

1. 创建一个带有 hello() 的新文件 hello.py. 
2. 从 main.py 删除 hello() 并替换为导入语句. 

以下是 *SEARCH/REPLACE* blocks:

{fence[0]}
hello.py
<<<<<<< SEARCH
=======
def hello():
    "print a greeting"

    print("hello")
>>>>>>> REPLACE
{fence[1]}

{fence[0]}
main.py
<<<<<<< SEARCH
def hello():
    "print a greeting"

    print("hello")
=======
from hello import hello
>>>>>>> REPLACE
{fence[1]}
""",
        ),
    ]
    }