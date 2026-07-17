"""兼容旧导入；危险识别与授权判断的唯一实现位于 approval_policy。"""

from hermes.approval_policy import (
    DANGEROUS_PATTERNS,
    detect_dangerous_command,
)


__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command"]
