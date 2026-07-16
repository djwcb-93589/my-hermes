# 飞书 Gateway 工具模块性能测试报告

> 测试日期：2026-07-16（Asia/Shanghai）  
> 测试对象：`my-hermes` 飞书 Gateway 下除 `cron` 外的全部工具模块  
> 工具入口：`terminal`、`file`、`memory`、`skills_list`、`skill_view`、`skill_manage`、`delegate_task`、`delegate_status`、`delegate_result`、`delegate_cancel`  
> 最终结论：**条件通过；不能判定为“全部正常”**

## 1. 结论摘要

本轮共验证 81 个不同语义的全链路功能场景，79 个通过，2 个失败：

| 模块 | 结果 | 结论 |
|---|---:|---|
| Terminal | 9 / 9 | 通过 |
| File | 20 / 20 | 通过；越界场景经修正后复测通过 |
| Memory | 16 / 16 | 通过 |
| Skill | 19 / 19 | 通过 |
| Delegate | 14 / 16 | 条件通过；查询 cancelled job 时无回复 |
| 跨工具审批 | 1 / 1 | 通过 |
| **合计** | **79 / 81** | **97.53%** |

当前模块可以支持日常 File、Terminal、Memory、Skill 操作，也可以使用 Delegate 的同步任务、后台提交、running 查询、取消请求和 completed 查询。唯一被真实复现的工具链路缺陷是：

- `delegate_status` 查询 cancelled job 时，Webhook 已 ACK，工具也已执行，但飞书端 20 秒内没有业务回复。
- `delegate_result` 查询 cancelled job 时表现相同。

因此：

- 如果当前使用范围不依赖“取消后台 Delegate 后再查询状态/结果”，工具模块可以正常使用。
- 如果要求所有设计场景都正常，则当前版本不通过，必须先修复 cancelled Delegate 的静默无回复问题。

另发现一个不属于工具处理器本身、但会影响旧部署升级的 Gateway 问题：从 schema v4 迁移数据库时，迁移过程可能因 `messages` 表不存在而失败。全新或当前 schema 数据库不受该用例影响。

## 2. 测试方法与真实性边界

### 2.1 当前真实配置启动检查

使用当前 `config.yaml` 和真实 Gateway 入口后台启动：

```text
uv run --frozen python main.py --gateway
```

实际结果：

- 监听：`127.0.0.1:8787/feishu/webhook`
- `/livez`：HTTP 200
- `/readyz`：HTTP 200
- readiness 中 lifecycle、runtime lease、adapter、inbox restore、webhook receiving、dispatcher、database read/write 全部为 `true`
- 使用当前 verification token 的 URL verification：HTTP 200，challenge 原样返回
- 使用错误 token：HTTP 403
- 完整测试结束后再次检查 `/livez`、`/readyz`：仍为 HTTP 200
- 测试结束后 Gateway 已正常停止，没有遗留监听进程

真实配置检查只验证启动、鉴权、租约、数据库和 Webhook 健康度；没有向真实飞书联系人批量发送测试消息。

### 2.2 隔离的真实 Gateway 全链路

功能和性能测试使用以下真实组件：

- `GatewayRunner`
- `FeishuAdapter`
- 飞书 Webhook 解析、鉴权、去重和 ACK
- SQLite inbox、message queue、conversation、outbox、delivery、approval 表
- AgentLoop 工具调用、错误分类和会话恢复
- File、Terminal、Memory、Skill、Delegate 的生产 handler
- 本机 LocalBackend / Git Bash
- 远程异步审批状态机，包括 `/approve`、`/deny`、`/stop`

为防止污染正式数据，以下状态均被隔离：

- 测试数据库
- 文件工作目录
- `MEMORY.md` / `USER.md`
- skills 目录和 trusted skill 状态
- 飞书开放平台回发接口
- LLM 响应

LLM 使用本地确定性脚本返回 tool call，飞书开放平台使用本地 HTTP 替身。这样测得的是 Gateway、审批、工具执行和可靠投递性能，不包含真实飞书公网延迟、真实模型推理延迟或模型选工具的准确率。

全部模拟飞书访问由一个独立子 agent 按矩阵执行；主测试进程只负责启动服务、观察日志、采集 SQLite/文件系统证据和资源快照。

## 3. 覆盖矩阵

### 3.1 Terminal

覆盖：

- 纯 `cd`、`pwd` 免审批
- `ls` 普通读取命令必须审批
- 非零退出码和 stderr
- 命令输出中的 API key 脱敏
- shell 写文件
- `rm` 拒绝后不执行
- `rm` 批准后只执行一次
- `cd . && pwd` 复合命令仍需审批
- cwd 与 File 工具共享并持久化

