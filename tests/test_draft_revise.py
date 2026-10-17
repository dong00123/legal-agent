"""左框说一句话 → AI 改模板字段（``POST /drafts/{id}/revise``）。

这条链路最危险的失败方式不是报错，而是**看起来很成功**：模型回了
"好的，已为您修改"，用户以为改了，其实一个字没动 —— 或者更糟，它只回了
自己改过的那两个字段，我们把整份 fills 替换掉，其余十来个栏目全空。

所以这里的用例都在验"**本地 fills 是基准、模型返回只是补丁**"这条不变量，
以及"一处没改成时什么都不许动"。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import draft_service, template_service as ts, user_service

API = "/api/v1"
PASSWORD = "Test-Pa55word!"
TPL = "广东_劳动仲裁申请书"


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Res:
    """``LLMResult`` 的最小替身（只用得到这两个字段）。"""

    def __init__(self, degraded: bool = False, provider: str = "fake") -> None:
        self.degraded = degraded
        self.provider = provider


def _fake_llm(monkeypatch, data, *, degraded: bool = False, provider: str = "fake"):
    """把 ``chat_json`` 换成固定返回值，顺带记下真正发出去的消息。"""
    from backend.llm import client as llm_client_mod

    seen: dict = {}

    async def _chat_json(messages, **kwargs):
        seen["messages"] = messages
        seen["task"] = kwargs.get("task")
        seen["payload"] = kwargs.get("payload")
        if isinstance(data, Exception):
            raise data
        return data, _Res(degraded=degraded, provider=provider)

    monkeypatch.setattr(llm_client_mod.llm_client, "chat_json", _chat_json)
    return seen


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    team = user_service.create_team(_uniq("改字段测试组"))
    u = user_service.create_user(_uniq("reviser"), PASSWORD, role="lawyer",
                                 full_name="改字段测试律师", team_id=team["id"])
    r = client.post(f"{API}/auth/login",
                    json={"username": u["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def made():
    ids: list[str] = []
    yield ids
    for did in ids:
        draft_service.delete_draft(did)


def _para_ids() -> list[str]:
    """真模板里可填的段落字段 id（不写死 p0/p1 —— 那是标题，填不进去）。"""
    tpl = ts.get_template(TPL)
    if tpl is None:
        pytest.skip(f"模板 {TPL} 不在模板目录")
    ids = [f["id"] for f in ts.fields_for_prompt(tpl) if f["kind"] == "para"]
    assert ids, "模板没有可填段落了"
    return ids


def _new_draft(client, token, made, **extra) -> str:
    payload = {"content": "申请人于 2024 年 1 月入职。", "doc_type": "arbitration_application",
               "title": _uniq("改字段"), "template_id": TPL}
    payload.update(extra)
    r = client.post(f"{API}/drafts", headers=_auth(token), json=payload)
    assert r.status_code == 201, f"建草稿失败：{r.status_code} {r.text}"
    made.append(r.json()["draft_id"])
    return r.json()["draft_id"]


def _revise(client, token, did, instruction="把请求事项第 2 条改成 80000 元", **body):
    payload = {"instruction": instruction}
    payload.update(body)
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token), json=payload)
    assert r.status_code == 200, f"revise 失败：{r.status_code} {r.text}"
    return r.json()


# --------------------------------------------------------------------------- 合并语义
def test_untouched_fields_survive(client, token, made, monkeypatch) -> None:
    """**模型只回它改过的字段时，其余栏目不能凭空消失。**

    这是整条链路最容易出的事故：把模型返回当成新的 fills 全集去替换，
    十几个栏目一夜之间全空，而页面上只看到"改动成功"。
    """
    ids = _para_ids()
    base = {ids[0]: "申请人：张三，男，汉族。", ids[1]: "身份证号：440101199001011234。"}
    if len(ids) > 2:
        base[ids[2]] = "住址：广州市天河区某路 1 号。"
    did = _new_draft(client, token, made, fills=base)

    _fake_llm(monkeypatch, {"fills": {ids[0]: "申请人：张三，男，汉族，1985 年生。"},
                            "reply": "已修改申请人信息"})
    out = _revise(client, token, did)

    assert out["fills"][ids[0]].endswith("1985 年生。"), out["fills"]
    for fid, val in base.items():
        if fid == ids[0]:
            continue
        assert out["fills"].get(fid) == val, f"{fid} 被清掉了：{out['fills']}"
    assert out["changed_count"] == 1, out["changed"]
    assert out["changed"][0]["id"] == ids[0]
    assert out["changed"][0]["old"].startswith("申请人")


def test_invented_field_id_is_dropped(client, token, made, monkeypatch) -> None:
    """**模型造出来的字段 id 一律丢掉。**

    落不到任何模板块上的 id 写进 fills 就是"改成功了但文书没变" —— 用户
    盯着页面找半天，找不到哪里变了。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原文保留"})

    _fake_llm(monkeypatch, {"fills": {"p999": "凭空造的一栏", ids[0]: "原文保留"},
                            "reply": "已新增一栏"})
    out = _revise(client, token, did)

    assert "p999" not in out["fills"], f"造出来的字段写进去了：{out['fills']}"
    assert out["changed_count"] == 0, out["changed"]
    assert out["fills"][ids[0]] == "原文保留"


