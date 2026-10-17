"""数据库引擎与会话管理。

策略：**MySQL 优先，SQLite 兜底**。
- 生产（律所内网）用 MySQL；
- 开发机 / 演示机没有 MySQL 时自动回落 SQLite，保证同一套 ORM 模型可用、流程可跑；
- 实际生效的 URL 通过 :data:`ACTIVE_DB_URL` 暴露，启动日志与 /health 都会打印，
  避免出现「以为连的是 MySQL，其实连的是 SQLite」这类静默失真。
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from backend.app.config import PROJECT_ROOT, settings
from backend.app.db.base import Base

logger = logging.getLogger("legal_agent.db")

ACTIVE_DB_URL: str = ""
ACTIVE_DB_BACKEND: str = "none"
_engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None

# 重命名备份表的后缀前缀。识别"陈旧外键"全靠它 —— 见 ``schema_health``。
LEGACY_PREFIX = "legacy_"

# MySQL 每连接的排序缓冲。**必须显式调大**，理由（本机 2026-09-22 实测）：
#   ``la_task`` 有 5 个 JSON 大列（``result``/``state``/``tool_calls``/``trace``/
#   ``evidence_gap``），实测 ``state`` 平均 15KB、最大 70KB。而列表类接口
#   （``task_service.list`` / ``draft_service`` / ``knowledge_service`` /
#   ``case_service``）都是 ``SELECT 全部列 ... ORDER BY updated_at DESC LIMIT n``。
#   MySQL 的 filesort 会把「排序键以外的列」打包进排序缓冲（packed addon fields），
#   InnoDB 默认 ``sort_buffer_size`` 只有 256KB —— 于是**只攒到 24 行任务**就开始报
#   ``1038 Out of sort memory, consider increasing server sort buffer size``，
#   整个 ``GET /tasks`` 直接 500。SQLite 没有这个概念，所以换到 MySQL 才会暴露。
#   实测：256KB 下 limit=20 也炸；调到 1MB 即可通过 limit=200。
#   取 8MB 留足余量（该缓冲按需分配、只在排序时占用，不是常驻）。
#   不放在 .env 里是因为它不是"部署差异"，而是"这套模型结构在 MySQL 上的硬需求"：
#   配小了就是静默炸列表接口，不该留给人去调。
MYSQL_SORT_BUFFER_BYTES = 8 * 1024 * 1024


@dataclass
class FkDef:
    """一条外键的完整定义（含级联规则），用于「拆下来再装回去」。"""

    table: str
    column: str
    constraint: str
    ref_table: str
    ref_column: str
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"


@dataclass
class SchemaReport:
    """结构对账的结果。``pending_fks`` 必须在 ``create_all`` 之后才能装回去。"""

    renamed: list[tuple[str, str, list[str]]] = field(default_factory=list)
    pending_fks: list[FkDef] = field(default_factory=list)
    restored_fks: list[str] = field(default_factory=list)
    failed_fks: list[tuple[str, str]] = field(default_factory=list)
    orphan_cleared: list[tuple[str, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_fks


def _sqlite_url(url: str) -> str:
    """把相对路径的 SQLite URL 落到项目根目录，避免受 cwd 影响。"""
    if url.startswith("sqlite:///./"):
        rel = url[len("sqlite:///./"):]
        return f"sqlite:///{(PROJECT_ROOT / rel).as_posix()}"
    return url


def _is_memory_sqlite(url: str) -> bool:
    """是不是内存库（``sqlite://`` / ``sqlite:///:memory:``）—— 连接一关库就没了。

    注意 ``sqlite://`` 这种**不写库路径**的写法：它在 ``rstrip("/")`` 之后就变成
    ``"sqlite:"`` 了，别拿 ``"sqlite://"`` 去比（本文件自己的用例就是这么发现写错的，
    见 ``tests/test_sqlite_pool.py``）。查询串要先切掉再比。
    """
    head = url.split("?", 1)[0]
    if ":memory:" in head:
        return True
    return head.rstrip("/") == "sqlite:"


def _sqlite_pool_kwargs(url: str) -> dict[str, Any]:
    """SQLite 降级路径的连接池参数。**内存库与文件库必须分岔，不能一刀切。**

    - **内存库** → ``StaticPool``（只有一条连接，所有会话共享同一个库）。
      这是 SQLAlchemy 官方给单连接内存库的推荐做法。不能换别的：
      默认的 ``SingletonThreadPool`` 会给**每个线程**一条新连接，
      而每条新连接都是一个**全新的空库**，表现为莫名其妙的 "no such table"。
      代价是这个池不支持任何并发，只适用于单线程测试。

    - **文件库** → ``NullPool``：每次 checkout 新开一条连接，归还即关闭，
      **不设连接数上限**。SQLite 开连接就是打开一个本地文件，成本极低，
      不值得为它做池化。

    为什么不分类一律 ``StaticPool``（本机 2026-09-28 实测）：
    那等于**整个进程共用一条 sqlite3 连接**。FastAPI 的同步路由是丢进线程池跑的，
    并发一上来两个线程同时用这条连接，报
    ``sqlite3.InterfaceError: bad parameter or other API misuse``（500）；
    更隐蔽的是 SQLAlchemy 对 StaticPool 的官方警告里那句 ——
    **一个会话的 ROLLBACK 会把另一个会话尚未提交的写入一并回滚**。
    实测表现：并发打 24 个只读接口，除 4 个 500 外还有 **2 个带着合法 token 的
    401「账号不存在或已停用」**（查用户那次的读结果被别的线程冲掉了）。
    这种"读接口偶发 401"极易被误判成鉴权 bug，所以在这里写清楚。

    为什么文件库不能退回默认的 ``QueuePool``（同样是实测）：
    QueuePool 默认 ``pool_size=5 + max_overflow=10``，硬上限 15 条，**超了就阻塞等**。
    而 FastAPI 的请求生命周期是「同步依赖拿一条连接 → 同步路由 → 响应发出后，
    依赖的生成器收尾（release 连接）**也要再占一个工作线程**」。
    于是形成**循环等待**：一部分请求攥着工作线程在等连接，另一部分攥着连接在等
    工作线程（要跑收尾才还得回去），两边谁也动不了 —— 直到 30s 池超时集体 500。
    触发点由 anyio 的工作线程上限（默认 40）和池上限共同决定。实测拐点：
    30 并发全绿、45 并发 14 个 500；日志里还能看到一批请求**耗时 30–32s 才返回 200**
    （排在后面的那批，等前面超时腾出线程才轮到）。
    ``NullPool`` 不设上限，请求永远不会卡在 "等连接" 这一步，循环等待不成立。

    顺带一提：MySQL 分支（``DB_POOL_SIZE=10 + DB_MAX_OVERFLOW=20`` = 30 < 40）
    理论上有同一个循环等待，只是阈值更低、更不容易撞上。真要在生产压到
    40 并发以上，要么把池开到工作线程数之上，要么就别让连接跨线程跳。

    ``check_same_thread=False`` 两种情况都要留：连接可能被交给另一个线程使用或关闭，
    Python 默认的同线程校验会直接拒绝。
    """
    return {"poolclass": StaticPool if _is_memory_sqlite(url) else NullPool}


def _ensure_mysql_database(url: str) -> None:
    """MySQL 库不存在时自动建库（utf8mb4，避免中文整列存坏）。"""
    from sqlalchemy.engine.url import URL, make_url

    u = make_url(url)
    dbname = u.database
    if not dbname:
        return
    # ⚠️ 这里**不能**写 ``u.set(database=None)``：在本机 SQLAlchemy 2.0.48 上实测
    #    它是**空操作** —— ``u.set(database=None).database`` 仍然是 ``legal_agent``。
    #    后果很隐蔽：建库时会去连一个还不存在的库，报
    #    ``1049 Unknown database``，于是自动建库永远失败，而 ``init_engine``
    #    接着连库也失败 → **静默降级 SQLite**（/health 看着在跑，其实没连 MySQL）。
    #    用 URL.create 显式构造"不选库"的服务器连接串（database 缺省即 None）。
    server_url = URL.create(
        drivername=u.drivername,
        username=u.username,
        password=u.password,
        host=u.host,
        port=u.port,
        query=u.query,
    )
    engine = create_engine(server_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            conn.commit()
    finally:
        engine.dispose()


def _try_create_engine(url: str, **kwargs: Any) -> Engine | None:
    try:
        engine = create_engine(url, pool_pre_ping=True, **kwargs)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:  # noqa: BLE001 - 任何连接问题都走降级
        logger.warning("数据库连接失败，准备降级：%s | %s", url.split("@")[-1], exc)
        return None


def init_engine(force_recreate: bool = False) -> Engine:
    """初始化引擎（幂等）。"""
    global _engine, SessionLocal, ACTIVE_DB_URL, ACTIVE_DB_BACKEND

    if _engine is not None and not force_recreate:
        return _engine

    if settings.DB_ENABLED:
        try:
            _ensure_mysql_database(settings.DB_URL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MySQL 建库检查失败：%s", exc)

        engine = _try_create_engine(
            settings.DB_URL,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_recycle=1800,
            # 见 MYSQL_SORT_BUFFER_BYTES 的说明：不加这条，列表接口在 la_task
            # 攒到几十行后就会报 1038 直接 500。
            connect_args={"init_command": f"SET SESSION sort_buffer_size={MYSQL_SORT_BUFFER_BYTES}"},
        )
        if engine is not None:
            _engine, ACTIVE_DB_URL, ACTIVE_DB_BACKEND = engine, settings.DB_URL, "mysql"
            SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
            logger.info("关系库已启用：MySQL")
            return engine

    sqlite_url = _sqlite_url(settings.SQLITE_FALLBACK_URL)
    engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False},
        **_sqlite_pool_kwargs(sqlite_url),
    )
    _engine, ACTIVE_DB_URL, ACTIVE_DB_BACKEND = engine, sqlite_url, "sqlite"
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    logger.warning("关系库已降级为 SQLite：%s", sqlite_url)
    return engine


def reconcile_schema(engine: Engine) -> SchemaReport:
    """结构与模型对账。

    为什么需要这一步：``Base.metadata.create_all`` **只建不存在的表**，
    如果库里已有同名但结构不同的表（例如上一轮版本留下的、或别的分支建的），
    create_all 会静默跳过，之后所有 ORM 写入都会报「Unknown column」，
    而报错位置在业务代码里，极易查错方向。

    处理策略**分两级**，优先做破坏性最小的那一级：

    1. **就地补列**（默认路径）：模型新增了列、库里没有 → ``ALTER TABLE ADD COLUMN``。
       新列一律建成 **NULL 允许**，因为 SQLAlchemy 的 ``default=`` 是 Python 侧默认值，
       只在 INSERT 时生效，并不生成 DDL DEFAULT；对已有行而言该列只能是 NULL。
       （MySQL 的 JSON 列在 8.0.13 以下干脆不支持 DDL 默认值，硬写 DEFAULT 会直接报错。）
       代码侧一律按 ``row.progress or []`` 取值，故 NULL 是安全的。
    2. **重命名备份**（兜底路径）：补列失败（类型无法编译、类型冲突、权限不足）时，
       把同名表重命名为 ``{table}_legacy_{时间戳}``（**不删除、不动数据**），再按新模型重表。

    ⚠️ **第 2 级会连带破坏外键图，必须配套修复。**
    MySQL（8.0 起）在 ``ALTER TABLE ... RENAME TO`` 时，会**自动把其他表里指向它的
    外键改指向改名后的表**。于是 ``la_user`` 改名成 ``la_user_legacy_x`` 之后：

    - ``la_case_material.uploaded_by`` 的外键跟去了 legacy 表；
    - ``create_all`` 建出**全新的空** ``la_user``；
    - 子表的外键**再也没有被改回来** —— 之后往 ``la_case_material`` 插一行，
      MySQL 拿 ``uploaded_by`` 去 legacy 表里找，找不到就报
      ``1452 Cannot add or update a child row``。

    这个报错发生在业务代码里、字段名还是业务字段名（``uploaded_by``），
    看起来像"用户 id 传错了"，而真正的成因是几个月前（或上一次启动时）一次改名。
    所以这里把受影响的外键**收集成 ``pending_fks`` 返回**，
    由 :func:`init_db` 在建表之后调用 :func:`restore_foreign_keys` 装回。

    返回值：:class:`SchemaReport` —— 就地补列成功的表不在 ``renamed`` 中。
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    existing = set(insp.get_table_names())
    report = SchemaReport()

    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing:
            continue
        try:
            cols = {c["name"] for c in insp.get_columns(table_name)}
        except Exception:  # noqa: BLE001
            continue

        missing = sorted({c.name for c in table.columns} - cols)
        if not missing:
            continue

        # ---- 第一级：就地补列
        still_missing = _add_missing_columns(engine, table_name, table, missing)
        if not still_missing:
            logger.info("表 %s 就地补齐缺失列：%s", table_name, ", ".join(missing))
            continue

        # ---- 第二级：重命名备份后重建
        backup = f"{table_name}_{LEGACY_PREFIX}{datetime.now():%Y%m%d%H%M%S}"
        logger.warning(
            "表 %s 无法就地补列（仍缺：%s），重命名备份为 %s 后重建",
            table_name, ", ".join(still_missing), backup,
        )
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE `{table_name}` RENAME TO `{backup}`"))
        report.renamed.append((table_name, backup, still_missing))

    # 改名之后才能看到"外键跟去了哪里"，所以放在循环外统一采集。
    #
    # 这里**不只看本次改名的表**：上一次启动改名留下的陈旧外键，
    # 本次不会产生任何 rename，却依然在污染数据库。所以改成扫全库 ——
    # 凡是指向 ``{当前模型表}_legacy_{时间戳}`` 的外键一律纳入修复队列。
    # 这样修复是幂等的：坏状态修一次就好，重复执行也不会出错。
    stale = _stale_fk_targets(engine)
    if stale:
        mapping = {backup: base for backup, base in stale.items()}
        if report.renamed:
            mapping.update({backup: orig for orig, backup, _ in report.renamed})
        pending: list[FkDef] = []
        seen: set[tuple[str, str]] = set()
        for fk in _capture_fks_to(engine, set(mapping)):
            key = (fk.table, fk.column)
            if key in seen:
                continue
            seen.add(key)
            fk.ref_table = mapping.get(fk.ref_table, fk.ref_table)
            pending.append(fk)

        if pending:
            logger.warning(
                "检测到 %d 条外键指向 legacy 备份表（%s），"
                "将在重建后改指回新表；不修的话这些子表的下一次 INSERT 会报 1452 外键冲突",
                len(pending),
                "、".join(sorted({f"{f.table}.{f.column}" for f in pending})),
            )
            _drop_fks(engine, pending)
            report.pending_fks = pending

    return report


