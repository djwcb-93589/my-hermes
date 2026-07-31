"""Dashboard 配置中心唯一允许列表及其安全注册边界。"""

from __future__ import annotations

from types import MappingProxyType

from hermes.config_model import DEFAULT_CONFIG

from .contracts import (
    ConfigApplyMode,
    ConfigFieldSpec,
    ConfigValueType,
)


class ConfigFieldRegistry:
    """不可变的显式配置字段目录，不接受运行时任意路径。"""

    __slots__ = ("_by_name", "_environment_override_keys", "_fields")

    def __init__(self, fields: tuple[ConfigFieldSpec, ...]) -> None:
        if type(fields) is not tuple or not fields:
            raise ValueError("config field registry must not be empty")
        by_name: dict[str, ConfigFieldSpec] = {}
        paths: set[tuple[str, ...]] = set()
        for field in fields:
            if not isinstance(field, ConfigFieldSpec):
                raise TypeError("config field registry contains invalid item")
            if field.public_name in by_name:
                raise ValueError("config field registry contains duplicate names")
            if field.config_path in paths:
                raise ValueError("config field registry contains duplicate paths")
            by_name[field.public_name] = field
            paths.add(field.config_path)
        for field in fields:
            if field.inherits_from is not None:
                inherited = by_name.get(field.inherits_from)
                if inherited is None or inherited.sensitive != field.sensitive:
                    raise ValueError(
                        "config field inheritance target is invalid"
                    )
                _validate_inheritance_chain(field, by_name)
        self._fields = fields
        self._by_name = MappingProxyType(by_name)
        self._environment_override_keys = tuple(dict.fromkeys(
            key
            for field in fields
            for key in field.environment_override_keys
        ))

    @property
    def fields(self) -> tuple[ConfigFieldSpec, ...]:
        return self._fields

    def get(self, name: object) -> ConfigFieldSpec | None:
        if type(name) is not str:
            return None
        return self._by_name.get(name)

    @property
    def environment_override_keys(self) -> tuple[str, ...]:
        """返回构建环境快照需要的固定内部键，不用于 API 投影。"""
        return self._environment_override_keys


def _validate_inheritance_chain(
    field: ConfigFieldSpec,
    by_name: dict[str, ConfigFieldSpec],
) -> None:
    visited = {field.public_name}
    current = field
    while current.inherits_from is not None:
        if current.inherits_from in visited:
            raise ValueError("config field inheritance contains a cycle")
        visited.add(current.inherits_from)
        inherited = by_name.get(current.inherits_from)
        if inherited is None:
            raise ValueError("config field inheritance target is invalid")
        current = inherited


_GATEWAY_RESTART = ConfigApplyMode.GATEWAY_RESTART
_APPLICATION_RESTART = ConfigApplyMode.APPLICATION_RESTART


def _field(
    name: str,
    value_type: ConfigValueType,
    *,
    default: object,
    nullable: bool = False,
    description: str,
) -> ConfigFieldSpec:
    return ConfigFieldSpec(
        public_name=name,
        config_path=tuple(name.split(".")),
        value_type=value_type,
        writable=True,
        sensitive=False,
        apply_mode=_GATEWAY_RESTART,
        nullable=nullable,
        has_default=True,
        default_value=default,
        description=description,
    )


def _sensitive_field(
    name: str,
    *,
    apply_mode: ConfigApplyMode = _GATEWAY_RESTART,
    environment_override_keys: tuple[str, ...] = (),
    inherits_from: str | None = None,
) -> ConfigFieldSpec:
    return ConfigFieldSpec(
        public_name=name,
        config_path=tuple(name.split(".")),
        value_type=ConfigValueType.STRING,
        writable=False,
        sensitive=True,
        apply_mode=apply_mode,
        nullable=False,
        has_default=False,
        environment_override_keys=environment_override_keys,
        inherits_from=inherits_from,
    )


_BROWSER_DEFAULTS = DEFAULT_CONFIG["browser"]
_REVIEW_DEFAULTS = DEFAULT_CONFIG["background_review"]
_PLUGIN_DEFAULTS = DEFAULT_CONFIG["plugins"]
_GATEWAY_DEFAULTS = DEFAULT_CONFIG["gateway"]