def test_same_value_is_not_reported_as_changed(client, token, made, monkeypatch) -> None:
    """模型把原值原样回一遍，不算"改了"。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "一模一样的内容"})

    _fake_llm(monkeypatch, {"fills": {ids[0]: "一模一样的内容"}, "reply": "已修改"})
    out = _revise(client, token, did)
    assert out["changed_count"] == 0, out["changed"]


# --------------------------------------------------------------------------- 如实回复
def test_zero_change_reply_does_not_echo_the_model(client, token, made, monkeypatch) -> None:
    """模型说"已修改"但一处没动时，**回复必须由本地按实际改动生成**。

    直接采信模型自述，就是"用户以为改成了、其实没改"的标准事故。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原样"})

    _fake_llm(monkeypatch, {"fills": {}, "reply": "好的，已为您修改完成"})
    out = _revise(client, token, did)

    assert out["changed_count"] == 0
    assert "没有找到可以修改的栏目" in out["reply"], out["reply"]
    assert "已为您修改" not in out["reply"] or "模型说明" in out["reply"], out["reply"]
    # 模型的解释要带上，用户才知道为什么没改成
    assert "模型说明" in out["reply"], out["reply"]


def test_reply_lists_what_actually_changed(client, token, made, monkeypatch) -> None:
    """有改动时，回复里列的是**真的动过的栏目**。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "旧内容"})

    _fake_llm(monkeypatch, {"fills": {ids[0]: "新内容"}, "reply": "改了标题"})
    out = _revise(client, token, did)
    assert "已修改：" in out["reply"], out["reply"]
    assert "改了标题" not in out["reply"], "把模型自述当成了结果说明"


def test_degraded_is_reported_honestly(client, token, made, monkeypatch) -> None:
    """模型不可用退回规则引擎时，``degraded`` 要如实透出来。

    前端据此提示"这次不是模型改的"。吞掉这个标志，用户会把规则引擎的
    空结果当成"AI 看过一遍说不用改"。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原样"})

    _fake_llm(monkeypatch, {"fills": {}}, degraded=True, provider="rule")
    out = _revise(client, token, did)
    assert out["degraded"] is True
    assert out["provider"] == "rule"


# --------------------------------------------------------------------------- 重渲染
def test_html_is_rerendered_and_template_fidelity_kept(client, token, made, monkeypatch) -> None:
    """改完字段后返回的 HTML 要**仍然是那份模板的版式**。

    改字段的全部意义就在于：改了之后还长得像原模板。这里连带验版式标记，
    因为最容易的退化是"改完变成纯文本"。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "旧的申请人信息"})

    _fake_llm(monkeypatch, {"fills": {ids[0]: "申请人：李四，女，汉族。"}, "reply": "ok"})
    out = _revise(client, token, did)

    h = out["html"]
    assert h, "改了字段却没重渲染 HTML"
    assert "李四" in h, f"新值没进 HTML：{h[:200]!r}"
    assert "font-family:" in h and "font-size:" in h, "重渲染把字体字号弄丢了"
    assert "text-align:center" in h, "重渲染把标题居中弄丢了"

    # 重渲染后必须清掉手动 HTML，否则预览还会优先给旧的那份
    detail = client.get(f"{API}/drafts/{did}", headers=_auth(token)).json()
    assert detail["content_html"] == "", "重渲染了但旧的手动 HTML 还在，预览会拿到过期的"
    assert detail["fills"][ids[0]] == "申请人：李四，女，汉族。"


def test_chat_log_is_persisted(client, token, made, monkeypatch) -> None:
    """左框的对话要存进草稿，刷新/重进编辑页不丢。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原样"})

    log = [{"role": "user", "text": "把申请人改成李四"},
           {"role": "ai", "text": "已修改：申请人信息。"}]
    _fake_llm(monkeypatch, {"fills": {ids[0]: "申请人：李四"}, "reply": "ok"})
    _revise(client, token, did, chat_log=log)

    detail = client.get(f"{API}/drafts/{did}", headers=_auth(token)).json()
    assert detail["chat_log"] == log, detail["chat_log"]


