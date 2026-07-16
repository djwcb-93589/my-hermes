"""本机路径表示转换工具。"""

from __future__ import annotations

from pathlib import PureWindowsPath


def windows_to_git_bash_path(win_path: str) -> str:
    """把 Windows 路径转换成 Git Bash/MSYS 路径。"""
    path = PureWindowsPath(win_path)
    if len(path.drive) == 2 and path.drive[1] == ":":
        drive_letter = path.drive[0].lower()
        rest = str(path)[len(path.drive):].lstrip("\\/").replace("\\", "/")
        return f"/{drive_letter}/{rest}" if rest else f"/{drive_letter}"
    return str(path).replace("\\", "/")


def git_bash_to_windows_path(bash_path: str) -> str:
    """把 ``/d/path`` 形式的 Git Bash/MSYS 路径转换成 Windows 路径。"""
    if (
        len(bash_path) >= 2
        and bash_path[0] == "/"
        and bash_path[1].isalpha()
        and (len(bash_path) == 2 or bash_path[2] == "/")
    ):
        drive = bash_path[1].upper()
        rest = bash_path[3:].replace("/", "\\") if len(bash_path) > 2 else ""
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return bash_path