def _stale_fk_targets(engine: Engine) -> dict[str, str]:
    """扫描库里所有 ``*_legacy_*`` 表，返回 ``{legacy表名: 对应的当前模型表名}``。

    只保留"干净版确实存在于当前模型"的那些 —— 历史遗留表（例如某个已删掉的
    设计里的 ``la_case_document``）不在其列，它们的外键不归我们管。
    """
    out: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE :pat"
            ), {"pat": f"%\\_{LEGACY_PREFIX}%"}).all()
    except Exception as exc:  # noqa: BLE001 - 非 MySQL 后端没有 information_schema
        logger.debug("跳过陈旧外键扫描：%s", exc)
        return out

    for (name,) in rows:
        name = str(name)
        idx = name.rfind(f"_{LEGACY_PREFIX}")
        if idx <= 0:
            continue
        base = name[:idx]
        if base in Base.metadata.tables:
            out[name] = base
    return out


# --------------------------------------------------------------------------- 外键修复
def _capture_fks_to(engine: Engine, ref_tables: set[str]) -> list[FkDef]:
    """采集**指向指定表**的外键定义（排除本身就是备份表的那些子表）。

    备份表是冻结的历史快照，它的外键保持原样即可 ——
    去动它反而会破坏"备份可独立回查"这件事。
    """
    if not ref_tables:
        return []
    placeholders = ", ".join(f":t{i}" for i in range(len(ref_tables)))
    sql = text(f"""
        SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.CONSTRAINT_NAME,
               kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME,
               rc.DELETE_RULE, rc.UPDATE_RULE
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
          ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
         AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE kcu.TABLE_SCHEMA = DATABASE()
          AND kcu.REFERENCED_TABLE_NAME IN ({placeholders})
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME
    """)
    params = {f"t{i}": name for i, name in enumerate(sorted(ref_tables))}
    out: list[FkDef] = []
    try:
        with engine.connect() as conn:
            for row in conn.execute(sql, params).all():
                child, column, cname, parent, pcol, del_rule, upd_rule = row
                if LEGACY_PREFIX in str(child):
                    # 子表自己就是备份表 —— 不碰。它是冻结的历史快照，
                    # 动它的外键反而会破坏"备份可独立回查"这件事。
                    continue
                if str(child) not in Base.metadata.tables:
                    # 子表不在当前模型里（上一代设计留下的表）——不归我们管。
                    # 给它加外键既无意义，也可能因列类型不匹配而失败。
                    continue
                out.append(FkDef(
                    table=str(child), column=str(column), constraint=str(cname),
                    ref_table=str(parent), ref_column=str(pcol),
                    on_delete=str(del_rule or "NO ACTION"),
                    on_update=str(upd_rule or "NO ACTION"),
                ))
    except Exception as exc:  # noqa: BLE001 - SQLite 没有 information_schema
        logger.warning("采集外键定义失败（可能是非 MySQL 后端，跳过修复）：%s", exc)
    return out