# --------------------------------------------------------------------------- 边界
def test_zero_change_keeps_manual_editing(client, token, made, monkeypatch) -> None:
    """**一处没改成时，手动排版不能被"顺手"清掉。**

    否则律师问了一句落不到字段上的话，回答是"没改"，代价却是排版全丢 ——
    没帮上忙还帮了倒忙。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原样"})
    client.put(f"{API}/drafts/{did}", headers=_auth(token), json={
        "content": "申请人于 2024 年 1 月入职。",
        "content_html": '<p style="text-align:center">我手动排过版</p>'})

    _fake_llm(monkeypatch, {"fills": {}, "reply": "做不到"})
    out = _revise(client, token, did, "在正文中间插一整段新论述", force=True)

    assert out["changed_count"] == 0
    assert out["html"] == "", "没改动却重渲染了"
    detail = client.get(f"{API}/drafts/{did}", headers=_auth(token)).json()
    assert "我手动排过版" in detail["content_html"], "手动排版被清掉了"
    # 没改动就不该拍快照 —— 历史里凭空多一条"AI 改过"
    assert client.get(f"{API}/drafts/{did}/versions",
                      headers=_auth(token)).json()["total"] == 0


def test_409_when_manually_edited_without_force(client, token, made, monkeypatch) -> None:
    """有手动排版时先 409，**在服务端拦**（前端可以旧、可以被绕过）。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原样"})
    client.put(f"{API}/drafts/{did}", headers=_auth(token), json={
        "content": "申请人于 2024 年 1 月入职。", "content_html": "<p>手动排版</p>"})

    seen = _fake_llm(monkeypatch, {"fills": {ids[0]: "新值"}, "reply": "ok"})
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token),
                    json={"instruction": "改一下"})
    assert r.status_code == 409, f"没拦：{r.status_code} {r.text}"
    assert "force" in r.text
    assert "messages" not in seen, "被拦下了却还是调了模型 —— 白花钱"

    # 带 force 才放行
    out = _revise(client, token, did, force=True)
    assert out["changed_count"] == 1
    assert out["fills"][ids[0]] == "新值"


def test_400_when_draft_has_no_template(client, token, made, monkeypatch) -> None:
    """不是套模板生成的草稿，没有字段可改 —— 说清楚，而不是硬改一通。"""
    did = _new_draft(client, token, made, template_id="", fills={})
    _fake_llm(monkeypatch, {"fills": {}, "reply": "ok"})
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token),
                    json={"instruction": "改一下"})
    assert r.status_code == 400, f"{r.status_code} {r.text}"
    assert "模板" in r.text


def test_missing_template_file_404(client, token, made, monkeypatch) -> None:
    """``template_id`` 指向一个已经不存在的模板时，报 404 而不是 500。"""
    did = _new_draft(client, token, made, template_id="这个模板不存在", fills={})
    _fake_llm(monkeypatch, {"fills": {}, "reply": "ok"})
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token),
                    json={"instruction": "改一下"})
    assert r.status_code == 404, f"{r.status_code} {r.text}"


