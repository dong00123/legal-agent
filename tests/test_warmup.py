"""检索链路预热的守卫测试。

**为什么单独一个文件。** 预热横跨两个地方：启动时的加载（``main.run_warmup``）
与对外暴露（``/health.warmup``）。前者属于生命周期、后者属于接口层，
放进哪边都别扭，合在一处才讲得清它要守什么。

**这里刻意不跑 lifespan。** 真的跑一次要 17 秒（加载 BGE 权重），
而本文件要验的三件事都跟"有没有真的加载"无关：
探针能不能读到预热状态、没预热时会不会假报、免登录探针会不会泄账号。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app


@pytest.fixture()
def no_startup_check():
    """临时抹掉启动自检记录，模拟"还没预热过"的状态。

    **用完必须放回去**：``app`` 是全局单例，别的测试文件也在用它。
    抹掉不还等于给后面的用例埋一个"为什么 /health 说没预热"的坑。
    """
    had = hasattr(app.state, "startup_check")
    saved = getattr(app.state, "startup_check", None)
    if had:
        del app.state.startup_check
    try:
        yield
    finally:
        if had:
            app.state.startup_check = saved


def test_warmup_is_on_by_default() -> None:
    """预热默认开着。

    关掉它的代价是第一个用户等十几秒 —— 那必须是有人**主动**选择，
    不能是默认值悄悄变成 False 了没人发现。
    """
    assert hasattr(settings, "WARMUP_ENABLED"), "配置缺 WARMUP_ENABLED 字段"
    assert settings.WARMUP_ENABLED is True


def test_health_carries_warmup_even_in_cheap_mode(no_startup_check) -> None:
    """``full=false`` 也必须带 warmup。

    **这是容器探针实际打的那一档。** 预热状态放在 full 里等于没有 ——
    探针永远只看得到"进程活着"，看不到"接下来要等 17 秒"。
    """
    client = TestClient(app)
    body = client.get("/health?full=false").json()
    assert "warmup" in body, "/health 缺 warmup 字段"


def test_unwarmed_health_says_unknown_not_ok(no_startup_check) -> None:
    """没有预热记录时，必须如实说"不知道"，不能默认 ok。

    默认成 ``ok: true`` 会让运维以为一切都预热好了，
    而实际是"压根没做" —— 这两件事在同一个字段上长得一样，
    唯一的区别就是这个断言。
    """
    client = TestClient(app)
    warm = client.get("/health?full=false").json()["warmup"]
    assert warm.get("ok") is None
    assert warm.get("note"), "只说 ok=null 不给解释，看的人不知道发生了什么"


def test_health_never_carries_full_startup_check() -> None:
    """``/health`` 是**免登录**的，不能挂完整的启动自检结果。

    那份结果里有 ``seed.created``（初始账号名）、未映射的 ``.env`` 键、
    结构对账明细 —— 挂上去等于给匿名访问者一份用户名枚举表。
    要看完整自检请走需要管理员的 ``/meta/config``。
    """
    client = TestClient(app)
    body = client.get("/health?full=true").json()
    assert "startup_check" not in body, "/health 不能带完整启动自检结果"
    assert "seed" not in body, "/health 不能带初始账号信息"
