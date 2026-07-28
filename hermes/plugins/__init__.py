"""由调用方显式装配的最小 Plugin 接口。"""

from .context import AsyncPluginContext, PluginContext, SyncPluginContext
from .runtime import (
    AsyncPluginRuntime,
    PluginConfigurationError,
    PluginLoadResult,
    PluginLoadSummary,
    PluginManifestError,
    SyncPluginRuntime,
)


__all__ = [
    "AsyncPluginContext",
    "AsyncPluginRuntime",
    "PluginConfigurationError",
    "PluginContext",
    "PluginLoadResult",
    "PluginLoadSummary",
    "PluginManifestError",
    "SyncPluginContext",
    "SyncPluginRuntime",
]