def test_model_failure_is_502_and_draft_untouched(client, token, made, monkeypatch) -> None:
    """模型调用炸了要报 502，**且草稿保持原样** —— 半改半不改最糟。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原值"})
    _fake_llm(monkeypatch, RuntimeError("网关 502"))

    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token),
                    json={"instruction": "改一下"})
    assert r.status_code == 502, f"{r.status_code} {r.text}"
    detail = client.get(f"{API}/drafts/{did}", headers=_auth(token)).json()
    assert detail["fills"][ids[0]] == "原值", "模型失败却把草稿改了"
    assert client.get(f"{API}/drafts/{did}/versions",
                      headers=_auth(token)).json()["total"] == 0


def test_empty_instruction_rejected(client, token, made) -> None:
    """空指令不该打模型。"""
    did = _new_draft(client, token, made, fills={})
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(token),
                    json={"instruction": ""})
    assert r.status_code == 422, f"{r.status_code} {r.text}"


# --------------------------------------------------------------------------- 版本 / 归属
def test_snapshot_recorded_as_ai_revise(client, token, made, monkeypatch) -> None:
    """改动要留下一版快照，来源标 ``ai_revise`` —— 历史弹层要靠它区分"谁改的"。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "旧值"})

    _fake_llm(monkeypatch, {"fills": {ids[0]: "新值"}, "reply": "ok"})
    out = _revise(client, token, did, "把申请人改成李四")

    vs = client.get(f"{API}/drafts/{did}/versions", headers=_auth(token)).json()
    assert vs["total"] == 1, vs
    item = vs["items"][0]
    assert item["source"] == "ai_revise", item
    assert item["version"] == out["version"], (item, out["version"])
    assert "李四" in item["note"], f"指令没记进备注：{item}"
    # 列表刻意不带正文（历史一多就是几 MB），所以真去恢复一次 ——
    # 顺带验**快照拍的是改完之后的状态**而不是改之前的：拍早一拍，
    # "恢复"就等于把 AI 的改动又退回去，而页面上看不出来。
    restored = client.post(f"{API}/drafts/{did}/versions/{item['id']}/restore",
                           headers=_auth(token)).json()
    assert restored["fills"][ids[0]] == "新值", f"快照里不是改后的值：{restored}"


def test_revise_is_owner_only(client, token, made, monkeypatch) -> None:
    """别人的草稿不能改。"""
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "原值"})

    other_team = user_service.create_team(_uniq("改字段外人组"))
    other = user_service.create_user(_uniq("outsider"), PASSWORD, role="lawyer",
                                     full_name="无关律师", team_id=other_team["id"])
    other_token = client.post(f"{API}/auth/login",
                              json={"username": other["username"],
                                    "password": PASSWORD}).json()["access_token"]

    seen = _fake_llm(monkeypatch, {"fills": {ids[0]: "偷改的值"}, "reply": "ok"})
    r = client.post(f"{API}/drafts/{did}/revise", headers=_auth(other_token),
                    json={"instruction": "改一下"})
    assert r.status_code == 403, f"越权改字段：{r.status_code} {r.text}"
    assert "messages" not in seen, "越权请求也把模型调了"


def test_404_for_unknown_draft(client, token, monkeypatch) -> None:
    _fake_llm(monkeypatch, {"fills": {}, "reply": "ok"})
    r = client.post(f"{API}/drafts/不存在/revise", headers=_auth(token),
                    json={"instruction": "改一下"})
    assert r.status_code == 404, f"{r.status_code} {r.text}"


def test_revise_requires_auth(client) -> None:
    r = client.post(f"{API}/drafts/whatever/revise", json={"instruction": "改一下"})
    assert r.status_code in (401, 403), f"免鉴权可达：{r.status_code}"


def test_prompt_carries_current_fills_and_instruction(client, token, made, monkeypatch) -> None:
    """发出去的 prompt 里必须**真的有**当前 fills 和用户这句话。

    只断言"调了 chat_json"是不够的：漏了传 fills，模型就只能瞎猜，
    而它猜出来的东西照样会写进草稿。
    """
    ids = _para_ids()
    did = _new_draft(client, token, made, fills={ids[0]: "申请人：王五，男。"})

    seen = _fake_llm(monkeypatch, {"fills": {}, "reply": "ok"})
    _revise(client, token, did, "把申请人年龄改成 40 岁")

    sent = seen["messages"][0]["content"]
    assert "申请人：王五，男。" in sent, "当前 fills 没进 prompt"
    assert "把申请人年龄改成 40 岁" in sent, "用户指令没进 prompt"
    assert ids[0] in sent, "字段清单没进 prompt（模型没法知道合法 id）"
    assert seen["task"] == "template_revise", seen["task"]
