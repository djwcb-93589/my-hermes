"""Computer Use 提供的内存化 Backend 实现。"""

from .fake import FakeBackend, FakeCall
from .noop import NoopBackend

__all__ = [
    "NoopBackend",
    "FakeBackend",
    "FakeCall",
]
