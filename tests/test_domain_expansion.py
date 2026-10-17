"""domain 体系扩展验收测试：18 个 data\\raw 领域目录全覆盖。

覆盖验收清单（domain.py 规则识别 + 检索过滤 + 目录兜底 + 枚举一致性）：
- 识别：海商法/民用航空→transport、治安管理处罚法→admin、渔业法→agriculture、
  对外贸易法→trade、食品安全法→safety、知识产权保护条例→ip、
  生态环境法典→env、国防动员法→military、宪法→constitution，
  以及刑法→criminal、仲裁法→judicature、公司法→economy、
  社会救助法→social_security、立法程序→legislation、
  国家通用语言文字法→culture、民族团结进步促进法→ethnic；
- 过滤：按领域滤除跨领域错配，source_path 目录兜底归位；
- 一致性：DOMAINS 与 agents.state.LegalDomain 字面量全集一致。
"""

from __future__ import annotations

from typing import get_args

import pytest

from backend.agents.state import LegalDomain
from backend.rag.domain import (
    DOMAINS,
    filter_statutes_by_domain,
    infer_domain,
)

# --------------------------------------------------------------------- 枚举一致性

def test_domains_cover_all_raw_dirs():
    """DOMAINS 必须覆盖 18 个 raw 目录对应的全部领域（+ civil 兜底 + unknown）。"""
    expected = {
        "labor", "tax", "env", "civil", "criminal", "admin", "constitution", "ip",
        "transport", "military", "agriculture", "trade", "safety", "culture",
        "ethnic", "social_security", "legislation", "judicature", "economy",
        "unknown",
    }
    assert set(DOMAINS) == expected


def test_domains_sync_with_legal_domain():
    """state.LegalDomain 与 domain.DOMAINS 必须完全一致。"""
    assert set(get_args(LegalDomain)) == set(DOMAINS)


# --------------------------------------------------------------------- 领域识别

@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("《中华人民共和国民用航空法》对民航主管部门的职责是如何规定的？", "transport"),
        ("海商法对承运人赔偿责任的规定", "transport"),
        ("治安管理处罚法规定行政拘留的条件是什么？", "admin"),
        ("渔业法规定捕捞许可证如何申领？", "agriculture"),
        ("对外贸易法规定进出口配额制度", "trade"),
        ("食品安全法规定食品召回制度", "safety"),
        ("知识产权保护条例对商业秘密的保护范围", "ip"),
        ("生态环境法典规定排污许可制度", "env"),
        ("国防动员法规定动员程序", "military"),
        ("宪法规定公民基本权利", "constitution"),
        ("刑法规定盗窃罪的构成要件", "criminal"),
        ("仲裁法规定仲裁协议的效力", "judicature"),
        ("公司法规定股东的出资义务", "economy"),
        ("社会救助法规定低保的申请条件", "social_security"),
        ("立法程序如何规定法律的修改？", "legislation"),
        ("国家通用语言文字法规定普通话推广", "culture"),
        ("民族团结进步促进法的主要内容", "ethnic"),
    ],
)
def test_infer_domain_new_domains(query: str, expected: str):
    assert infer_domain(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("公司违法解除劳动合同，经济补偿怎么算？", "labor"),
        ("虚开增值税专用发票的处罚", "tax"),
        ("工厂排污被环保部门处罚怎么办？", "env"),
        ("合同违约是否可以要求继续履行？", "civil"),
    ],
)
def test_infer_domain_legacy_domains_keep_working(query: str, expected: str):
    """既有四领域识别不回归。"""
    assert infer_domain(query) == expected


def test_infer_domain_unknown_on_weak_signal():
    assert infer_domain("你好") == "unknown"


# --------------------------------------------------------------------- 检索过滤

_ITEMS = [
    {"doc_name": "《中华人民共和国劳动合同法》",
     "source_path": r"C:\Users\demo\data\raw\劳动法\劳动合同法.txt"},
    {"doc_name": "《中华人民共和国治安管理处罚法》",
     "source_path": r"C:\Users\demo\data\raw\行政法\治安管理处罚法.txt"},
    {"doc_name": "《中华人民共和国海商法》",
     "source_path": r"C:\Users\demo\data\raw\交通运输\海商法.txt"},
    {"doc_name": "《中华人民共和国知识产权保护条例》",
     "source_path": r"C:\Users\demo\data\raw\知识产权法\知识产权保护条例.txt"},
    {"doc_name": "《中华人民共和国食品安全法》",
     "source_path": r"C:\Users\demo\data\raw\安全生产\食品安全法.txt"},
    {"doc_name": "《中华人民共和国民法典》",
     "source_path": r"C:\Users\demo\data\raw\其他\民法典.txt"},
]


def test_filter_statutes_by_domain_filters_cross_domain():
    """劳动问题不应召回治安/海商/知识产权/食品安全等跨领域法条。"""
    kept = filter_statutes_by_domain(_ITEMS, "labor")
    assert [i["doc_name"] for i in kept] == ["《中华人民共和国劳动合同法》"]


def test_filter_statutes_by_domain_keeps_own_domain():
    kept = filter_statutes_by_domain(_ITEMS, "transport")
    assert [i["doc_name"] for i in kept] == ["《中华人民共和国海商法》"]

    kept = filter_statutes_by_domain(_ITEMS, "safety")
    assert [i["doc_name"] for i in kept] == ["《中华人民共和国食品安全法》"]


def test_filter_statutes_by_domain_raw_dir_fallback():
    """doc_name 词表未覆盖时，source_path 的 data\\raw 目录兜底归属。"""
    items = [
        {"doc_name": "《某未收录之特别条例》",
         "source_path": r"C:\Users\demo\data\raw\财税\某未收录之特别条例.txt"},
        {"doc_name": "《另一特别条例》",
         "source_path": r"C:\Users\demo\data\raw\司法制度\另一特别条例.txt"},
    ]
    kept = filter_statutes_by_domain(items, "tax")
    assert [i["doc_name"] for i in kept] == ["《某未收录之特别条例》"]

    kept = filter_statutes_by_domain(items, "judicature")
    assert [i["doc_name"] for i in kept] == ["《另一特别条例》"]


def test_filter_statutes_by_domain_unknown_returns_all():
    assert filter_statutes_by_domain(_ITEMS, "unknown") == _ITEMS
    assert filter_statutes_by_domain(_ITEMS, "") == _ITEMS
