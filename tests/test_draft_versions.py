"""编辑页草稿：模板预览 / 历史版本 / 恢复 / 归属。

用的是**真库**（与 ``tests/test_api.py`` 一致），所以每个用例自己收尾删数据。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from backend.app.db.session import session_scope
from backend.app.main import app
from backend.app.services import draft_service, user_service

API = "/api/v1"
PASSWORD = "Test-Pa55word!"

# 这份模板在 文书模板 目录里存在时才跑相关用例（换机器/CI 属正常缺失）。
TPL = "广东_劳动仲裁申请书"


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    team = user_service.create_team(_uniq("编辑页测试组"))
    u = user_service.create_user(_uniq("editor"), PASSWORD, role="lawyer",
                                 full_name="编辑页测试律师", team_id=team["id"])
    r = client.post(f"{API}/auth/login",
                    json={"username": u["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def made():
    """记录本用例建出来的草稿，结束时清掉（真库不能留垃圾）。"""
    ids: list[str] = []
    yield ids
    for did in ids:
        draft_service.delete_draft(did)


def _new_draft(client: TestClient, token: str, **extra) -> str:
    payload = {"content": "申请人于 2024 年 1 月入职。", "doc_type": "arbitration_application",
               "title": _uniq("测试文书")}
    payload.update(extra)
    r = client.post(f"{API}/drafts", headers=_auth(token), json=payload)
    assert r.status_code == 201, f"建草稿失败：{r.status_code} {r.text}"
    return r.json()["draft_id"]


def _has_template(name: str) -> bool:
    from backend.app.services import template_service as ts

    return (ts.template_dir() / f"{name}.docx").exists()


# --------------------------------------------------------------------------- 旧数据
def test_old_draft_with_null_new_columns_does_not_500(client: TestClient, token: str, made) -> None:
    """**老草稿（新列全是 NULL）打开不能 500。**

    这是本次改动最容易翻车的地方：``reconcile_schema`` 加列时**不填默认值**，
    所以已有行的 ``template_id`` / ``fills`` / ``content_html`` / ``chat_log``
    全都是 NULL。读路径只要少写一个 ``or``，就是"新草稿好用、打开旧草稿炸"。
    """
    did = _new_draft(client, token)
    made.append(did)

    # 手工把新列还原成 NULL，模拟一份迁移前就存在的草稿
    with session_scope() as db:
        db.execute(sa_text(
            "UPDATE la_draft SET template_id=NULL, fills=NULL, "
            "content_html=NULL, chat_log=NULL WHERE draft_id=:d"), {"d": did})

    r = client.get(f"{API}/drafts/{did}/preview", headers=_auth(token))
    assert r.status_code == 200, f"老草稿预览 500 了：{r.status_code} {r.text}"
    assert r.json()["source"] == "plain", r.json()

    r = client.get(f"{API}/drafts/{did}", headers=_auth(token))
    assert r.status_code == 200, f"老草稿详情 500 了：{r.status_code} {r.text}"
    body = r.json()
    assert body["template_id"] == "" and body["fills"] == {}
    assert body["content_html"] == "" and body["chat_log"] == []


# --------------------------------------------------------------------------- 模板预览
def test_preview_renders_template_faithfully(client: TestClient, token: str, made) -> None:
    """有 ``template_id`` 时，预览走真模板回填 —— 版式标记一个都不能少。

    这是"严格按照模板生成"的验收点：居中标题、正文字体、首行缩进。
    """
    if not _has_template(TPL):
        pytest.skip(f"模板 {TPL} 不在模板目录")

    # 字段 id 由块位置决定（p0 是标题、不可填），所以这里取真实存在的
    # 段落字段 —— 写死 p0/p1 会在别的模板上悄悄变成"没填任何东西"。
    from backend.app.services import template_service as ts

    ids = [f["id"] for f in ts.fields_for_prompt(ts.get_template(TPL)) if f["kind"] == "para"]
    assert ids, "模板没有可填段落了"
    did = _new_draft(client, token, template_id=TPL,
                     fills={ids[0]: "申请人：张三，男，汉族。",
                            ids[1]: "身份证号：440101199001011234。"})
    made.append(did)

    r = client.get(f"{API}/drafts/{did}/preview", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "template", body
    h = body["html"]
    assert "font-family:" in h, "字体丢了"
    assert "font-size:" in h, "字号丢了"
    assert "text-align:center" in h, "标题没居中"
    assert "text-indent:" in h, "正文首行缩进丢了"
    # 回填的值要真的出现
    assert "张三" in h, "fills 没被填进模板"
    # 页面尺寸要给到前端，否则渲染不出"一张纸"
    assert body["page"].get("width"), body["page"]


def test_preview_prefers_manual_edits(client: TestClient, token: str, made) -> None:
    """``content_html`` 非空时**必须**给这份 —— 重渲染会抹掉律师的手动排版。"""
    if not _has_template(TPL):
        pytest.skip(f"模板 {TPL} 不在模板目录")

    did = _new_draft(client, token, template_id=TPL, fills={"p0": "原文"})
    made.append(did)

    r = client.put(f"{API}/drafts/{did}", headers=_auth(token), json={
        "content": "改过的正文", "content_html": "<p>我手动排过版</p>"})
    assert r.status_code == 200, r.text

    body = client.get(f"{API}/drafts/{did}/preview", headers=_auth(token)).json()
    assert body["source"] == "edited", body
    assert "我手动排过版" in body["html"]


def test_preview_strips_script_from_browser_supplied_html(client: TestClient, token: str, made) -> None:
    """浏览器送来的 ``content_html`` 里的脚本必须被挡掉。

    ``contenteditable`` 的 innerHTML 可以包含从任何网页粘贴来的内容，
    而这份 HTML 会**存库、再发给每个打开草稿的人** —— 漏了就是存储型 XSS。
    """
    did = _new_draft(client, token)
    made.append(did)

    r = client.put(f"{API}/drafts/{did}", headers=_auth(token), json={
        "content": "正文",
        "content_html": '<p>正常</p><script>alert(1)</script>'
                        '<img src=x onerror=alert(2)>'})
    assert r.status_code == 200, r.text

    body = client.get(f"{API}/drafts/{did}/preview", headers=_auth(token)).json()
    low = body["html"].lower()
    assert "script" not in low, f"脚本存进去了：{body['html']!r}"
    assert "onerror" not in low
    assert "正常" in body["html"], "净化把正文一起吃掉了"

    # 库里存的也应当是净化过的版本（纵深防御：读的时候还会再净化一次）
    stored = client.get(f"{API}/drafts/{did}", headers=_auth(token)).json()["content_html"]
    assert "script" not in stored.lower(), f"库里存了未净化的 HTML：{stored!r}"


# --------------------------------------------------------------------------- 版本
def test_snapshot_only_when_asked(client: TestClient, token: str, made) -> None:
    """默认**不拍快照**，显式传 ``snapshot=true`` 才拍。

    这不是洁癖：``save_from_state`` 在每个 agent 任务结束时都会跑，而且同一任务
    重跑、缓存命中都会触发。默认拍的话历史会被这些无关任务刷爆。
    """
    did = _new_draft(client, token)
    made.append(did)

    assert client.get(f"{API}/drafts/{did}/versions",
                      headers=_auth(token)).json()["total"] == 0

    # 不传 snapshot：版本号涨了，但历史里不该多东西
    r = client.put(f"{API}/drafts/{did}", headers=_auth(token),
                   json={"content": "第二次内容"})
    assert r.json()["version"] == 2, r.json()
    assert client.get(f"{API}/drafts/{did}/versions",
                      headers=_auth(token)).json()["total"] == 0, "默认拍了快照"

    # 传 snapshot：这才记一条
    r = client.put(f"{API}/drafts/{did}", headers=_auth(token),
                   json={"content": "第三次内容", "snapshot": True})
    assert r.json()["version"] == 3
    vs = client.get(f"{API}/drafts/{did}/versions", headers=_auth(token)).json()
    assert vs["total"] == 1, vs
    assert vs["items"][0]["version"] == 3


def test_unchanged_save_does_not_bump_version(client: TestClient, token: str, made) -> None:
    """内容没变就不涨版本 —— 否则"保存"连点两下就是两条历史。"""
    did = _new_draft(client, token)
    made.append(did)
    r1 = client.put(f"{API}/drafts/{did}", headers=_auth(token), json={"content": "同一份内容"})
    v1 = r1.json()["version"]
    r2 = client.put(f"{API}/drafts/{did}", headers=_auth(token), json={"content": "同一份内容"})
    assert r2.json()["version"] == v1, "内容没变却涨了版本"
    assert r2.json().get("unchanged") is True


def test_restore_goes_back_and_keeps_history(client: TestClient, token: str, made) -> None:
    """恢复到旧版：正文回到那一版，**且中间的历史不被擦掉**。

    "恢复错了想撤回"是常规操作，所以恢复是**追加**一版而不是回滚版本号。
    """
    did = _new_draft(client, token, content="第一版内容")
    made.append(did)

    client.put(f"{API}/drafts/{did}", headers=_auth(token),
               json={"content": "第二版内容", "snapshot": True})
    client.put(f"{API}/drafts/{did}", headers=_auth(token),
               json={"content": "第三版内容", "snapshot": True})
    vs = client.get(f"{API}/drafts/{did}/versions", headers=_auth(token)).json()
    assert vs["total"] == 2, vs
    newest_v2 = [v for v in vs["items"] if v["version"] == 2][0]

    r = client.post(f"{API}/drafts/{did}/versions/{newest_v2['id']}/restore",
                    headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "第二版内容"
    assert r.json()["restored_from"] == 2

    # 恢复后的状态本身也进了历史 → 还能再退回第三版
    vs2 = client.get(f"{API}/drafts/{did}/versions", headers=_auth(token)).json()
    assert vs2["total"] == 3, f"恢复把历史擦掉了或者没记新版本：{vs2}"
    assert any("恢复到 v2" in v["note"] for v in vs2["items"]), vs2


def test_restore_rejects_a_version_of_another_draft(client: TestClient, token: str, made) -> None:
    """A 草稿的版本 id 不能拿来恢复 B 草稿（否则越权读别人草稿的正文）。"""
    d1 = _new_draft(client, token, content="甲草稿")
    d2 = _new_draft(client, token, content="乙草稿")
    made += [d1, d2]
    client.put(f"{API}/drafts/{d1}", headers=_auth(token),
               json={"content": "甲草稿改过", "snapshot": True})
    vid = client.get(f"{API}/drafts/{d1}/versions", headers=_auth(token)).json()["items"][0]["id"]

    r = client.post(f"{API}/drafts/{d2}/versions/{vid}/restore", headers=_auth(token))
    assert r.status_code == 404, f"跨草稿恢复没被拦住：{r.status_code} {r.text}"


def test_versions_are_owner_only(client: TestClient, token: str, made) -> None:
    """别人的草稿，预览和历史版本都不能读。"""
    did = _new_draft(client, token)
    made.append(did)

    other_team = user_service.create_team(_uniq("编辑页外人组"))
    other = user_service.create_user(_uniq("other"), PASSWORD, role="lawyer",
                                     full_name="无关律师", team_id=other_team["id"])
    r = client.post(f"{API}/auth/login",
                    json={"username": other["username"], "password": PASSWORD})
    other_token = r.json()["access_token"]

    for path in (f"{API}/drafts/{did}/preview", f"{API}/drafts/{did}/versions"):
        rr = client.get(path, headers=_auth(other_token))
        assert rr.status_code == 403, f"{path} 没做归属校验：{rr.status_code}"


def test_new_endpoints_require_auth(client: TestClient) -> None:
    """三条新路由都必须登录才能调。"""
    for path in (f"{API}/drafts/whatever/preview",
                 f"{API}/drafts/whatever/versions",
                 f"{API}/drafts/whatever/versions/1/restore"):
        method = client.post if path.endswith("/restore") else client.get
        r = method(path)
        assert r.status_code in (401, 403), f"{path} 免鉴权可达：{r.status_code}"


def test_delete_draft_removes_its_versions(client: TestClient, token: str) -> None:
    """删草稿要把版本快照一起删，否则会留下永远看不到的孤儿行。"""
    from sqlalchemy import func, select

    from backend.app.db.models import DraftVersion

    did = _new_draft(client, token, content="待删草稿")
    client.put(f"{API}/drafts/{did}", headers=_auth(token),
               json={"content": "待删草稿改过", "snapshot": True})
    assert client.get(f"{API}/drafts/{did}/versions",
                      headers=_auth(token)).json()["total"] == 1

    assert client.delete(f"{API}/drafts/{did}", headers=_auth(token)).status_code == 200

    with session_scope() as db:
        left = db.execute(
            select(func.count()).select_from(DraftVersion).where(DraftVersion.draft_id == did)
        ).scalar_one()
    assert left == 0, f"删草稿后还剩 {left} 条版本快照"
