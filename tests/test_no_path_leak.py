"""服务端路径不得出现在对外输出里。

**为什么单独一个文件。** 这类问题不会让任何测试变红 —— 接口照常返回 200，
功能完全正常，只是响应里多了一个键。它只会在"有人把接口响应贴给外人看"
或者"生成的文书被直接发给客户"时出事，而那时已经晚了。所以要用测试把
这条边界钉住。

两个泄漏面都要堵：

1. **API 响应**（``citation_to_dict``）—— 调用方能拿到；
2. **生成的文书正文**（``rule_engine``）—— 更隐蔽也更危险，因为律师会
   直接把它复制出去。类案文件名里还可能带当事人姓名。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services.case_service import material_to_dict
from backend.app.services.citation_service import citation_to_dict
from backend.llm import rule_engine

# 绝对路径的样子：Windows 盘符、POSIX 根、以及入库时用的相对路径 data/raw/...
_ABS_WIN = re.compile(r"[A-Za-z]:[\\/]")
_ABS_POSIX = re.compile(r"(?:^|[\s（(（\"])/(?:app|home|usr|data|root|Users)/")
_REL_CORPUS = re.compile(r"data[\\/](?:raw|corpus|processed|knowledge)")


def test_citation_dict_has_no_source_path() -> None:
    """API 返回的引用条目里不能有 ``source_path``。

    没有任何调用方用它（前端明确不渲染），但它会把服务端目录结构带出去。
    """

    class _C:  # 只提供 citation_to_dict 实际读取的字段
        id = 1
        task_id = "t"
        case_id = "c"
        ctype = "statute"
        name = "中华人民共和国劳动法"
        article = "第 10 条"
        case_no = ""
        court = ""
        status = "有效"
        version = "2018"
        source_path = "C:\\secret\\data\\raw\\劳动法\\中华人民共和国劳动法.pdf"
        raw = "正文"
        verified = True
        reason = ""
        confidence = 1.0
        created_at = None

    d = citation_to_dict(_C())  # type: ignore[arg-type]
    assert "source_path" not in d, "引用响应又把服务端路径带回去了"
    assert "C:\\secret" not in str(d), "路径以别的键名泄漏出来了"


def test_material_dict_has_no_server_paths() -> None:
    """材料响应里不能有 ``stored_path``，**也不许有 ``parsed_path``**。

    来历（2026-10-12）：``parsed_path`` 一直跟着材料响应往外漏 ——
    ``GET /cases/{id}/materials``、``GET /materials/{id}``、``POST /materials/{id}/parse``
    三条都带它，而**前端从来没用过**（只用到 ``parsed`` 这个布尔量）。
    当初只把 ``stored_path`` 藏起来，是因为只想到了它；同一个坑的第二个字段
    就这么静静地漏了很久，直到写「查看材料」时按"响应里不许出现 path"断言才抓到。

    这条守住两头：默认不给任何路径；内部调用方显式 ``with_path=True`` 才拿 ``stored_path``
    （解析管线要靠它找文件，不能一并删掉）。
    """

    class _M:  # 只提供 material_to_dict 实际读取的字段
        id = 1
        case_id = "c"
        filename = "借条.txt"
        doc_type = "evidence"
        size_bytes = 10
        sha256 = "x"
        parsed = True
        created_at = None
        uploaded_by = 1
        stored_path = "C:\\secret\\uploads\\借条.txt"
        parsed_path = "C:\\secret\\processed\\借条.json"

    d = material_to_dict(_M())  # type: ignore[arg-type]
    assert "stored_path" not in d, "材料响应把服务端原文路径带出去了"
    assert "parsed_path" not in d, "材料响应又把服务端解析路径带回去了"
    assert "C:\\secret" not in str(d), f"路径以别的键名泄漏出来了：{sorted(d)}"

    d2 = material_to_dict(_M(), with_path=True)  # type: ignore[arg-type]
    assert d2["stored_path"] == _M.stored_path, (
        "with_path=True 是服务端内部的口子，删了它解析管线条就找不到文件"
    )


def test_rule_engine_does_not_print_source_path() -> None:
    """生成文书的引用清单里不能出现路径。

    律师会直接读这段，也会直接把它复制给客户 —— 所以这里的要求比接口更严：
    不光不能是绝对路径，连 ``data/raw/...`` 这种相对路径也不行。
    """
    src = Path(rule_engine.__file__).read_text(encoding="utf-8")
    # 直接盯住那段格式化字符串：它拼的是"｜来源："，曾经后面跟 source_path
    assert "｜来源：{s.get('source_path')" not in src, (
        "规则引擎又把 source_path 拼进文书正文了"
    )

    for pat, why in (
        (_ABS_WIN, "Windows 绝对路径"),
        (_ABS_POSIX, "POSIX 绝对路径"),
        (_REL_CORPUS, "语料相对路径"),
    ):
        hits = [ln for ln in src.splitlines() if pat.search(ln)
                and not ln.lstrip().startswith("#")]
        assert not hits, f"rule_engine 里出现{why}：{hits[:2]}"


def test_task_result_strips_source_path_recursively() -> None:
    """任务结果里**任意一层**的 ``source_path`` 都要剥掉。

    **这条测的是"任意深度"，不是"已知路径"。** 引用条目藏在 ``result.statutes``
    还是 ``result.cases``、将来会不会挪到 ``result.evidence.hits``，取决于编排层
    怎么写 —— 只盯已知路径的断言会在结构一变就悄悄失效，而且还以为边界有人守着。
    """
    from backend.app.services.task_service import _strip_source_paths

    result = {
        "statutes": [{"name": "劳动合同法", "source_path": "C:\\srv\\data\\raw\\a.pdf"}],
        "cases": [{"case_no": "（2024）沪01民终1号",
                   "source_path": "/app/data/raw/张三诉李四劳动争议.pdf"}],
        # 嵌套一层：模拟引用条目将来挪到别的容器里
        "evidence": {"hits": [{"source_path": "D:\\x\\y.pdf", "text": "..."}]},
        "answer": "正文里不该有路径",
        "confidence": 0.8,
    }
    clean = _strip_source_paths(result)

    def _find_paths(obj, path="") -> list[str]:
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "source_path":
                    found.append(f"{path}.{k}")
                found += _find_paths(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found += _find_paths(v, f"{path}[{i}]")
        return found

    assert _find_paths(clean) == [], "多层嵌套里还有 source_path 没剥干净"
    # 剥键不能顺手改掉别的内容 —— 为了让断言变绿而丢数据就本末倒置了
    assert clean["statutes"][0]["name"] == "劳动合同法"
    assert clean["cases"][0]["case_no"] == "（2024）沪01民终1号"
    assert clean["evidence"]["hits"][0]["text"] == "..."
    assert clean["answer"] == "正文里不该有路径"
    # 原对象不被就地修改：落库快照要留完整信息供追溯
    assert "source_path" in result["statutes"][0]


def test_to_dict_result_has_no_source_path() -> None:
    """``TaskService._to_dict`` 返回的 ``result`` 里不能有路径。

    上一条测函数本身，这一条测它**真的被接到了序列化出口上** —— 函数写了却没调用，
    是这类修复最常见的结局（本文件存在之前，这个泄漏就是这么留下来的）。
    """
    from backend.app.services.task_service import TaskService

    class _Row:
        task_id = "t"
        case_id = ""
        user_id = 1
        team_id = 1
        kind = "chat"
        intent = "consult"
        query = "q"
        status = "succeeded"
        progress = 100
        citation_check: dict = {}
        confidence = 0.9
        elapsed_ms = 1
        source_client = "test"
        error = ""
        evidence_gap: list = []
        created_at = None
        updated_at = None
        finished_at = ""
        result = {"statutes": [{"name": "劳动法",
                                "source_path": "C:\\srv\\data\\raw\\a.pdf"}]}
        state: dict = {}
        trace: list = []
        tool_calls: list = []

    d = TaskService._to_dict(_Row())  # type: ignore[arg-type]
    assert "source_path" not in str(d["result"]), "result 又带回服务端路径了"


def test_generated_text_has_no_path_when_statutes_present() -> None:
    """端到端地跑一次文本装配，确认输出里确实没有路径。

    上面那条是查源码，这条是查产物 —— 只查源码的话，路径从别处拼接
    （比如外层又 format 了一次）就漏了。
    """
    # 装配入口是 ``_h_report``（规则引擎的"分析意见"handler）。
    # 走私有函数是有意的：泄漏就发生在这段拼接里，测包装层测不到它。
    build = getattr(rule_engine, "_h_report", None)
    assert build is not None, "规则引擎的 report handler 改名了，这条守卫要跟着更新"

    payload = {
        "statutes": [{
            "doc_name": "中华人民共和国劳动法",
            "article_no": "第 10 条",
            "status": "有效",
            "source_path": "/app/data/raw/劳动法/x.pdf",
        }],
        "cases": [],
        "knowledges": [],
        "intent": "consult",
    }
    text = str(build(payload, []))
    assert "/app/data" not in text and "data/raw" not in text, (
        f"文书正文里出现了路径：{[ln for ln in text.splitlines() if 'data' in ln][:2]}"
    )


# --------------------------------------------------------------------------- 文书模板目录
# 这一组是 2026-09-27 补的。上面四条守的是"引用/任务结果"那条线，而
# ``GET /doc/templates`` 是**另一条**完全没被看着的出口：它当时既没有鉴权，
# 又把 ``template_dir()`` 的绝对路径塞在 ``dir`` 里返回 ——
# ``curl`` 不带任何 token 就能拿到 ``C:\Users\<部署者>\Desktop\文书模板``。
# 换句话说，光把值改成目录名只堵了一半：没有鉴权的话，目录名本身也是白给的。


def test_doc_templates_requires_auth() -> None:
    """``GET /doc/templates`` 必须登录才能调。

    模板目录的绝对路径曾经就是从这里匿名泄出去的。配套的
    ``stats()``「只回目录名」是第二道，不能因为补了鉴权就把那道撤了 ——
    鉴权只挡匿名调用者，挡不住"已登录的人把响应贴出去"。
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from backend.app.main import app

    with fastapi_testclient.TestClient(app) as client:
        resp = client.get("/api/v1/doc/templates")
        assert resp.status_code == 401, (
            f"/doc/templates 又能匿名访问了（HTTP {resp.status_code}）—— "
            "它返回模板目录信息，必须挂 RequireUser。"
        )


