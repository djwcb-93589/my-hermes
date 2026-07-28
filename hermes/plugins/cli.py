"""Plugin 管理命令的轻量输出入口，不启动 Agent 或 Gateway。"""

from __future__ import annotations

from hermes.plugins.manager import PluginManager, PluginManagerError


def run_plugins_command(arguments: list[str]) -> int:
    """执行 plugins 子命令并返回进程退出码。"""
    if not arguments or arguments[0] not in {"list", "enable", "disable", "doctor"}:
        print(
            "Usage: plugins list | plugins enable <plugin-name> | "
            "plugins disable <plugin-name> | plugins doctor <plugin-name>"
        )
        return 2
    command = arguments[0]
    if command == "list" and len(arguments) != 1:
        print("error: InvalidArguments")
        return 2
    if command != "list" and len(arguments) != 2:
        print("error: InvalidArguments")
        return 2

    try:
        manager = PluginManager()
        if command == "list":
            _print_list(manager.list_plugins())
            return 0
        name = arguments[1]
        if command == "enable":
            result = manager.enable(name)
            print(f"{result.name}: {result.status}")
            return 0 if result.success else 1
        if command == "disable":
            result = manager.disable(name)
            print(f"{result.name}: {result.status}")
            return 0 if result.success else 1
        result = manager.doctor(name)
        _print_doctor(result)
        return 0 if result.ready else 1
    except PluginManagerError as exc:
        print(f"error: {exc.error_code}")
        return 1
    except Exception:
        # 管理命令不向用户暴露底层异常类型、消息或路径。
        print("error: InternalError")
        return 1


def _print_list(items) -> None:
    if not items:
        print("No Plugins discovered.")
        return
    print(
        "NAME             VERSION   SOURCE        ENABLED   MANIFEST  "
        "DUPLICATE  STATUS"
    )
    for item in items:
        version = item.version or "-"
        enabled = "yes" if item.enabled else "no"
        manifest = "yes" if item.manifest_valid else "no"
        duplicate = "yes" if item.duplicate else "no"
        print(
            f"{item.name:<16} {version:<9} {item.source_type:<13} "
            f"{enabled:<9} {manifest:<9} {duplicate:<10} {item.status}"
        )


def _print_doctor(result) -> None:
    for check in result.checks:
        suffix = f" ({check.detail})" if check.detail else ""
        print(f"[{check.status}] {check.name}{suffix}")
    if result.ready:
        print("Plugin is ready for CLI and Gateway.")
    else:
        print("Plugin cannot be enabled safely.")
