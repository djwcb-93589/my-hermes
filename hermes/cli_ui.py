"""默认 CLI 的终端输入输出边界。"""

from __future__ import annotations

from contextlib import nullcontext
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout


PROMPT_TEXT = "You: "


def _is_interactive_terminal() -> bool:
    """判断当前标准输入和输出是否都可用于交互式终端。"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


class CLIInput:
    """复用一个交互式输入会话，并在非终端环境回退到原生输入。"""

    def __init__(self) -> None:
        self._session = PromptSession() if _is_interactive_terminal() else None

    def prompt(self) -> str:
        """读取一条用户输入；Ctrl+C 仅取消本次输入并重新提示。"""
        while True:
            try:
                if self._session is not None:
                    return self._session.prompt(PROMPT_TEXT)
                return input(PROMPT_TEXT)
            except KeyboardInterrupt:
                print()
            except EOFError:
                return ""


def patched_cli_stdout():
    """为整个交互式 CLI 会话协调普通 print 与正在编辑的输入行。"""
    if _is_interactive_terminal():
        return patch_stdout()
    return nullcontext()