def test_template_stats_exposes_dir_name_only() -> None:
    """模板目录信息只给目录名，不给绝对路径。

    键名 ``dir`` 不能删：前端（``web/app.js`` 的 ``paintTemplates``）拿它拼
    「请把 .docx 模板放进 … 目录」的空状态提示。所以这里断言的是**值**。
    """
    from backend.app.services import template_service as ts

    d = ts.stats()
    assert "dir" in d, "前端空状态提示依赖 dir 这个键，删了它那条提示就瞎了"

    leaked = _ABS_WIN.search(str(d["dir"]))
    assert not leaked, (
        f"模板目录的绝对路径又回到响应里了：{d['dir']!r}。"
        "这条接口是给前端拼提示文案用的，目录名就够，绝对路径会带出部署者的用户名。"
    )
    assert d["dir"] == ts.template_dir().name, (
        f"dir 应当是目录名 {ts.template_dir().name!r}，实际是 {d['dir']!r}"
    )


async def test_compose_error_message_has_no_absolute_path(monkeypatch) -> None:
    """没有模板时那条报错文案里不能带绝对路径。

    这条文案会被 ``chat.py`` 原样当成 HTTP 400 的 ``detail`` 返回给客户端，
    所以它和接口响应一样，是对外输出。查源码是查不出来的 —— 路径是在
    运行时由 f-string 拼进去的，只能真的抛一次看。
    """
    from backend.app.services import template_service as ts

    # 让它以为一个模板都没有。直接换掉 pick_template，避免真去扫盘 / 写模板缓存。
    monkeypatch.setattr(ts, "pick_template", lambda *a, **k: None)

    with pytest.raises(ValueError) as ei:
        await ts.compose("申请人于 2024 年 1 月入职，公司拖欠工资。")

    msg = str(ei.value)
    assert not _ABS_WIN.search(msg), (
        f"compose 的报错文案里带上了绝对路径：{msg!r}。"
        "它会作为 400 的 detail 直接发给客户端。"
    )
    assert ts.template_dir().name in msg, (
        f"文案里应当只剩目录名（{ts.template_dir().name!r}），实际是 {msg!r} —— "
        "别为了不泄漏把有用的信息一起删掉。"
    )
