"""全局 schema 版本管理、建表与迁移总调度。

本模块是持久化领域的 schema 入口,负责:

* 定义当前代码支持的 ``LATEST_SCHEMA_VERSION``;
* 读取 / 设置数据库的 ``schema_version``;
* 在全新库上按固定领域顺序创建全部表与触发器;
* 在旧库上按版本号顺序执行 migration,失败整体回滚。

依赖方向:

    schema -> database (基础能力)
    schema -> schemas/* (各领域 DDL)
    schema -> migrations/* (各版本迁移函数)

领域持久化模块 (gateway、delivery、approval、cron、feishu、
orchestration) 不得反向依赖本模块,以避免循环。
``LATEST_SCHEMA_VERSION`` 是少数允许被领域模块引用的常量,因为运行期
健康检查需要用它确认数据库已就绪。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .database import DBError, _apply_pragmas
from .migrations.approval import _migrate_v13_to_v14, _migrate_v28_to_v29, _migrate_v29_to_v30, _migrate_v30_to_v31
from .migrations.background_review import _migrate_v32_to_v33, _migrate_v33_to_v34, _migrate_v34_to_v35
from .migrations.backend_control import _migrate_v37_to_v38
from .migrations.core import _migrate_v1_to_v2, _migrate_v26_to_v27
from .migrations.cron import _migrate_v18_to_v19, _migrate_v19_to_v20, _migrate_v20_to_v21, _migrate_v24_to_v25
from .migrations.delivery import _migrate_v15_to_v16, _migrate_v16_to_v17, _migrate_v21_to_v22
from .migrations.feishu import _migrate_v9_to_v10, _migrate_v10_to_v11, _migrate_v17_to_v18
from .migrations.gateway import _migrate_v2_to_v3, _migrate_v3_to_v4, _migrate_v4_to_v5, _migrate_v5_to_v6, _migrate_v6_to_v7, _migrate_v7_to_v8, _migrate_v8_to_v9, _migrate_v11_to_v12, _migrate_v12_to_v13, _migrate_v14_to_v15
from .migrations.mixed import _migrate_v22_to_v23, _migrate_v23_to_v24
from .migrations.observation import _migrate_v35_to_v36, _migrate_v40_to_v41
from .migrations.orchestration import _migrate_v38_to_v39, _migrate_v39_to_v40
from .migrations.runtime import _migrate_v36_to_v37
from .migrations.tool_execution import _migrate_v25_to_v26, _migrate_v27_to_v28, _migrate_v31_to_v32
from .schemas import (
    approval,
    background_review,
    backend_control,
    core,
    cron,
    delivery,
    feishu,
    gateway,
    observation,
    orchestration,
    runtime,
    tool_execution,
)

# 当前最新 schema 版本。每次升级表结构或持久化语义时 +1,并在 _migrate 里加对应分支。
# 为什么需要 schema version:让 db 启动时知道结构处于哪个版本,需要的话
# 按顺序执行 migration,避免依赖用户手动删库升级。
LATEST_SCHEMA_VERSION = 41


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema version。

    返回 0 表示全新库(还没任何表);返回 1 表示老库(v1 时代还没引入
    schema_version 表);返回 >=2 表示已经过 migration。
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        # schema_version 表不存在 -- 可能是全新库,也可能是 v1 老库
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        if cur.fetchone() is None:
            return 0  # 全新库
        return 1  # v1 老库(只有 sessions/messages,无 schema_version 表)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    # schema_version 只保留一行,避免未来版本号堆叠导致判断含糊。
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))


def _create_latest_schema(conn: sqlite3.Connection) -> None:
    """按固定领域顺序创建全新数据库所需的完整 Schema。

    顺序与历史 migration 累积结果保持一致:Gateway 基础表 -> Gateway
    ownership/lease -> Approval -> Delivery -> Gateway fencing triggers
    -> Feishu Inbox / pending attachment -> Cron -> Observation / Runtime
    -> Backend Control -> Orchestration。各领域通过公开的
    ``create_schema`` 入口创建自身表结构,Gateway 的 fencing triggers
    因顺序依赖单独由 ``create_fencing_triggers`` 暴露。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    core.create_schema(conn)
    background_review.create_schema(conn)
    gateway.create_schema(conn)
    approval.create_schema(conn)
    delivery.create_schema(conn)
    gateway.create_fencing_triggers(conn)
    feishu.create_schema(conn)
    cron.create_schema(conn)
    tool_execution.create_schema(conn)
    observation.create_schema(conn)
    runtime.create_schema(conn)
    backend_control.create_schema(conn)
    orchestration.create_schema(conn)


