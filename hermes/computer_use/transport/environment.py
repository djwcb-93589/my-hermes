"""cua-driver 子进程的最小安全环境构造。"""

import os
from collections.abc import Mapping


_SENSITIVE_ENVIRONMENT_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GLM_API_KEY",
        "ZHIPUAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "COHERE_API_KEY",
        "XAI_API_KEY",
    }
)
_SENSITIVE_ENVIRONMENT_SUFFIXES = (
    "_API_KEY",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_CLIENT_SECRET",
)
_REQUIRED_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
    }
)


def build_cua_driver_env(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造移除模型密钥后供 cua-driver 继承的子进程环境。"""

    environment = _deduplicate_windows_environment(dict(os.environ))
    _remove_sensitive_variables(environment)
    _apply_overrides(environment, overrides)
    _remove_sensitive_variables(environment)
    return _deduplicate_windows_environment(environment)


def _apply_overrides(
    environment: dict[str, str],
    overrides: Mapping[str, str] | None,
) -> None:
    """应用配置覆盖项，并在 Windows 上按不区分大小写的名称合并。"""

    if not overrides:
        return
    if os.name != "nt":
        environment.update(overrides)
        return

    normalized_keys = {
        name.upper(): name
        for name in environment
    }
    for name, value in overrides.items():
        normalized_name = name.upper()
        previous_name = normalized_keys.get(normalized_name)
        if previous_name is not None:
            environment.pop(previous_name, None)
        final_name = "PATH" if normalized_name == "PATH" else name
        environment[final_name] = value
        normalized_keys[normalized_name] = final_name


def _remove_sensitive_variables(environment: dict[str, str]) -> None:
    """删除敏感变量名，且不读取或记录其值。"""

    for name in tuple(environment):
        if _is_sensitive_variable(name):
            environment.pop(name, None)


def _is_sensitive_variable(name: str) -> bool:
    """判断变量名是否属于需要隔离的密钥或令牌。"""

    normalized_name = name.upper()
    if normalized_name in _REQUIRED_ENVIRONMENT_NAMES:
        return False
    return (
        normalized_name in _SENSITIVE_ENVIRONMENT_NAMES
        or normalized_name.endswith(_SENSITIVE_ENVIRONMENT_SUFFIXES)
    )


def _deduplicate_windows_environment(
    environment: dict[str, str],
) -> dict[str, str]:
    """保留 Windows 环境变量的最后一个不区分大小写的定义。"""

    if os.name != "nt":
        return dict(environment)

    deduplicated: dict[str, str] = {}
    normalized_keys: dict[str, str] = {}
    for name, value in environment.items():
        normalized_name = name.upper()
        previous_name = normalized_keys.get(normalized_name)
        if previous_name is not None:
            deduplicated.pop(previous_name, None)
        final_name = "PATH" if normalized_name == "PATH" else name
        deduplicated[final_name] = value
        normalized_keys[normalized_name] = final_name
    return deduplicated
