"""由调用方显式装配的最小 Plugin 接口。"""

from .context import AsyncPluginContext, PluginContext, SyncPluginContext
from .manager import (
    PluginDoctorCheck,
    PluginDoctorResult,
    PluginInspection,
    PluginManager,
    PluginManagerError,
    PluginOperationResult,
)
from .runtime import (
    AsyncPluginRuntime,
    PluginConfigurationError,
    PluginLoadResult,
    PluginLoadSummary,
    PluginManifestError,
    SyncPluginRuntime,
    discover_plugin_candidates,
)


__all__ = [
    "AsyncPluginContext",
    "AsyncPluginRuntime",
    "PluginDoctorCheck",
    "PluginDoctorResult",
    "PluginConfigurationError",
    "PluginContext",
    "PluginInspection",
    "PluginLoadResult",
    "PluginLoadSummary",
    "PluginManager",
    "PluginManagerError",
    "PluginManifestError",
    "PluginOperationResult",
    "discover_plugin_candidates",
    "SyncPluginContext",
    "SyncPluginRuntime",
]