def _drop_fks(engine: Engine, fks: list[FkDef]) -> None:
    for fk in fks:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE `{fk.table}` DROP FOREIGN KEY `{fk.constraint}`"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("拆除外键 %s.%s 失败：%s", fk.table, fk.constraint, exc)


def restore_foreign_keys(engine: Engine, fks: list[FkDef]) -> SchemaReport:
    """把 :func:`reconcile_schema` 拆下来的外键装回**新表**。

    必须在 ``Base.metadata.create_all`` **之后**调用 —— 新表还不存在时加外键必然失败。

    孤儿行处理：子表里可能留着指向"旧表 id"的值（旧表已改名为备份表），
    而新表是空的。此时直接加外键会被 MySQL 拒绝（它会校验存量行）。
    处理方式是**能置空就置空**（这些都是 ``uploaded_by`` / ``updated_by`` 这类
    可空的可选外键），并把清掉的行数如实报出来；不能置空的就放弃加这条外键并记录失败 ——
    **绝不静默跳过**，否则下次插入又是一样的外键冲突，而修复过程看起来"成功"了。
    """
    report = SchemaReport(pending_fks=list(fks))
    if not fks:
        return report

    for fk in fks:
        cleared = _clear_orphans(engine, fk)
        if cleared is not None:
            report.orphan_cleared.append((f"{fk.table}.{fk.column}", cleared))
            if cleared:
                logger.warning(
                    "外键修复：%s.%s 有 %d 行指向已改名的旧表，已置为 NULL",
                    fk.table, fk.column, cleared,
                )
        ddl = (
            f"ALTER TABLE `{fk.table}` ADD CONSTRAINT `{fk.constraint}` "
            f"FOREIGN KEY (`{fk.column}`) REFERENCES `{fk.ref_table}` (`{fk.ref_column}`) "
            f"ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}"
        )
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            report.restored_fks.append(f"{fk.table}.{fk.column} -> {fk.ref_table}")
            logger.info("外键已改指回新表：%s.%s -> %s", fk.table, fk.column, fk.ref_table)
        except Exception as exc:  # noqa: BLE001
            report.failed_fks.append((f"{fk.table}.{fk.column}", str(exc)))
            logger.error(
                "外键 %s.%s 改指失败（该子表的写入可能继续报 1452）：%s",
                fk.table, fk.column, exc,
            )
    return report