结果：9 / 9 通过。

### 3.2 File

覆盖全部 action：

- `pwd`、`context`
- `list`、`stat`
- `read`、`read_range`
- `write`、`append`、`replace`

覆盖边界：

- 所有 path action 在 Gateway 中先审批
- 读取内容中的明确凭证脱敏
- write 默认拒绝覆盖、`overwrite=true` 后覆盖
- replace 首个匹配、全部匹配、无匹配
- 文件不存在
- 敏感 `.env` 默认拒绝
- 真正越过 `file_root` 的路径在审批前拒绝
- unknown action、缺少 path
- `/deny` 后文件不存在
- `/stop` 后待审批写入被取消，之后 `/approve` 不能执行

首次越界用例使用 `../../../../outside.txt`，从隔离 workspace 解析后仍位于 `D:\my-hermes` 内，因此产品正确进入审批、但测试误判失败。修正为真正越界的 `../../../../../outside.txt` 后，定向复测结果为：不询问审批、返回安全失败、总耗时 784.263 ms。

结果：20 / 20 通过。

### 3.3 Memory

覆盖：

- memory/user 两个 target
- 空读、add/read/remove/replace
- 重复条目
- substring 多匹配歧义
- invalid target、unknown action、空内容
- `§` 分隔符拒绝
- prompt injection / credential 类内容拒绝
- 文件夹自动初始化
- 原子写失败保持原文件
- 并发写不丢条目
- lock timeout

结果：

- 飞书全链路：16 / 16 通过
- 基础设施补测：3 / 3 通过

### 3.4 Skill

覆盖：

- `skills_list`
- `skill_view`
- `skill_manage` 的 create/edit/patch/delete
- duplicate create、not found、invalid name、path traversal
- patch 无匹配、多个匹配、唯一匹配
- 中文正文、rich frontmatter、非法目录跳过
- 原子写失败
- medium-risk skill 在无人值守 Gateway 中要求确认并阻止正文返回
- high-risk skill 在无人值守 Gateway 中直接 safety block
- 删除 skills root 防护

结果：

- 飞书全链路：19 / 19 通过
- handler 直接补测：19 / 19 通过

### 3.5 Delegate

覆盖：

- 同步 child 成功
- child toolset 白名单
- 禁止 memory/delegate/cron 和 `skill_manage`
- Gateway 远程模式下禁止委托 File/Terminal，由主 agent 直接发起精确审批
- invalid goal、invalid toolset
- 后台 submit
- running status
- running result（返回 `Job is still running`）
- cancel request
- completed status/result
- cancelled status/result
- unknown job 和缺少 job_id
- child backend 清理
- 两个 child 的 cwd/文件隔离
- 不落入 default session

结果：14 / 16 通过。失败仅为 cancelled status/result。

### 3.6 审批与日常组合场景

覆盖：

- 待审批时普通消息被拦截，不启动新任务
- 审批只能由原请求者处理
- 审批执行后不能 replay
- `/deny` 后不执行
- `/stop` 取消 pending approval
- 一个模型响应中含多个受控 tool call 时，只创建首个审批，后续调用返回 `approval_deferred`
- File 写入、Terminal 删除、Memory 持久化、Skill 生命周期、Delegate 后台任务连续运行

文件系统最终证据：

- `generated.txt` 内容为 `ONE ONE two three`
- 被拒绝的 `denied.txt` 不存在
- `/stop` 取消的 `stop-cancelled.txt` 不存在
- 批准删除的文件不存在
- 拒绝删除的文件仍存在
- 被 defer 的 Terminal 写入文件不存在
- 批准创建的 Terminal 文件存在

审批表最终状态：

- executed：24
- failed：4
- denied：2
- cancelled：1

其中 4 个 failed 是“审批成功后工具按预期返回业务错误”，对应 exists、replace no match、file not found 等负向用例，不是审批基础设施故障。

## 4. 性能结果

### 4.1 功能链路延迟

79 个成功语义场景（已用正确越界复测替换错误夹具，并加入 running/completed Delegate 补测）：

| 指标 | count | min | mean | p50 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| 首条业务回复 | 79 | 716.791 ms | 792.154 ms | 780.804 ms | 841.315 ms | 1153.541 ms |
| 端到端完成 | 79 | 716.861 ms | 1052.082 ms | 800.649 ms | 1702.139 ms | 3818.825 ms |

说明：

