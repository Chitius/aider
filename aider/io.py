import base64
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.history import FileHistory
from pygments.lexers import MarkdownLexer, guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from rich.console import Console
from rich.text import Text
from termcolor import colored

from .dump import dump  # noqa: F401
from .utils import is_image_file

class InputOutput:
    num_error_outputs = 0
    num_user_asks = 0

    def __init__(
        self,
        pretty=True,
        yes=False,
        input_history_file=None,
        chat_history_file=None,
        input=None,
        output=None,
        user_input_color="blue",
        tool_output_color=None,
        tool_error_color="red",
        encoding="utf-8",
        dry_run=False,
        llm_history_file=None,
        editingmode=EditingMode.EMACS,
    ):
        self.editingmode = editingmode
        no_color = os.environ.get("NO_COLOR")
        if no_color is not None and no_color != "":
            pretty = False

        self.user_input_color = user_input_color if pretty else None
        self.tool_output_color = tool_output_color if pretty else None
        self.tool_error_color = tool_error_color if pretty else None

        self.input = input
        self.output = output

        self.pretty = pretty
        if self.output:
            self.pretty = False

        self.yes = yes

        self.input_history_file = input_history_file
        self.llm_history_file = llm_history_file
        if chat_history_file is not None:
            self.chat_history_file = Path(chat_history_file)
        else:
            self.chat_history_file = None

        self.encoding = encoding
        self.dry_run = dry_run

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=False, no_color=True)

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_chat_history(f"\n# aider chat started at {current_time}\n\n")

    def read_image(self, filename):
        try:
            with open(str(filename), "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read())
                return encoded_string.decode("utf-8")
        except FileNotFoundError:
            self.tool_error(f"{filename}: file not found error")
            return
        except IsADirectoryError:
            self.tool_error(f"{filename}: is a directory")
            return
        except Exception as e:
            self.tool_error(f"{filename}: {e}")
            return

    def read_text(self, filename):
        if is_image_file(filename):
            return self.read_image(filename)

        try:
            with open(str(filename), "r", encoding=self.encoding) as f:
                return f.read()
        except FileNotFoundError:
            self.tool_error(f"{filename}: file not found error")
            return
        except IsADirectoryError:
            self.tool_error(f"{filename}: is a directory")
            return
        except UnicodeError as e:
            self.tool_error(f"{filename}: {e}")
            self.tool_error("Use --encoding to set the unicode encoding.")
            return

    def write_text(self, filename, content):
        if self.dry_run:
            return
        with open(str(filename), "w", encoding=self.encoding) as f:
            f.write(content)

    def get_input(self, commands):
        # TODO: 需要提示用户可选的 commands
        if self.pretty:
            # style = dict(style=self.user_input_color) if self.user_input_color else dict()
            # self.console.rule(**style)
            try:
                length = os.get_terminal_size().columns
            except:
                length = 100
            colored_text = colored("-" * length, self.user_input_color)
            # 输出彩色文本
            print(colored_text)
        else:
            print()

        inp = input("> ") + "\n"
        self.user_input(inp)
        return inp

    def add_to_input_history(self, inp):
        if not self.input_history_file:
            return
        FileHistory(self.input_history_file).append_string(inp)

    def get_input_history(self):
        if not self.input_history_file:
            return []

        fh = FileHistory(self.input_history_file)
        return fh.load_history_strings()

    def log_llm_history(self, role, content):
        if not self.llm_history_file:
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        with open(self.llm_history_file, "a", encoding=self.encoding) as log_file:
            log_file.write(f"{role.upper()} {timestamp}\n")
            log_file.write(content + "\n")

    def user_input(self, inp, log_only=True):
        if not log_only:
            # style = dict(style=self.user_input_color) if self.user_input_color else dict()
            # self.console.print(inp, **style)
            print(colored(inp, self.user_input_color))

        prefix = "####"
        if inp:
            hist = inp.splitlines()
        else:
            hist = ["<blank>"]

        hist = f"  \n{prefix} ".join(hist)

        hist = f"""
{prefix} {hist}"""
        self.append_chat_history(hist, linebreak=True)

    # OUTPUT

    def ai_output(self, content):
        """ 把响应内容写入对话历史文件 (不进行 stdout 上的输出) """
        hist = "\n" + content.strip() + "\n\n"
        self.append_chat_history(hist)

    def confirm_ask(self, question, default="y"):
        self.num_user_asks += 1

        if self.yes is True:
            res = "yes"
        elif self.yes is False:
            res = "no"
        else:
            res = input(question + " ")

        hist = f"{question.strip()} {res.strip()}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)

        if not res or not res.strip():
            return
        return res.strip().lower().startswith("y")

    def prompt_ask(self, question, default=None):
        self.num_user_asks += 1

        if self.yes is True:
            res = "yes"
        elif self.yes is False:
            res = "no"
        else:
            res = input(question + " ")

        hist = f"{question.strip()} {res.strip()}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)

        return res

    def print(self, message = "", color = "white"):
        print(colored(message, color))

    def tool_error(self, message="", strip=True):
        self.num_error_outputs += 1

        if message.strip():
            if "\n" in message:
                for line in message.splitlines():
                    self.append_chat_history(line, linebreak=True, blockquote=True, strip=strip)
            else:
                if strip:
                    hist = message.strip()
                else:
                    hist = message
                self.append_chat_history(hist, linebreak=True, blockquote=True)

        # message = Text(message)
        # style = dict(style=self.tool_error_color) if self.tool_error_color else dict()
        # self.console.print(message, **style)
        print(colored(message, self.tool_error_color))
        

    def tool_output(self, *messages, log_only=False):
        """
        负责:
        1. tokens 数告知
        ...
        """
        if messages:
            hist = " ".join(messages)
            hist = f"{hist.strip()}"
            self.append_chat_history(hist, linebreak=True, blockquote=True)

        if not log_only:
            # messages = list(map(Text, messages))
            # style = dict(style=self.tool_output_color) if self.tool_output_color else dict()
            # self.console.print(*messages, **style)
            for message in messages:
                print(colored(message, self.tool_output_color))

    def append_chat_history(self, text, linebreak=False, blockquote=False, strip=True):
        if blockquote:
            if strip:
                text = text.strip()
            text = "> " + text
        if linebreak:
            if strip:
                text = text.rstrip()
            text = text + "  \n"
        if not text.endswith("\n"):
            text += "\n"
        if self.chat_history_file is not None:
            with self.chat_history_file.open("a", encoding=self.encoding) as f:
                f.write(text)