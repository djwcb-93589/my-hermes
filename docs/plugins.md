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

入口可以直接定义，也可以从包内模块导入：

```python
from .registration import register
```

`plugins enable` 只做静态入口检查，不执行这段代码；最终是否真的可调用仍由 Runtime 和 doctor 的隔离动态检查确认。

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

## Plugin 放在哪里

默认用户目录是 `<HERMES_HOME>/plugins`；例如将 `HERMES_HOME` 设置为 `~/.hermes` 时：

```text
~/.hermes/plugins/<plugin-name>/
```

也可以在用户配置文件中增加搜索目录。环境变量会和生产配置使用相同规则展开：

```yaml
plugins:
  enabled: []
  search_paths:
    - ${PLUGIN_HOME}/plugins
  enable_project_plugins: false
```

项目 Plugin 放在当前工作目录的 `./.my-hermes/plugins/<plugin-name>/`。它默认不扫描；只有显式写入下面的配置后才允许发现：

```yaml
plugins:
  enable_project_plugins: true
```

`examples/plugins/` 只是示例目录，不会因为示例文件存在而自动进入生产搜索路径。

## 五个事件什么时候发生

- `pre_llm_call`：压缩和取消检查完成后、模型请求发出前。控制 Hook 可以决定是否允许本次请求，`AddContext` 只进入本次请求副本。
- `post_llm_call`：模型成功返回、assistant 消息处理和持久化完成后。它适合记录完成原因、文本和 token 摘要。
- `pre_tool_call`：工具名称和参数通过硬安全检查后、真正执行前。它可以在工具执行前阻止调用。
- `post_tool_call`：工具结果标准化并持久化完成后。它只能观察结果，不能撤销已经完成的操作。
- `run_end`：`AgentLoopResult` 已生成、即将返回调用方之前。它适合观察本次运行的状态和停止原因。

`pre_llm_call` 与 `pre_tool_call` 是控制型事件，控制 Hook 失败、超时或返回无效值时默认阻止；另外三个是观察型事件，失败会隔离，不改变 Agent 主流程。观察型 Hook 的返回值只保存在结构化诊断结果中。

控制 Hook 示例：

```python
from hermes.hooks import AddContext, Allow, Block


def register(ctx):
    ctx.register_hook("pre_llm_call", lambda context: Allow(), hook_id="allow")
    ctx.register_hook(
        "pre_tool_call",
        lambda context: Block("tool is not allowed in this situation"),
        hook_id="block_tool",
    )
    ctx.register_hook(
        "pre_llm_call",
        lambda context: AddContext("只加入本次模型请求的短暂提示"),
        hook_id="request_context",
    )
```

## doctor 的边界

成功示例：

```text
[PASS] plugin discovered
[PASS] unique plugin name
[PASS] manifest valid
[PASS] register callable
[PASS] sync registration compatible
[PASS] async registration compatible
Plugin is ready for CLI and Gateway.
```

重复名称会立即停止，不会任选一个目录执行：

```text
[PASS] plugin discovered
[FAIL] unique plugin name (DuplicatePluginName)
Plugin cannot be enabled safely.
```

manifest 或异步 `register` 示例：

```text
[FAIL] manifest valid (InvalidManifest)
Plugin cannot be enabled safely.

[FAIL] sync registration compatible (AsyncRegisterNotAllowed)
Plugin cannot be enabled safely.
```

`doctor` 会真实导入 Plugin 模块并调用一次 `register(ctx)`，只是使用相互隔离的临时 Sync/Async Registry，之后清理动态模块、子模块并恢复 `sys.path`。因此 doctor 不是沙箱，也不能证明第三方 Plugin 安全；启用不熟悉的 Plugin 前仍应审查其源码。

`enable` 和 `disable` 修改配置后不会热更新当前进程。请重启 CLI 或 Gateway，配置变更才会加载。

## 搜索范围和 doctor 的边界

项目 Plugin 的固定目录是当前工作目录下的
`.my-hermes/plugins/<plugin-name>/`。只有 `enable_project_plugins: true` 时，
这个目录才会加入活动搜索根目录；目录中的符号链接目标也必须仍位于
`.my-hermes/plugins/` 内，不能指向 `.my-hermes` 的其他目录、用户目录或外部路径。

禁用项目 Plugin 不会参与活动搜索根目录的重名判断。例如用户目录和项目目录都
有 `audit-log`，但项目扫描未开启时，生产 Runtime 和 `doctor audit-log` 都只看
用户目录的候选，不会报告重复名称。只有活动搜索根目录完全找不到候选时，doctor
才会检查项目目录；如果那里存在 Plugin，会报告：

```text
[PASS] plugin discovered
[FAIL] project plugins explicitly enabled (ProjectPluginsDisabled)
Plugin cannot be enabled safely.
```

## `.env` 和静态入口检查

CLI/Gateway 生产启动与 `plugins` 管理命令都会先读取 `<HERMES_HOME>/.env`，再展开
`config.yaml` 中的 `${VAR}`。已经存在的系统环境变量优先，`.env` 不会覆盖它们；
缺失的变量仍保留原占位符。

`plugins enable` 的静态检查只分析 Plugin `__init__.py` 的顶层入口。顶层的同步
`def register(ctx)`、`from .registration import register` 和
`from .registration import register as register` 都可以通过；顶层异步定义或
`register = something`、`register: object = something`、`register += something`
会被拒绝。函数体、类体或其他嵌套作用域里的局部变量不会被误认为 Plugin 入口。
最终是否真实可调用，仍由 Runtime 和 doctor 的动态导入检查确认。