def _clear_orphans(engine: Engine, fk: FkDef) -> int | None:
    """把子表里"指向已改名旧表"的值置空。返回清掉的行数；无法处理返回 ``None``。"""
    try:
        with engine.connect() as conn:
            orphans = conn.execute(text(
                f"SELECT COUNT(*) FROM `{fk.table}` c "
                f"LEFT JOIN `{fk.ref_table}` p ON c.`{fk.column}` = p.`{fk.ref_column}` "
                f"WHERE c.`{fk.column}` IS NOT NULL AND p.`{fk.ref_column}` IS NULL"
            )).scalar_one()
        orphans = int(orphans or 0)
        if not orphans:
            return 0

        nullable = True
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ), {"t": fk.table, "c": fk.column}).first()
            if rows is not None:
                nullable = str(rows[0]).upper() == "YES"
        if not nullable:
            logger.error(
                "外键修复：%s.%s 有 %d 行孤儿数据且该列 NOT NULL，无法置空，"
                "将跳过这条外键（需人工清理后重启）",
                fk.table, fk.column, orphans,
            )
            return None

        with engine.begin() as conn:
            conn.execute(text(
                f"UPDATE `{fk.table}` c "
                f"LEFT JOIN `{fk.ref_table}` p ON c.`{fk.column}` = p.`{fk.ref_column}` "
                f"SET c.`{fk.column}` = NULL "
                f"WHERE c.`{fk.column}` IS NOT NULL AND p.`{fk.ref_column}` IS NULL"
            ))
        return orphans
    except Exception as exc:  # noqa: BLE001
        logger.warning("外键修复：清理 %s.%s 孤儿数据失败：%s", fk.table, fk.column, exc)
        return None


