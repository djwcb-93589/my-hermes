# Python Plugin Hook 使用说明

Hermes 的 Plugin 是进程内可信 Python 代码。Plugin 可以观察 Hook 事件，也可以通过受约束的控制 Hook 返回 `Allow`、`Block` 或 `AddContext`；它不是沙箱，启用第三方 Plugin 前应先审查源码。

## 目录和 manifest

每个 Plugin 使用独立目录：

```text
<plugin-root>/<plugin-name>/
├── plugin.yaml
└── __init__.py
```

`plugin.yaml` 至少包含合法且稳定的 `name`、字符串 `version`，可选 `description`。目录名必须与 `name` 相同。`__init__.py` 必须提供同步的 `register(ctx)`；Runtime 不会运行异步 `register`，也不会修改 `sys.path`。

## 注册 Hook

```python
from hermes.hooks import Allow


def observe(context):
    return None


def register(ctx):
    ctx.register_hook(
        "post_tool_call",
        observe,
        hook_id="audit_tool_result",
    )
    ctx.register_hook(
        "pre_llm_call",
        lambda context: Allow(),
        hook_id="audit_llm_allow",
    )
```

Hook ID 会加上 Plugin 命名空间，最终形如 `audit-log:audit_tool_result`。未显式指定 ID 时使用稳定的包内模块名和 callback `qualname`。

当前事件包括 `pre_llm_call`、`pre_tool_call`、`post_llm_call`、`post_tool_call` 和 `run_end`。控制 Hook 失败、超时或返回无效值时默认阻止；观察 Hook 失败会与 Agent 主流程隔离。观察 Hook 的返回值只保存在结构化分发结果中供诊断使用，AgentLoop 不消费它们。

同步 CLI Registry 不接受异步 callback；Gateway 的 Async Registry 同时接受同步和异步 callback。同步 callback 通过线程执行，超时只能停止等待，不能强制终止线程，因此不要写死循环、无限阻塞或无超时网络请求。

## 管理命令

```text
uv run main.py plugins list
uv run main.py plugins enable audit-log
uv run main.py plugins disable audit-log
uv run main.py plugins doctor audit-log
```

管理命令只读取 manifest 或在临时 Registry 中诊断，不启动 AgentLoop、模型 Client、CLI Worker 或 Gateway。`enable` 只修改用户实际配置文件的 `plugins.enabled`，不导入或执行 Plugin；配置通过临时文件、flush、`fsync` 和原子替换保存，并使用进程锁和文件锁避免并发覆盖。配置修改在下一次 CLI 或 Gateway 启动时生效。

默认只扫描用户 Plugin 目录和显式 `search_paths`。项目目录 `./.my-hermes/plugins` 只有在 `enable_project_plugins: true` 时才会扫描。未列入 `plugins.enabled` 的 Plugin 不会被 Runtime 导入。

`doctor` 会分别创建临时 Sync 和 Async Registry，检查目录边界、manifest、动态命名空间导入、同步 `register`、Hook 注册兼容性和 `sys.path` 修改，并在完成后清理所有动态模块；它不会修改正式配置或生产 Registry。