DEFAULT_CONFIG_FIELD_REGISTRY = ConfigFieldRegistry((
    _field(
        "browser.enabled",
        ConfigValueType.BOOLEAN,
        default=_BROWSER_DEFAULTS["enabled"],
        description="是否在下一次 Gateway 启动时启用 Browser 工具。",
    ),
    _field(
        "browser.headless",
        ConfigValueType.BOOLEAN,
        default=_BROWSER_DEFAULTS["headless"],
        description="Browser Runtime 是否使用无界面模式。",
    ),
    _field(
        "browser.channel",
        ConfigValueType.STRING,
        default=_BROWSER_DEFAULTS["channel"],
        nullable=True,
        description="Browser Runtime 使用的受支持浏览器通道。",
    ),
    _field(
        "browser.idle_timeout_seconds",
        ConfigValueType.NUMBER,
        default=_BROWSER_DEFAULTS["idle_timeout_seconds"],
        description="Browser Runtime 空闲回收时间。",
    ),
    _field(
        "browser.startup_timeout_seconds",
        ConfigValueType.NUMBER,
        default=_BROWSER_DEFAULTS["startup_timeout_seconds"],
        description="Browser Runtime 启动超时时间。",
    ),
    _field(
        "browser.operation_timeout_seconds",
        ConfigValueType.NUMBER,
        default=_BROWSER_DEFAULTS["operation_timeout_seconds"],
        description="Browser 单次操作超时时间。",
    ),
    _field(
        "background_review.enabled",
        ConfigValueType.BOOLEAN,
        default=_REVIEW_DEFAULTS["enabled"],
        description="是否在下一次 Gateway 启动时启用后台审视。",
    ),
    _field(
        "background_review.memory_interval",
        ConfigValueType.INTEGER,
        default=_REVIEW_DEFAULTS["memory_interval"],
        description="后台 Memory 审视间隔。",
    ),
    _field(
        "background_review.skill_tool_batch_interval",
        ConfigValueType.INTEGER,
        default=_REVIEW_DEFAULTS["skill_tool_batch_interval"],
        description="后台 Skill 工具批次间隔。",
    ),
    _field(
        "background_review.claim_ttl_seconds",
        ConfigValueType.NUMBER,
        default=_REVIEW_DEFAULTS["claim_ttl_seconds"],
        description="后台审视任务认领有效期。",
    ),
    _field(
        "background_review.retry_cooldown_seconds",
        ConfigValueType.NUMBER,
        default=_REVIEW_DEFAULTS["retry_cooldown_seconds"],
        description="后台审视失败后的重试冷却时间。",
    ),
    _field(
        "background_review.max_iterations",
        ConfigValueType.INTEGER,
        default=_REVIEW_DEFAULTS["max_iterations"],
        description="单个后台审视任务的最大迭代次数。",
    ),
    _field(
        "background_review.max_concurrent_jobs",
        ConfigValueType.INTEGER,
        default=_REVIEW_DEFAULTS["max_concurrent_jobs"],
        description="后台审视最大并发任务数。",
    ),
    _field(
        "background_review.max_pending_jobs",
        ConfigValueType.INTEGER,
        default=_REVIEW_DEFAULTS["max_pending_jobs"],
        description="后台审视最大待处理任务数。",
    ),
    _field(
        "plugins.enabled",
        ConfigValueType.STRING_LIST,
        default=tuple(_PLUGIN_DEFAULTS["enabled"]),
        description="下一次进程启动时显式启用的 Plugin 名称。",
    ),
    _field(
        "gateway.busy_input_mode",
        ConfigValueType.STRING,
        default=_GATEWAY_DEFAULTS["busy_input_mode"],
        description="Gateway 忙碌时处理新输入的受支持策略。",
    ),
    _field(
        "gateway.platforms.cli.enabled",
        ConfigValueType.BOOLEAN,
        default=_GATEWAY_DEFAULTS["platforms"]["cli"]["enabled"],
        description="下一次 Gateway 启动时是否启用 CLI Adapter。",
    ),
    _field(
        "gateway.platforms.feishu.enabled",
        ConfigValueType.BOOLEAN,
        default=_GATEWAY_DEFAULTS["platforms"]["feishu"]["enabled"],
        description="下一次 Gateway 启动时是否启用 Feishu Adapter。",
    ),
    _field(
        "gateway.platforms.weixin.enabled",
        ConfigValueType.BOOLEAN,
        default=_GATEWAY_DEFAULTS["platforms"]["weixin"]["enabled"],
        description="下一次 Gateway 启动时是否启用 Weixin Adapter。",
    ),
    _sensitive_field(
        "api_key",
        apply_mode=_APPLICATION_RESTART,
        environment_override_keys=("OPENAI_API_KEY",),
    ),
    _sensitive_field(
        "fallback.api_key",
        apply_mode=_APPLICATION_RESTART,
        environment_override_keys=("FALLBACK_API_KEY",),
        inherits_from="api_key",
    ),
    _sensitive_field("gateway.platforms.feishu.app_secret"),
    _sensitive_field("gateway.platforms.feishu.verification_token"),
    _sensitive_field("gateway.platforms.feishu.encrypt_key"),
    _sensitive_field("gateway.platforms.weixin.token"),
    _sensitive_field(
        "terminal.ssh_key",
        apply_mode=_APPLICATION_RESTART,
    ),
))


__all__ = [
    "DEFAULT_CONFIG_FIELD_REGISTRY",
    "ConfigFieldRegistry",
]
