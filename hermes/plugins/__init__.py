"""由调用方显式装配的最小 Plugin 接口。"""

from .context import AsyncPluginContext, PluginContext, SyncPluginContext


__all__ = [
    "AsyncPluginContext",
    "PluginContext",
    "SyncPluginContext",
]
