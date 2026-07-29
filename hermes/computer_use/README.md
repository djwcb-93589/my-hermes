# Computer Use

## 模块目标

`hermes.computer_use` 将承载独立、跨平台的电脑操作能力。本目录提供稳定的公开契约，使 Backend、驱动、CLI 和适配层可以在明确的模块边界内逐步实现。

## 当前阶段

当前已完成 P0 公开数据契约和 P1 Backend 抽象、统一错误契约。当前仍然：

- 没有任何真实 Backend；
- 不能截图；
- 不能点击；
- 不能输入；
- 不能连接 cua-driver；
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

未来的 cua-driver Backend、FakeBackend 和 NoopBackend 都必须实现同一个
`ComputerUseBackend` 接口。Backend 实现负责遵守统一生命周期、操作签名和异常契约。

## 图片边界

核心模块只保存原始图片字节，不负责转换为 OpenAI、Anthropic 或其他模型厂商的多模态消息格式。模型格式转换属于未来的 Agent 适配层。

## 后续阶段

- P2：Fake/Noop Backend
- P3：cua-driver 生命周期与通信
- P4：只读观察能力
