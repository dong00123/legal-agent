"""SQLite 降级路径的连接池守卫。

盯的是一类**只在并发下才现形、而且报错方向全是错的**的缺陷：池选错了，
接口在单发时全绿，一上并发就 500 —— 而 500 的堆栈指向的是业务代码
（``get_current_user`` 之类的第一处 DB 访问），看起来像鉴权或查询写错了。

本机 2026-09-28 实测过两种错法，两种都写成了断言：

1. **一律 StaticPool**：整个进程共用一条 sqlite3 连接 → 并发下报
   ``sqlite3.InterfaceError: bad parameter or other API misuse``；
   更坏的是带着**合法 token 也能偶发 401「账号不存在或已停用」**
   （查用户那次读的结果被别的线程冲掉了）。
2. **文件库用默认 QueuePool**：硬上限 15 条连接，超了阻塞等 30s。
   而 FastAPI 归还连接**还要再占一个工作线程**（anyio 默认 40 个），
   于是形成循环等待 —— 实测 30 并发全绿、45 并发 14 个 500，
   日志里还有一批请求耗时 30–32s 才返回 200。

所以这里锁定的判据是：**文件库必须用不设上限的 NullPool，内存库必须用 StaticPool**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool, StaticPool

from backend.app.db import session as db_session
from backend.app.db.session import _is_memory_sqlite, _sqlite_pool_kwargs

CONNECT_ARGS = {"check_same_thread": False}


def _engine_for(url: str):
    return create_engine(url, connect_args=CONNECT_ARGS, **_sqlite_pool_kwargs(url))


# ============================================================ 1. 池型选择
@pytest.mark.parametrize("url", [
    "sqlite:///C:/tmp/whatever.db",
    "sqlite:///./data/legal_agent.db",
    "sqlite:////var/lib/legal_agent/x.db",
])
def test_file_sqlite_uses_nullpool(url: str) -> None:
    """文件库必须 NullPool —— 不设连接数上限，请求就不会卡在"等连接"上。"""
    kwargs = _sqlite_pool_kwargs(url)
    assert kwargs.get("poolclass") is NullPool, (
        f"文件库 {url} 拿到的是 {kwargs.get('poolclass')}，不是 NullPool。"
        "退回 QueuePool 会重新引入 15 条上限的循环等待（见本文件 docstring）。"
        "StaticPool 更不行：整个进程共用一条连接，并发下会 500 和偶发 401。"
    )


@pytest.mark.parametrize("url", [
    "sqlite://",
    "sqlite:///:memory:",
    "sqlite:///:memory:?cache=shared",
])
def test_memory_sqlite_uses_staticpool(url: str) -> None:
    """内存库必须 StaticPool —— 换别的池，每条连接都是一个**全新的空库**。"""
    assert _is_memory_sqlite(url), f"{url} 应该被判定成内存库"
    kwargs = _sqlite_pool_kwargs(url)
    assert kwargs.get("poolclass") is StaticPool, (
        f"内存库 {url} 拿到的是 {kwargs.get('poolclass')}。"
        "默认的 SingletonThreadPool 会给每个线程一条新连接，"
        "而每条新连接都是空库，表现为莫名其妙的 'no such table'。"
    )


def test_memory_detection_does_not_swallow_file_urls() -> None:
    """``:memory:`` 的判定别把文件路径误伤进来（路径里带 memory 字样很常见）。"""
    for url in ["sqlite:///C:/tmp/memory.db", "sqlite:///./data/in-memory-test.db"]:
        assert not _is_memory_sqlite(url), f"{url} 是文件库，被误判成内存库了"


# ============================================================ 2. 真引擎的接线
def test_live_engine_pool_matches_backend() -> None:
    """验证的是 ``init_engine()`` **真实建出来的那个引擎**，不是上面的辅助函数。

    只测 ``_sqlite_pool_kwargs`` 的话，有人在 ``create_engine`` 那行又显式传一个
    ``poolclass``，测试照样绿 —— 这里堵的就是那个缝。
    """
    engine = db_session.init_engine()
    if db_session.ACTIVE_DB_BACKEND != "sqlite":
        pytest.skip(f"当前跑在 {db_session.ACTIVE_DB_BACKEND} 上，本用例只管 SQLite 降级路径")

    pool_name = type(engine.pool).__name__
    if _is_memory_sqlite(db_session.ACTIVE_DB_URL):
        assert pool_name == "StaticPool", f"内存库的池是 {pool_name}"
    else:
        assert pool_name == "NullPool", (
            f"文件库 {db_session.ACTIVE_DB_URL} 的池是 {pool_name}，应该是 NullPool。"
            "QueuePool 会在并发超过 pool_size+max_overflow 时阻塞 30s 再 500。"
        )


def test_engine_connect_args_allow_cross_thread_use() -> None:
    """``check_same_thread`` 必须关掉：连接可能被交给另一个线程使用或关闭。"""
    engine = db_session.init_engine()
    if db_session.ACTIVE_DB_BACKEND != "sqlite":
        pytest.skip(f"当前跑在 {db_session.ACTIVE_DB_BACKEND} 上")

    args = engine.dialect.create_connect_args(engine.url)[1]
    assert args.get("check_same_thread") is False, (
        f"connect_args 里 check_same_thread={args.get('check_same_thread')!r}，"
        "没关掉的话跨线程复用连接会被 sqlite3 直接拒绝"
    )


# ============================================================ 3. 行为：不能再有 15 条上限
def test_no_connection_cap_on_file_sqlite(tmp_path) -> None:
    """同时占住 25 条连接 —— 有上限的池（QueuePool 默认 15）会在这里阻塞。

    这是把"45 并发 14 个 500"那条现象压缩成一个不依赖起服务的断言。
    """
    db_file = tmp_path / "cap.db"
    engine = _engine_for(f"sqlite:///{db_file.as_posix()}")

    conns = []
    try:
        for i in range(25):
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conns.append(conn)
        assert len(conns) == 25, "同时占住的连接数不对，断言写错了"
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"同时占 25 条连接时失败了：{type(exc).__name__}: {exc}\n"
            "如果这是 QueuePool 的 connection timed out，说明池又被设成有上限的了"
        ) from exc
    finally:
        for conn in conns:
            conn.close()
        engine.dispose()
