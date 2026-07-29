# Computer Use

## 模块目标

`hermes.computer_use` 将承载独立、跨平台的电脑操作能力。本目录提供稳定的公开契约，使 Backend、驱动、CLI 和适配层可以在明确的模块边界内逐步实现。

## 当前阶段

当前已完成 P0 公开数据契约、P1 Backend 抽象与统一错误契约、P2
NoopBackend 和 FakeBackend、P3 cua-driver 进程生命周期和 MCP stdio
通信层，以及以下阶段：

- P3.6 已完成 Windows 子进程环境继承和启动说明；
- P4 已完成 cua-driver 只读观察 Backend。

`CuaDriverBackend` 当前支持：

- `capture`
- `list_apps`
- `list_windows`
- `wait`

当前明确不支持：

- `click`
- `double_click`
- `right_click`
- `middle_click`
- `drag`
- `scroll`
- `type`
- `key`
- `focus_app`
- `set_value`

因此当前仍然：

- 不能点击；
- 不能输入；
- 不能执行任何会修改电脑状态的动作；
- 没有 CLI；
- 没有 Agent 接入。

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
子进程，并把 P4 支持的应用、窗口、捕获和等待结果转换为正式数据契约。捕获以
驱动返回的窗口为目标，不会自动聚焦窗口或启动应用。

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

## 图片边界

核心模块只保存原始图片字节，不负责转换为 OpenAI、Anthropic 或其他模型厂商的多模态消息格式。模型格式转换属于未来的 Agent 适配层。

## 后续阶段

- P5/P6：逐步实现会修改电脑状态的动作及其必要边界