def schema_health() -> dict[str, Any]:
    """结构健康自检：**本项目管的表还有没有外键指向 legacy 备份表**。

    与 :func:`restore_foreign_keys` 的区别：那个是"修"，这个是"查"。
    放在 ``/health`` 里，让"结构已经坏了"能在一眼之内被发现，
    而不是等到某天某个业务接口报 1452 才回头找原因。

    **两类结果分开报**，因为它们对 ``ok`` 的影响完全不同：

    - ``stale_fks``：子表**在当前模型里**，外键却指向备份表 —— 这是本项目的结构
      出了问题，下次插入就会炸，``ok=False``。
    - ``stale_fks_foreign_tables``：子表**不在当前模型里**（库里遗留的上一代设计，
      例如本项目曾经有过的 ``la_case_document``）。它们的外键不归我们管，
      也不影响任何一条业务路径 —— 只作为信息报出来，**不影响 ``ok``**。

    这个区分是必要的：把历史遗留表也算成故障，会让健康检查在这类机器上
    **永远显示红色**，而"永远红"的健康检查等于没有 —— 真出问题时没人会看。
    """
    engine = init_engine()
    out: dict[str, Any] = {
        "stale_fks": [], "stale_fks_foreign_tables": [],
        "legacy_tables": [], "ok": True,
    }
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME
                FROM information_schema.KEY_COLUMN_USAGE kcu
                WHERE kcu.TABLE_SCHEMA = DATABASE()
                  AND kcu.REFERENCED_TABLE_NAME LIKE :pat
                ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
            """), {"pat": f"%\\_{LEGACY_PREFIX}%"}).all()
            known = set(Base.metadata.tables)
            for t, c, r in rows:
                t = str(t)
                if LEGACY_PREFIX in t:
                    continue      # 子表自己就是备份表，跳过
                item = {"table": t, "column": str(c), "refs_legacy": str(r)}
                (out["stale_fks"] if t in known
                 else out["stale_fks_foreign_tables"]).append(item)

            out["legacy_tables"] = [str(x[0]) for x in conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE :pat"
            ), {"pat": f"%\\_{LEGACY_PREFIX}%"}).all()]
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(exc)
        return out

    out["ok"] = not out["stale_fks"]
    if out["stale_fks"]:
        out["hint"] = ("本项目的子表存在指向 legacy 备份表的外键，"
                       "调用 init_db() 会自动改指回当前表")
    return out


def _add_missing_columns(
    engine: Engine, table_name: str, table: Any, missing: list[str]
) -> list[str]:
    """逐列 ALTER TABLE ADD COLUMN，返回**仍然缺失**的列名。"""
    failed: list[str] = []
    for name in missing:
        col = table.columns[name]
        try:
            ddl_type = col.type.compile(dialect=engine.dialect)
        except Exception as exc:  # noqa: BLE001
            logger.warning("列 %s.%s 类型无法编译成 DDL：%s", table_name, name, exc)
            failed.append(name)
            continue
        if not ddl_type:
            failed.append(name)
            continue

        # 允许 NULL：见函数 docstring 的说明
        clause = f"`{name}` {ddl_type} NULL"
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN {clause}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("为 %s 补列 %s 失败：%s", table_name, name, exc)
            failed.append(name)

    if not failed:
        # 已有行该列为 NULL，明确提示，避免"以为有新列就有数据"
        logger.info("表 %s 补列完成；已有数据行的新列值为 NULL", table_name)
    return failed


def init_db() -> SchemaReport:
    """建表（幂等，含结构对账与外键修复）。

    **三步的顺序不能调换**：

    1. ``reconcile_schema`` —— 补列 / 改名备份，并**拆下**被改名连带影响的外键；
    2. ``create_all`` —— 按新模型建出缺失的表（含被改名后重建的那张）；
    3. ``restore_foreign_keys`` —— 把第 1 步拆下的外键装回**新表**。

    第 2 步放在第 3 步之前是硬约束：新表还不存在时加外键必然失败。
    这个顺序此前是错的（只有 1、2 两步，没有第 3 步），后果是子表外键永远指向
    改名后的备份表，插入时报 1452，而报错字段是业务字段名，很容易查错方向。
    """
    from backend.app.db import models  # noqa: F401  确保模型已注册

    engine = init_engine()
    report = reconcile_schema(engine)
    Base.metadata.create_all(bind=engine)
    if report.pending_fks:
        repair = restore_foreign_keys(engine, report.pending_fks)
        report.restored_fks = repair.restored_fks
        report.failed_fks = repair.failed_fks
        report.orphan_cleared = repair.orphan_cleared
    logger.info("建表完成，共 %d 张表；结构对账：改名 %d 张，修复外键 %d 条%s",
                len(Base.metadata.tables), len(report.renamed), len(report.restored_fks),
                f"，失败 {len(report.failed_fks)} 条" if report.failed_fks else "")
    return report


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """事务上下文。"""
    if SessionLocal is None:
        init_engine()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入。"""
    with session_scope() as session:
        yield session


