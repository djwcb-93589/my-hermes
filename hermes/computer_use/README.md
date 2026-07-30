# Computer Use

## 模块目标

`hermes.computer_use` 将承载独立、跨平台的电脑操作能力。本目录提供稳定的公开契约，使 Backend、驱动、CLI 和适配层可以在明确的模块边界内逐步实现。

## 当前阶段

当前已完成 P0 公开数据契约、P1 Backend 抽象与统一错误契约、P2
NoopBackend 和 FakeBackend、P3 cua-driver 进程生命周期和 MCP stdio
通信层，以及以下阶段：

- P3.6 已完成 Windows 子进程环境继承和启动说明；
- P4 已完成 cua-driver 只读观察 Backend；
- P4.5.1 已修复普通 `data` 字段被误判为图片的问题；
- P5 已实现 session 隔离、活动目标、点击、文本输入、按键和应用目标选择；
- P6 已实现 `drag`、`scroll` 和 `set_value`。

`CuaDriverBackend` 当前支持：

- `capture`
- `click`
- `double_click`
- `right_click`
- `middle_click`
- `drag`
- `scroll`
- `type`
- `key`
- `set_value`
- `list_apps`
- `list_windows`
- `focus_app`
- `wait`

所有写操作必须先通过 `capture` 或 `focus_app` 选择活动窗口。元素拖动、元素
滚动和 `set_value` 还必须使用最近一次 `capture` 返回的元素。拖动支持元素到
元素和坐标到坐标；滚动支持窗口、元素和坐标目标。不支持的精确参数会返回明确
失败或降级结果，不会被静默忽略。

`focus_app` 不会抢占真实桌面焦点，也不会自动启动应用。当前仍未接入审批、
安全策略、CLI 和 Agent。

## 依赖方向

允许的依赖方向是：

```text
未来的 Agent 适配层
        ↓
hermes.computer_use
```

禁止 Computer Use 核心模块反向依赖现有 Agent 系统：

```text
hermes.computer_use
        ↓
AgentLoop / Conversation / Gateway / Tool Registry
```

核心模块只能依赖 Python 标准库和本目录内的模块。

## 公开入口

未来所有调用方都应面向统一协议：

```python
ComputerUseExecutor.execute(...)
```

调用结果必须使用 `ComputerUseResult` 定义的正式返回类型，不能用裸字典替代稳定契约。

`CuaDriverBackend`、FakeBackend 和 NoopBackend 都实现同一个
`ComputerUseBackend` 接口。Backend 实现负责遵守统一生命周期、操作签名和异常契约。

NoopBackend 用于最简单的调用链检查，只返回空捕获或不可验证的动作结果。
FakeBackend 用于在内存中配置应用、窗口、捕获结果、动作结果和异常，并记录调用。
两者都不会操作真实电脑。`CuaDriverBackend` 通过 P3 transport 管理 cua-driver
子进程，并把观察结果和 P6 基础动作转换为正式数据契约。Backend 为每个实例创建
独立 session；驱动不支持 session 或启动 session 失败时，会降级为匿名调用。
捕获以驱动返回的窗口为目标，不会自动聚焦窗口或启动应用。

## Windows 启动

transport 使用 `shell=False` 和原生参数序列启动 cua-driver。命令中必须显式包含
驱动所需的 `mcp` 子命令：

```python
from hermes.computer_use.transport import CuaDriverConfig

config = CuaDriverConfig(
    command=["cua-driver", "mcp"],
)
```

如果 cua-driver 没有加入 VSCode 进程继承的 `PATH`，可以使用 Windows 原生绝对
路径：

```python
from hermes.computer_use.transport import CuaDriverConfig

config = CuaDriverConfig(
    command=[
        r"C:\tools\cua-driver\cua-driver.exe",
        "mcp",
    ],
)
```

不要传入 `/c/tools/...` 形式的 Git Bash 路径。transport 不会自动添加 `"mcp"`，
也不会自动安装、下载或查找 cua-driver，更不会修改系统 `PATH`。`env` 仅表示在
父进程环境基础上覆盖或增加的变量。

## P6.5 驱动边界安全

P6.5 会在启动 cua-driver 前清理其子进程环境中继承的模型 API Key、访问令牌和客户端密钥；即使这些变量出现在 `CuaDriverConfig.env` 覆盖项中，也会在最终启动前再次移除。驱动仍会继承 `PATH`、`SystemRoot`、`TEMP` 等启动所需环境变量。

可通过 `check_cua_driver_readiness()` 独立检查 cua-driver 的安装和健康状态。该检查不会自动安装驱动、修改 `PATH` 或请求系统权限。解释器退出时，transport 会尽力关闭仍登记为活动状态的 cua-driver 子进程。

## 图片边界

核心模块只保存原始图片字节，不负责转换为 OpenAI、Anthropic 或其他模型厂商的多模态消息格式。模型格式转换属于未来的 Agent 适配层。

## 后续阶段

- 后续阶段将独立处理审批、安全策略和 Agent 适配
