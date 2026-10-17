"""测试公共夹具（pytest 自动加载，无需 import）。

⚠ 为什么必须在这里隔离复审模型：
开发机的 ``.env`` 里 ``LLM_REVIEW_*`` 是**真实开启**的（双模型交叉复核）。
不隔离的话，每个走到 ``ReviewNode`` 的用例都会真的去打外部模型 —— 两个后果：
1. 慢：一次复审是几十秒的网络往返，全量用例会跑到超时；
2. 假红：第二模型随时可能把某条风险降级/删除/补漏，于是
   ``test_review_dedupe.py`` 里"单轮扫描条数 == 清单条数"这种断言
   就跟着模型的自由裁量一起飘。

这里默认把 ``review_provider`` 打成 ``None`` =「没配第二模型」，
``review_node._second_pass`` 收到 None 就原样返回初审清单（只登记一条
证据缺口），用例回到**单轮初审**这一确定行为。
需要测复审本身的用例（``test_review_second_pass.py``）在函数体内自行
``monkeypatch.setattr`` 覆盖，函数级 monkeypatch 晚于本夹具设置、优先生效。

抽查同理：``REVIEW_SPOT_CHECK_RATE`` 默认打成 0，免得"这条用例有没有被抽中"
变成掷骰子。要测抽查比例的用例自己覆盖。
"""
from __future__ import annotations

import pytest

from backend.app.config import settings
from backend.llm import client as llm_client_mod


@pytest.fixture(autouse=True)
def _isolate_review_second_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭复审模型与人工抽查，让用例行为与模型/随机数解耦。"""
    monkeypatch.setattr(llm_client_mod, "review_provider", lambda: None)
    monkeypatch.setattr(settings, "REVIEW_SPOT_CHECK_RATE", 0.0)