- 端到端 max 来自包含 pending probe、错误用户审批和 replay 的复合审批场景。
- 百分位使用线性插值。
- 两个 cancelled Delegate 查询失败均在客户端 20 秒超时，未纳入成功延迟分布。

76 个基础功能请求的 Webhook ACK：

| count | min | mean | p50 | p95 | max |
|---:|---:|---:|---:|---:|---:|
| 76 | 7.624 ms | 21.740 ms | 22.651 ms | 33.954 ms | 39.473 ms |

22 个有效审批决策回合（已排除最初错误的越界夹具）：

| count | min | mean | p50 | p95 | max |
|---:|---:|---:|---:|---:|---:|
| 22 | 77.537 ms | 774.240 ms | 778.514 ms | 947.101 ms | 964.303 ms |

### 4.2 并发吞吐

| 场景 | 请求数 | 结果 | wall time | 吞吐 |
|---|---:|---:|---:|---:|
| 10 并发、40 个免审批只读/上下文操作 | 40 | 40 / 40 | 5512.983 ms | 7.256 ops/s |
| 8 个并发 File stat 审批完整回合 | 8 | 8 / 8 | 3149.759 ms | 2.540 approval ops/s |

只读并发 ACK：p50 156.695 ms，p95 276.171 ms，max 295.080 ms，40 个请求全部 HTTP 200。

审批并发中，8 个请求全部收到审批问题，8 个批准后全部收到最终结果，没有串 route、丢审批或重复执行。

### 4.3 持久化与投递一致性

主功能/并发轮次结束时：

- inbox processed：159
- outbox delivered：157
- outbox pending/retry/permanent_failed：0
- 飞书平台替身收到业务消息：157
- exactly two processed inbox 没有 outbox；正好对应 cancelled `delegate_status` 和 `delegate_result`

这说明其余链路没有发现消息丢失；两个异常也被数据库证据精确定位为 Agent 结果被静默吞掉，而不是飞书发送失败。

### 4.4 资源快照

同一隔离 Gateway 进程：

| 阶段 | CPU seconds | Working Set | Private Memory | Threads | Handles |
|---|---:|---:|---:|---:|---:|
| 启动后 | 1.25 | 68.76 MB | 52.92 MB | 11 | 218 |
| 中途 | 13.59 | 38.40 MB | 56.04 MB | 10 | 232 |
| 结束前 | 22.84 | 20.94 MB | 58.36 MB | 12 | 239 |

短测期间没有崩溃或 readiness 下降。Private Memory 增加约 5.44 MB；样本时间较短，不能据此判定存在或不存在长期内存泄漏。长期 soak 和纯传输层结论应继续参考原有 `飞书Gateway性能测试报告.md`。

## 5. 缺陷分析

### 5.1 P1：cancelled Delegate 的 status/result 在飞书中静默无回复

稳定复现流程：

1. `delegate_task(background=true)` 提交一个 4 秒任务：通过，约 804 ms。
2. `delegate_status` 在运行中查询：通过，返回 `status=running`，约 785 ms。
3. `delegate_result` 在运行中查询：通过，返回 `Job is still running`，约 735 ms。
4. `delegate_cancel`：通过，返回 `cancel_requested`，约 797 ms。
5. 等 child 落入 cancelled 终态后调用 `delegate_status`：Webhook ACK 200，20,036.798 ms 内无业务回复。
6. 调用 `delegate_result`：Webhook ACK 200，20,046.180 ms 内无业务回复。

数据库和日志证据：

- 两个 tool call 都已执行并写入 conversation history。
- status Tool Result 为 `ok=true, status=cancelled, error="cancel requested"`。
- result Tool Result 为 `ok=false, status=cancelled, error="cancel requested"`。
- 两条 inbox 最终都为 processed。
- 两条消息都没有 outbox。

根因推断：

1. 通用 AgentLoop 看到顶层 `error` 后，把 Tool Result 视为错误。
2. fatal marker 扫描又命中字符串 `cancelled`。
3. 结果被升级为 `error_type=cancelled`。
4. Gateway 将它误认为“当前用户消息本身已被取消”，把响应映射为 `None`。
5. `_process` 对“无 response、无 outbox”直接完成 inbox，因此用户看到静默。

建议修复方向：

- 不要仅凭 status/result payload 中出现 `cancelled` 字符串就把当前 Agent 消息判为 cancelled。
- `delegate_status` 的 `ok=true` 状态视图应始终作为正常 Tool Result 交回模型。
- `delegate_result` 的 cancelled job 应作为“可展示的终态结果”，而不是取消当前 Gateway 请求。
- 增加 Gateway 回归测试，断言 cancelled status/result 均产生 outbox 和用户回复。