def _migrate(conn: sqlite3.Connection, current: int) -> int:
    """按版本号顺序执行 migration,返回最新版本。

    老库 v1 -> v2 会重建 sessions/messages,让外键 / NOT NULL
    约束对既有数据库也生效。v2 -> v3 新增 Gateway 当前会话映射,
    v3 -> v4 新增 Gateway 待处理消息队列,v4 -> v5 新增出站回复队列,
    v5 -> v6 关联最终回答投递状态,v6 -> v7 区分部分取消,
    v7 -> v8 增加原始平台消息归属索引,v8 -> v9 增加 Gateway 运行租约,
    v9 -> v10 正式接管 Feishu Inbox schema,v10 -> v11 持久化 Inbox
    route_key,v11 -> v12 增加运行租约 epoch 与 Outbox claim fencing,
    v12 -> v13 保存每条 route 的历史 conversation 归属,v13 -> v14
    增加与 Tool Result 绑定的远程审批请求,v14 -> v15 增加持久化审批恢复,
    v15 -> v16 增加出站文件任务与 gateway_send_file 审批类型,
    v16 -> v17 增加文件任务到 Outbox 的持久关联,v17 -> v18
    增加等待下一条用户指令的飞书附件记录,v18 -> v19 将 Cron 正式状态
    迁移到 SQLite 的任务定义与运行记录表,v19 -> v20 增加每任务
    AgentLoop 轮数上限,v20 -> v21 为 Gateway Cron 增加 fenced claim。
    旧数据不满足新约束时拒绝迁移。
    v29 -> v30 扩展 Gateway 审批可持久化的浏览器高风险工具，v30 -> v31
    移除审批表中的具体工具名称枚举。v31 -> v32 增加等待人工批准的
    工具执行 Journal 状态，避免审批占位结果被当作执行失败。v35 -> v36
    增加安全 Observation 表，v36 -> v37 增加 Runtime 当前快照表，
    v37 -> v38 增加 Gateway Supervisor 控制请求与进程绑定，v38 -> v39
    增加持久化 Workflow、Task、Dependency 与 Task Run 事实表，v39 -> v40
    按正式终结的 Run 历史重算 Task 已消耗的执行预算。
    """
    if current < 1:
        # 极少见:有 schema_version 表但版本 < 1,补基础表
        _create_latest_schema(conn)
        current = LATEST_SCHEMA_VERSION

    if current < 2:
        # DDL 不依赖 sqlite3.Connection 的 with 协议,这里显式开事务,
        # 避免重建表中途失败时留下半迁移状态。
        conn.execute("BEGIN")
        try:
            _migrate_v1_to_v2(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_order "
                "ON messages(session_id, id)"
            )
            _set_schema_version(conn, 2)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 2

    if current < 3:
        conn.execute("BEGIN")
        try:
            _migrate_v2_to_v3(conn)
            _set_schema_version(conn, 3)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 3

    if current < 4:
        conn.execute("BEGIN")
        try:
            _migrate_v3_to_v4(conn)
            _set_schema_version(conn, 4)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 4

    if current < 5:
        conn.execute("BEGIN")
        try:
            _migrate_v4_to_v5(conn)
            _set_schema_version(conn, 5)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 5

    if current < 6:
        conn.execute("BEGIN")
        try:
            _migrate_v5_to_v6(conn)
            _set_schema_version(conn, 6)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 6

    if current < 7:
        conn.execute("BEGIN")
        try:
            _migrate_v6_to_v7(conn)
            _set_schema_version(conn, 7)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 7

    if current < 8:
        conn.execute("BEGIN")
        try:
            _migrate_v7_to_v8(conn)
            _set_schema_version(conn, 8)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 8

    if current < 9:
        conn.execute("BEGIN")
        try:
            _migrate_v8_to_v9(conn)
            _set_schema_version(conn, 9)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 9

    if current < 10:
        conn.execute("BEGIN")
        try:
            _migrate_v9_to_v10(conn)
            _set_schema_version(conn, 10)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 10

    if current < 11:
        conn.execute("BEGIN")
        try:
            _migrate_v10_to_v11(conn)
            _set_schema_version(conn, 11)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 11

    if current < 12:
        conn.execute("BEGIN")
        try:
            _migrate_v11_to_v12(conn)
            _set_schema_version(conn, 12)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 12

    if current < 13:
        conn.execute("BEGIN")
        try:
            _migrate_v12_to_v13(conn)
            _set_schema_version(conn, 13)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 13

    if current < 14:
        conn.execute("BEGIN")
        try:
            _migrate_v13_to_v14(conn)
            _set_schema_version(conn, 14)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 14

    if current < 15:
        conn.execute("BEGIN")
        try:
            _migrate_v14_to_v15(conn)
            _set_schema_version(conn, 15)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 15

    if current < 16:
        conn.execute("BEGIN")
        try:
            _migrate_v15_to_v16(conn)
            _set_schema_version(conn, 16)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 16

    if current < 17:
        conn.execute("BEGIN")
        try:
            _migrate_v16_to_v17(conn)
            _set_schema_version(conn, 17)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 17

    if current < 18:
        conn.execute("BEGIN")
        try:
            _migrate_v17_to_v18(conn)
            _set_schema_version(conn, 18)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 18

    if current < 19:
        conn.execute("BEGIN")
        try:
            _migrate_v18_to_v19(conn)
            _set_schema_version(conn, 19)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 19

    if current < 20:
        conn.execute("BEGIN")
        try:
            _migrate_v19_to_v20(conn)
            _set_schema_version(conn, 20)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 20

    if current < 21:
        conn.execute("BEGIN")
        try:
            _migrate_v20_to_v21(conn)
            _set_schema_version(conn, 21)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 21

    if current < 22:
        conn.execute("BEGIN")
        try:
            _migrate_v21_to_v22(conn)
            _set_schema_version(conn, 22)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 22

    if current < 23:
        conn.execute("BEGIN")
        try:
            _migrate_v22_to_v23(conn)
            _set_schema_version(conn, 23)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 23

    if current < 24:
        conn.execute("BEGIN")
        try:
            _migrate_v23_to_v24(conn)
            _set_schema_version(conn, 24)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 24

    if current < 25:
        conn.execute("BEGIN")
        try:
            _migrate_v24_to_v25(conn)
            _set_schema_version(conn, 25)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 25

    if current < 26:
        conn.execute("BEGIN")
        try:
            _migrate_v25_to_v26(conn)
            _set_schema_version(conn, 26)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 26

    if current < 27:
        conn.execute("BEGIN")
        try:
            _migrate_v26_to_v27(conn)
            _set_schema_version(conn, 27)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 27

    if current < 28:
        conn.execute("BEGIN")
        try:
            _migrate_v27_to_v28(conn)
            _set_schema_version(conn, 28)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 28

    if current < 29:
        conn.execute("BEGIN")
        try:
            _migrate_v28_to_v29(conn)
            _set_schema_version(conn, 29)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 29

    if current < 30:
        conn.execute("BEGIN")
        try:
            _migrate_v29_to_v30(conn)
            _set_schema_version(conn, 30)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 30

    if current < 31:
        conn.execute("BEGIN")
        try:
            _migrate_v30_to_v31(conn)
            _set_schema_version(conn, 31)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 31

    if current < 32:
        conn.execute("BEGIN")
        try:
            _migrate_v31_to_v32(conn)
            _set_schema_version(conn, 32)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 32

    if current < 33:
        conn.execute("BEGIN")
        try:
            _migrate_v32_to_v33(conn)
            _set_schema_version(conn, 33)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 33

    if current < 34:
        conn.execute("BEGIN")
        try:
            _migrate_v33_to_v34(conn)
            _set_schema_version(conn, 34)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 34

    if current < 35:
        conn.execute("BEGIN")
        try:
            _migrate_v34_to_v35(conn)
            _set_schema_version(conn, 35)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 35

    if current < 36:
        conn.execute("BEGIN")
        try:
            _migrate_v35_to_v36(conn)
            _set_schema_version(conn, 36)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 36

    if current < 37:
        conn.execute("BEGIN")
        try:
            _migrate_v36_to_v37(conn)
            _set_schema_version(conn, 37)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 37

    if current < 38:
        conn.execute("BEGIN")
        try:
            _migrate_v37_to_v38(conn)
            _set_schema_version(conn, 38)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 38

    if current < 39:
        conn.execute("BEGIN")
        try:
            _migrate_v38_to_v39(conn)
            _set_schema_version(conn, 39)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 39

    if current < 40:
        conn.execute("BEGIN")
        try:
            _migrate_v39_to_v40(conn)
            _set_schema_version(conn, 40)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 40

    if current < 41:
        conn.execute("BEGIN")
        try:
            _migrate_v40_to_v41(conn)
            _set_schema_version(conn, 41)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 41

    return current


def init_db(db_path: str) -> sqlite3.Connection:
    """打开唯一 SQLite 数据库,并在同一连接上完成建库或迁移。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _apply_pragmas(conn)
        current = _get_schema_version(conn)
        if current > LATEST_SCHEMA_VERSION:
            raise DBError(
                "db schema version is newer than this code supports: "
                f"{current} > {LATEST_SCHEMA_VERSION}"
            )
        if current == 0:
            _create_latest_schema(conn)
            _set_schema_version(conn, LATEST_SCHEMA_VERSION)
        elif current < LATEST_SCHEMA_VERSION:
            _migrate(conn, current)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise
