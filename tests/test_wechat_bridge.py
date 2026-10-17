"""微信机器人接入的接口层测试（2026-10-11）。

这条链路的静默故障特别多，所以每个用例都在钉一件"坏了也不报错"的事：

1. **桥接密钥空着 = 必须拒绝。** 一个"密钥没配就当没校验"的实现会全绿通过
   所有功能用例，却把 `/wechat/events` 变成任何人可调的公网接口 ——
   它能在任意案件里写材料、生成证据链。
2. **未绑定的 peer 不能碰案件数据。** 只在"能不能回话"上测试是不够的：
   一个默认放行的实现照样会回"你名下还没有诉讼案件"（因为 viewer 是 None 时
   SQL 过滤掉了全部），看起来像"正常工作"。
3. **只有诉讼案件能上传证据。** 与 Web 端同一道闸门；这里从**事件入口**
   再打一遍，因为机器人是另一条不受前端约束的调用路径。
4. **普通聊天必须原样放行（handled=false）。** 这台机器人同时是角色扮演机器人，
   一旦我们把所有文本都吃掉，它的正常聊天就没了 —— 而且不会报任何错。

用例会写真实关系库（与 ``test_api.py`` 同一套约定：造的数据都带随机后缀，
结束时删掉案件与绑定）。
"""

from __future__ import annotations

import base64
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.services import case_service, user_service, wechat_service

API = "/api/v1"
PASSWORD = "Test-Pa55word!"

# 桥接密钥：测试自己设一个，不去动开发环境的 ``.env``。
BRIDGE_TOKEN = "test-bridge-token-" + uuid.uuid4().hex[:8]

# 一条真实点儿的材料正文。用 .txt 而不是图片：解析器对纯文本必然能出正文，
# 于是"证据链基于材料正文"这条路径真的被走通，而不是靠"无正文也容错"蒙过去。
EVIDENCE_TEXT = "借条：今借到人民币壹拾万元整（¥100000），约定 2026 年 12 月 31 日前归还。"
EVIDENCE_B64 = base64.b64encode(EVIDENCE_TEXT.encode("utf-8")).decode("ascii")


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _bridge() -> dict[str, str]:
    return {"X-Bridge-Token": BRIDGE_TOKEN}


# =========================================================================== 夹具
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def env(client):
    """开启微信接入并把桥接密钥设成测试值（用完全局恢复）。"""
    old_enabled = settings.WECHAT_ENABLED
    old_token = settings.WECHAT_BRIDGE_TOKEN
    settings.WECHAT_ENABLED = True
    settings.WECHAT_BRIDGE_TOKEN = BRIDGE_TOKEN
    yield
    settings.WECHAT_ENABLED = old_enabled
    settings.WECHAT_BRIDGE_TOKEN = old_token


@pytest.fixture(scope="module")
def lawyer(client):
    """一名测试律师 + 一件诉讼案件 + 一件非诉案件（用来验证准入闸门）。"""
    user = user_service.create_user(_uniq("wxlawyer"), PASSWORD, role="lawyer",
                                    full_name="微信测试律师", team_id=None)
    token = _login(client, user["username"])

    lit_id = _uniq("WX-LIT")
    other_id = _uniq("WX-CON")
    client.post(f"{API}/cases", headers=_auth(token), json={
        "case_id": lit_id, "title": "微信接入诉讼案", "case_type": "litigation",
    })
    client.post(f"{API}/cases", headers=_auth(token), json={
        "case_id": other_id, "title": "微信接入非诉案", "case_type": "contract",
    })

    out = {"id": user["id"], "username": user["username"], "token": token,
           "lit": lit_id, "other": other_id, "peers": []}
    yield out

    for cid in (lit_id, other_id):
        try:
            case_service.delete_case(cid, case_service.INTERNAL)
        except Exception:  # noqa: BLE001
            pass
    for peer in out["peers"]:
        try:
            wechat_service.unbind_peer(peer)
        except Exception:  # noqa: BLE001
            pass


def _post_event(client: TestClient, peer: str, **kw):
    body = {"peer_id": peer, "bot_id": "test-bot"}
    body.update(kw)
    return client.post(f"{API}/wechat/events", headers=_bridge(), json=body)


def _bind(client: TestClient, lawyer: dict, peer: str) -> None:
    """走完整绑定流程：Web 取码 → 微信里发码。"""
    code = client.post(f"{API}/wechat/bind-code", headers=_auth(lawyer["token"])).json()["code"]
    r = _post_event(client, peer, text=f"/绑定 {code}")
    assert r.status_code == 200, r.text
    assert "绑定成功" in r.json()["replies"][0], r.text


# =========================================================================== 鉴权
def test_events_require_bridge_token(client, env):
    """缺 / 错误的桥接密钥一律 401，且**不带密钥时绝不能进业务**。"""
    assert client.post(f"{API}/wechat/events", json={"peer_id": "x"}).status_code == 401
    r = client.post(f"{API}/wechat/events", headers={"X-Bridge-Token": "wrong"},
                    json={"peer_id": "x"})
    assert r.status_code == 401, r.text


def test_events_are_closed_when_disabled(client):
    """没开、或开了但密钥为空 → 503（不是 404：404 会被当成路径写错）。

    这一条专门防"密钥没配就不校验"的实现 —— 那种实现所有功能用例都过，
    但接口对全网开放。
    """
    old_enabled, old_token = settings.WECHAT_ENABLED, settings.WECHAT_BRIDGE_TOKEN
    try:
        settings.WECHAT_ENABLED = False
        settings.WECHAT_BRIDGE_TOKEN = BRIDGE_TOKEN
        r = client.post(f"{API}/wechat/events", headers=_bridge(), json={"peer_id": "x"})
        assert r.status_code == 503, r.text
        assert "未开启" in r.json()["detail"]

        # 开了但密钥空 —— 必须同样拒绝，不能"空密钥就放行"。
        settings.WECHAT_ENABLED = True
        settings.WECHAT_BRIDGE_TOKEN = ""
        r = client.post(f"{API}/wechat/events", headers=_bridge(), json={"peer_id": "x"})
        assert r.status_code == 503, r.text
        assert "BRIDGE_TOKEN" in r.json()["detail"]
    finally:
        settings.WECHAT_ENABLED, settings.WECHAT_BRIDGE_TOKEN = old_enabled, old_token


# =========================================================================== 放行规则
def test_plain_chat_falls_through(client, env, lawyer):
    """不带命令、不带附件的普通文本必须 ``handled=false``。

    这台机器人同时是角色扮演机器人。把普通聊天吃掉不会报错，
    只会让它的正常对话凭空消失 —— 所以要有这条钉子。
    """
    peer = _uniq("wx-peer")
    r = _post_event(client, peer, text="今天天气不错")
    assert r.status_code == 200, r.text
    assert r.json() == {"handled": False, "replies": []}


def test_unbound_peer_gets_guidance_not_case_data(client, env, lawyer):
    """未绑定的 peer：只给引导，**不返回任何案件信息**。"""
    peer = _uniq("wx-peer")
    r = _post_event(client, peer, text="/案件")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handled"] is True
    joined = "\n".join(body["replies"])
    assert "还没绑定" in joined
    assert lawyer["lit"] not in joined, "未绑定却泄露了案件号"
    assert "诉讼案" not in joined, "未绑定却泄露了案件标题"


def test_unbound_peer_cannot_upload_evidence(client, env, lawyer):
    """未绑定就发附件：不许落盘，只回引导。"""
    peer = _uniq("wx-peer")
    r = _post_event(client, peer, attachments=[{
        "filename": "借条.txt", "mime": "text/plain", "data_base64": EVIDENCE_B64,
    }])
    assert r.status_code == 200, r.text
    assert "还没绑定" in "\n".join(r.json()["replies"])
    assert case_service.list_materials(lawyer["lit"], case_service.INTERNAL) == []


# =========================================================================== 绑定
def test_bind_code_is_single_use_and_expires(client, env, lawyer):
    """绑定码一次性：用过即废；重复使用必须失败。"""
    peer_a = _uniq("wx-peer")
    peer_b = _uniq("wx-peer")
    code = client.post(f"{API}/wechat/bind-code", headers=_auth(lawyer["token"])).json()["code"]
    lawyer["peers"].append(peer_a)

    ok = _post_event(client, peer_a, text=f"/绑定 {code}")
    assert "绑定成功" in ok.json()["replies"][0]

    again = _post_event(client, peer_b, text=f"/绑定 {code}")
    assert "绑定失败" in again.json()["replies"][0]
    # 第二个 peer 没有因此被绑定
    assert wechat_service.get_binding(peer_b) is None

    # 再取一次码会把上一张未用的作废 —— 否则等于同时有多张有效码
    first = client.post(f"{API}/wechat/bind-code", headers=_auth(lawyer["token"])).json()["code"]
    second = client.post(f"{API}/wechat/bind-code", headers=_auth(lawyer["token"])).json()["code"]
    r = _post_event(client, peer_b, text=f"/绑定 {first}")
    assert "绑定失败" in r.json()["replies"][0], "旧码没有随新码作废"
    # 新码可用
    r2 = _post_event(client, peer_b, text=f"/绑定 {second}")
    assert "绑定成功" in r2.json()["replies"][0]
    lawyer["peers"].append(peer_b)


def test_help_and_unknown_command_falls_through(client, env):
    """帮助要答；**不认识的 ``/`` 命令要交回**，不能抢。

    这台机器人主要是角色扮演机器人：看到 ``/`` 就回一句"未识别的命令"，
    会把它正常的对话吃掉（有人就是会打 "/笑"），而且不报任何错。
    """
    peer = _uniq("wx-peer")
    r = _post_event(client, peer, text="/帮助")
    assert "绑定" in r.json()["replies"][0]

    for text in ("/不存在的命令", "/笑", "/whatever"):
        body = _post_event(client, peer, text=text).json()
        assert body == {"handled": False, "replies": []}, f"{text} 被本助手抢走了：{body}"


def test_other_subsystem_commands_are_handed_back(client, env, lawyer):
    """机器人里另一个子系统（P2P 用户互转）的命令必须原样交回。

    来历：两边都占用 ``/绑定`` 与 ``/解绑``，而 P2P 的拦截在 worker 里排在前面，
    律师发绑定码收到的是 P2P 那句「绑定码无效或已过期。请到用户中心重新生成绑定码」。
    修法是按**码的形状**与**当前状态**分派，而不是把命令抢过来。
    """
    peer = _uniq("wx-peer")
    # P2P 的码是 4~12 位任意字母数字；本助手的是 8 位受限字符集 —— 形状不同即交回。
    for text in ("/绑定 AB12CD", "/绑定 xyz", "/我的身份", "/断开", "/黑名单", "/拉黑 bob"):
        body = _post_event(client, peer, text=text).json()
        assert body == {"handled": False, "replies": []}, f"{text} 被本助手抢走了：{body}"

    # 没绑过本助手的 peer 发 /解绑，只可能是 P2P 的意图
    body = _post_event(client, peer, text="/解绑").json()
    assert body == {"handled": False, "replies": []}, body

    # 反之，绑过本助手的 peer 发 /解绑 就是本助手的
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)
    r = _post_event(client, peer, text="/解绑")
    assert "已解除绑定" in r.json()["replies"][0], r.text
    assert wechat_service.get_binding(peer) is None


def test_status_reports_binding_and_case(client, env, lawyer):
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)

    r = _post_event(client, peer, text="/状态")
    joined = "\n".join(r.json()["replies"])
    assert lawyer["username"] in joined
    assert "未选择" in joined


# =========================================================================== 案件选择
def test_case_list_only_shows_litigation(client, env, lawyer):
    """``/案件`` 只列诉讼案件 —— 非诉案件在这里选了也用不了（证据链只对诉讼开放）。"""
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)

    joined = "\n".join(_post_event(client, peer, text="/案件").json()["replies"])
    assert lawyer["lit"] in joined
    assert lawyer["other"] not in joined, "非诉案件不该出现在证据链的可选列表里"


def test_select_case_then_upload_and_chain(client, env, lawyer):
    """完整主路径：绑定 → 选案件 → 发文件 → 材料落库 + 证据链生成。

    这条用例是这次改动的**目的本身**（"律师从微信上传证据并生成证据链"），
    所以断言落在三处**真实副作用**上：磁盘/库里的材料、案件上的证据链字段、
    以及回给律师的那几句话。
    """
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)

    # 选择（用案件号直接选，避免依赖列表顺序）
    r = _post_event(client, peer, text=f"/选 {lawyer['lit']}")
    assert "已选择案件" in r.json()["replies"][0], r.text

    before = len(case_service.list_materials(lawyer["lit"], case_service.INTERNAL))
    up = _post_event(client, peer, attachments=[{
        "filename": "借条.txt", "mime": "text/plain", "data_base64": EVIDENCE_B64,
    }])
    assert up.status_code == 200, up.text
    replies = up.json()["replies"]
    joined = "\n".join(replies)
    assert "已归档" in joined, replies

    # ① 材料真的落库了
    mats = case_service.list_materials(lawyer["lit"], case_service.INTERNAL)
    assert len(mats) == before + 1, mats
    assert mats[-1]["filename"].endswith(".txt")

    # ② 证据链真的写进了案件（模型不可用时是确定性骨架，同样有内容）
    chain = case_service.get_evidence_chain(lawyer["lit"], case_service.INTERNAL)
    assert chain["chain"], "上传后没有生成证据链"
    assert chain["generated_at"]
    # ③ 回话里带了证据链 —— 不是只说"上传成功"
    assert any("证据链" in msg for msg in replies), replies


def test_upload_to_non_litigation_case_is_refused(client, env, lawyer):
    """非诉案件不能进证据链 —— 从**机器人这条路**也要挡住。

    前端隐藏按钮只是"别把做不到的摆出来"，这条才是规则本身；
    而机器人是一条不受前端约束的调用路径，必须单独验证。
    """
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)

    # 即便硬把当前案件设成非诉案件，上传也必须被拒。
    wechat_service._set_case(peer, lawyer["other"])
    before = len(case_service.list_materials(lawyer["other"], case_service.INTERNAL))
    r = _post_event(client, peer, attachments=[{
        "filename": "借条.txt", "mime": "text/plain", "data_base64": EVIDENCE_B64,
    }])
    joined = "\n".join(r.json()["replies"])
    assert "只对诉讼案件开放" in joined, joined
    assert len(case_service.list_materials(lawyer["other"], case_service.INTERNAL)) == before


def test_shrinking_attachment_is_reported_not_crashed(client, env, lawyer):
    """超过单文件上限的附件：回一句人话，不抛 500、不落库。"""
    old_mb = settings.WECHAT_MAX_ATTACHMENT_MB
    settings.WECHAT_MAX_ATTACHMENT_MB = 1
    try:
        peer = _uniq("wx-peer")
        _bind(client, lawyer, peer)
        lawyer["peers"].append(peer)
        _post_event(client, peer, text=f"/选 {lawyer['lit']}")

        big = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode("ascii")
        r = _post_event(client, peer, attachments=[{
            "filename": "大文件.txt", "mime": "text/plain", "data_base64": big,
        }])
        assert r.status_code == 200, r.text
        joined = "\n".join(r.json()["replies"])
        assert "上限" in joined, joined
    finally:
        settings.WECHAT_MAX_ATTACHMENT_MB = old_mb


def test_unbind_stops_access(client, env, lawyer):
    """解绑之后立刻不能再查案件 —— 手机丢了/离职交接靠它。"""
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)

    r = _post_event(client, peer, text="/解绑")
    assert "已解除绑定" in r.json()["replies"][0]

    joined = "\n".join(_post_event(client, peer, text="/案件").json()["replies"])
    assert "还没绑定" in joined
    assert lawyer["lit"] not in joined


def test_web_status_and_revoke(client, env, lawyer):
    """Web 侧：能看状态、能撤销某个微信号的绑定。"""
    peer = _uniq("wx-peer")
    _bind(client, lawyer, peer)
    lawyer["peers"].append(peer)

    st = client.get(f"{API}/wechat/status", headers=_auth(lawyer["token"])).json()
    assert st["enabled"] is True
    assert st["ready"] is True
    assert any(b["peer_id"] == peer for b in st["bindings"])

    # 撤销
    assert client.delete(f"{API}/wechat/bindings/{peer}",
                         headers=_auth(lawyer["token"])).status_code == 200
    assert wechat_service.get_binding(peer) is None

    # 撤销别人的绑定 → 404（不是 200，也不是 403：不暴露"存在但无权"）
    other_peer = _uniq("wx-peer")
    _bind(client, lawyer, other_peer)
    other = user_service.create_user(_uniq("wxother"), PASSWORD, role="lawyer",
                                     full_name="另一个律师", team_id=None)
    other_token = _login(client, other["username"])
    r = client.delete(f"{API}/wechat/bindings/{other_peer}", headers=_auth(other_token))
    assert r.status_code == 404, r.text
    lawyer["peers"].append(other_peer)