### 5.2 P1：schema v4 数据库迁移失败

完整 pytest 中 `test_v4_database_migrates_to_gateway_outbox` 失败：`_migrate_v6_to_v7` 创建带外键的 `gateway_message_deliveries` 并插入时，旧测试数据库没有 `messages` 表，SQLite 报 `no such table: main.messages`。

影响：旧版本数据库跨多个 schema 升级时可能无法启动。当前全新测试库和当前正式库启动、readiness 均正常。

建议：为各历史 schema 准备真实最小 fixture，保证基础 sessions/messages 表在依赖它们的迁移步骤前存在，并测试 v4 到当前版本的完整迁移链。

## 6. 自动化回归结果与测试漂移

### 6.1 通过项

- Gateway 审批、工具暴露和工具质量专项：28 / 28 passed
- Memory 基础设施：3 / 3 passed
- Skill handler：19 / 19 passed
- 当前兼容的 Delegate 白名单、清理、隔离补测：9 / 9 passed

### 6.2 全量 pytest

结果：

```text
194 passed, 10 failed, 2 xfailed, 9 errors
```

其中：

- 9 个 collection error 来自 `memory_test.py` 被 pytest 当作 fixture 测试收集，但该文件实际是自带 runner 的脚本，`ctx` 不是 pytest fixture；按正确方式执行 `python memory_test.py infra` 后 3 / 3 通过。
- 多个 error sanitizer 断言仍要求隐藏所有外部绝对路径、只保留头部截断；当前实现明确采用“只脱敏凭证值、工作区内路径相对化、外部路径保留、首尾共同截断”，属于测试与当前策略不一致。
- 一项旧断言要求单次 tool dispatch/JSON 错误立即 `tool_error`；当前 AgentLoop 允许非致命参数错误进入下一轮让模型自纠，旧 fake client 没有提供下一轮响应，最终变成 `model_error`。
- `delegate_test.py` 仍引用旧的 `dlg.client`，生产代码已使用 `_default_client`。在测试进程内适配名称后，当前兼容的 Delegate 套件通过。
- v4 数据库迁移失败是真实兼容性问题，不归类为测试漂移。

这些测试维护问题不会改变本报告的全链路工具结论，但会让仓库的“一键全量 pytest”目前无法作为绿色发布门禁。

## 7. 最终评估

### 可以确认正常的部分

- 当前配置能启动真实 Feishu Gateway，鉴权、租约、数据库和 readiness 正常。
- File/Terminal 的远程异步审批能够控制读、写、删除和普通命令。
- 纯 `cd`、`pwd` 以及 File `pwd/context` 按设计免审批。
- 拒绝、取消、错误用户、replay、多 tool call defer 均能阻止未授权副作用。
- File、Terminal、Memory、Skill 的全部工具入口和主要异常分支正常。
- Delegate 的同步、后台 submit、running 和 completed 路径正常。
- 40 路只读和 8 路审批并发测试没有消息丢失或跨会话执行。

### 尚不能确认正常的部分

- cancelled Delegate 的 status/result 会静默无回复。
- v4 历史数据库升级到当前 schema 的迁移链不可靠。
- 本轮没有测真实飞书公网和真实 LLM 延迟，性能数字不能直接当成生产公网 SLA。
- 本轮是功能深测与短并发，不是长时间 soak；长期稳定性继续参考原传输层报告并在修复后补跑。

综合判断：**Gateway 工具模块具备日常使用能力，但当前应标记为“条件通过”，不应宣称除 cron 外所有工具场景完全正常。**

## 8. 测试产物

主要产物：

- `cache/feishu-tool-perf/tool_gateway_server.py`
- `cache/feishu-tool-perf/tool_gateway_client.py`
- `cache/feishu-tool-perf/run-20260716-tools-03/client-results.json`
- `cache/feishu-tool-perf/run-20260716-tools-03/server-summary.json`
- `cache/feishu-tool-perf/run-20260716-tools-03/platform-messages.json`
- `cache/feishu-tool-perf/run-20260716-tools-04/client-results.json`（正确越界定向复测）
- `cache/feishu-tool-perf/run-20260716-tools-05/client-results.json`（completed Delegate 定向复测）
- `cache/feishu-tool-perf/run-20260716-tools-06/client-results.json`（running/cancelled Delegate 稳定复现）

本轮没有修改 `hermes/` 下任何生产代码。
