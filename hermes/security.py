"""Terminal 危险命令识别的稳定导入入口。"""

from hermes.tools.terminal_approval import (
    DANGEROUS_PATTERNS,
    detect_dangerous_command,
)


__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command"]
