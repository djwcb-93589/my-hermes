"""与 Dashboard UI 无关的通用可观测契约与纯转换接口。

接入规则：普通工具以轻量 ToolDeclaration 共享 schema 和策略，再由运行时注册 handler；
能力目录只聚合声明快照；
后台组件可选发布 RuntimeComponentSnapshot；文件类结果可选发布 ArtifactRecord。
只有需要专用 UI 交互语义的模块才应注册 DashboardExtensionDescriptor，普通工具
和普通文件输出不需要反向依赖 Dashboard。
"""

from .artifacts import (
    ArtifactPublisher,
    ArtifactRecord,
    ArtifactStatus,
    ArtifactSummary,
    COMMON_ARTIFACT_KINDS,
    NullArtifactPublisher,
    project_artifact,
)
from .contracts import (
    CapabilityDescriptor,
    ModelCallObservation,
    NullObservationSink,
    ObservationSink,
    RunObservation,
    ToolCallObservation,
    ToolsetDescriptor,
    freeze_safe_metadata,
)
from .extensions import DashboardExtensionDescriptor, DashboardExtensionRegistry
from .hooks import register_observation_sink
from .runtime import (
    NullRuntimeStatusPublisher,
    RuntimeComponentReporter,
    RuntimeComponentSnapshot,
    RuntimeComponentState,
    RuntimeStatusPublisher,
)
from .tool_execution import ToolExecutionSummary, project_tool_execution


__all__ = [
    "ArtifactPublisher",
    "ArtifactRecord",
    "ArtifactStatus",
    "ArtifactSummary",
    "CapabilityDescriptor",
    "COMMON_ARTIFACT_KINDS",
    "DashboardExtensionDescriptor",
    "DashboardExtensionRegistry",
    "ModelCallObservation",
    "NullArtifactPublisher",
    "NullObservationSink",
    "NullRuntimeStatusPublisher",
    "ObservationSink",
    "RunObservation",
    "RuntimeComponentReporter",
    "RuntimeComponentSnapshot",
    "RuntimeComponentState",
    "RuntimeStatusPublisher",
    "ToolCallObservation",
    "ToolExecutionSummary",
    "ToolsetDescriptor",
    "freeze_safe_metadata",
    "project_artifact",
    "project_tool_execution",
    "register_observation_sink",
]
