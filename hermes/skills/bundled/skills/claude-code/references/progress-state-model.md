# round 与状态模型

受管状态由 Controller/Runtime 产生，Agent 只读取 Tool result，不自行把日志文本写回状态机。Process 生命周期与任务 round 是两层不同事实。

## Snapshot state

| 状态 | 含义 |
| --- | --- |
| `STARTING` | 受管 Session 正在启动，尚未确认可接收任务。 |
| `READY` | 活跃 Session 已有可信可输入证据，且没有当前 ActionRequired。 |
| `WORKING` | 当前 round 已提交，出现真实非 echo 工作活动。 |
| `WAITING_INPUT` | Claude Code 正在等待澄清或其他用户输入。 |
| `WAITING_APPROVAL` | Claude Code 正在等待权限、认证或其他确认。 |
| `COMPLETED` | 已有完成证据并收敛为完成。 |
| `FAILED` | 已有失败证据或失败进程事实并收敛。 |
| `INTERRUPTED` | 已确认的协作式中断已按真实证据收敛。 |
| `LOST` | 进程/所有权事实无法可靠确认。 |
| `UNKNOWN` | 现有输出不足以可靠分类；不是完成或可输入证据。 |

`STALLED` 不在 Snapshot state 枚举中。它是 Controller 的 ActionRequired/outcome，表示有界观察中没有足够的新活动；它不等于进程已退出、任务失败、READY 或可以自动发送新指令。

## round 与 process

| Tool 字段 | 语义 |
| --- | --- |
| `process_active` | 仅表示受管底层 process 仍处于 active ProcessStatus。 |
| `round_id` | 当前或已保存 round 的不透明身份。 |
| `round_terminal` | 当前返回的 round 已经 `COMPLETED`、`FAILED` 或 `INTERRUPTED` 等终态。 |
| `outcome` | 本次 Controller 调用的有界结果，例如 `running`、`action_required`、`terminal`、`interrupt_pending` 或 `stalled`。 |

一个 Session 可在相同 `process_id` 上顺序拥有多个 round。只有最新终态 round、没有活动 round、没有未消费 ActionRequired 并且 Session READY 时，`send_instruction` 才会创建不同的新 `round_id`。不得使用旧 round、未知 round 或活动 round 追加普通输入。

## 事件与活动

事件使用 [output-observation.md](output-observation.md) 所列的当前生产枚举。`PROGRESS`、新的相关 `OUTPUT`、真实完成/失败信号、进程状态变化或新 ActionRequired 可以更新理解；输入 echo、ANSI 重绘、spinner、`effort` UI、孤立 `$`、重复错误和空读不能单独更新状态或推断成功。

`raw_cursor` 始终属于 Controller/Runtime；Agent 不保存第二套 cursor、完整日志、输入历史、凭据、Handle 或 Claude 私有 session 文件。