def db_status() -> dict[str, Any]:
    """当前生效的关系库后端。

    注意：模块级 ``ACTIVE_DB_URL`` 只有在 ``init_engine()`` 之后才有值，
    所以这里先做一次幂等初始化 —— 否则健康检查在启动早期会返回
    ``backend="none"``，看起来像"数据库没启用"，实际只是还没初始化。
    """
    if _engine is None:
        init_engine()
    return {"backend": ACTIVE_DB_BACKEND, "url": ACTIVE_DB_URL.split("@")[-1] if ACTIVE_DB_URL else ""}


def dispose_engine() -> None:
    """释放连接池（优雅关闭时调用）。

    需要它的理由：``engine.dispose()`` 关闭池里所有连接。不调用的话，
    进程退出时 MySQL 侧会看到一批连接直接断掉，在 ``SHOW PROCESSLIST`` 里
    留下 ``Aborted_clients`` 计数 —— 内网 DBA 看到这个数字会来问是不是出了故障。
    另外 SQLite 的池会**一直攥着空闲连接**（文件库是 ``NullPool`` 交出去、
    内存库是 ``StaticPool`` 那一条），不显式释放会让 ``tmp`` 库文件删不掉
    （Windows 上表现为 ``PermissionError``）。

    **幂等且不抛错**：关闭阶段的异常不应该阻止进程退出，也不应该把
    "优雅关闭失败"报成启动问题。
    """
    global _engine, SessionLocal, ACTIVE_DB_BACKEND, ACTIVE_DB_URL
    if _engine is None:
        return
    try:
        _engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("释放数据库连接池时出错（忽略）：%s", exc)
    finally:
        _engine = None
        SessionLocal = None
        ACTIVE_DB_BACKEND = "none"
        ACTIVE_DB_URL = ""

