"""接口层集成测试（TestClient + 真实关系库 + 真实 Agent 编排）。

**为什么不做 mock。** 这一层的价值几乎全部来自"接线是否真的通了"：
路由有没有注册、依赖有没有注入、服务层的异常有没有被翻译成正确的状态码、
案件隔离是在 SQL 上生效还是只在文档里写着。把 ``orchestrator`` mock 掉，
测试会全绿，但线上第一次真实调用就报 500 —— 这正是这类测试最容易骗人的地方。

代价是这些用例会**写真实的 MySQL**（``legal_agent`` 库）。所以：

- 所有造出来的数据都带随机后缀（``_uniq()``），名字里带 ``测试``前缀，
  跑多少次都不会互相干扰；
- 案件、材料、草稿在用例结束时删掉；用户只停用不删除
  （``user_service`` 的既定策略，见其 docstring）；
- **不去改 ``admin`` / ``lawyer`` 这两个种子账号的口令** ——
  改口令的用例自己新建账号来改，否则一次测试就把开发环境的登录方式改掉了。

用例的组织顺序 = 依赖顺序：系统 → 鉴权 → 案件隔离 → 材料 → 对话/任务 → 草稿 → WebSocket。
后面依赖前面的产物（``actors`` fixture 提供两个不同团队的律师）。
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import case_service, draft_service, user_service

API = "/api/v1"

# 测试账号口令。**不用出厂口令**（user_service 会拒绝把出厂口令当"已修改"），
# 也刻意满足长度要求。
PASSWORD = "Test-Pa55word!"
NEW_PASSWORD = "Test-Pa55word!-v2"


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
    return r.json()["access_token"]


# =========================================================================== 夹具
@pytest.fixture(scope="module")
def client():
    """带 lifespan 的 TestClient。

    用 ``with`` 而不是直接 ``TestClient(app)``：只有进入上下文才会跑 lifespan，
    也就是才会执行启动自检（建表、外键修复、种子账号）。
    不跑它的话，第一个用例会因为"表不存在"失败，而报错看起来像接口写错了。
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def actors(client):
    """两个不同团队的律师 + 同团队的第二名律师 + 管理员。

    **两个团队是关键**：只有一个团队时，"案件隔离生效"和"隔离完全没生效"
    这两种实现都会让用例通过（因为所有人都看得到所有人）。
    """
    team_a = user_service.create_team(_uniq("测试甲组"))
    team_b = user_service.create_team(_uniq("测试乙组"))

    alice = user_service.create_user(_uniq("alice"), PASSWORD, role="lawyer",
                                     full_name="甲组律师", team_id=team_a["id"])
    amy = user_service.create_user(_uniq("amy"), PASSWORD, role="lawyer",
                                   full_name="甲组同事", team_id=team_a["id"])
    bob = user_service.create_user(_uniq("bob"), PASSWORD, role="lawyer",
                                   full_name="乙组律师", team_id=team_b["id"])
    solo = user_service.create_user(_uniq("solo"), PASSWORD, role="lawyer",
                                    full_name="无团队律师", team_id=None)

    admin_token = _login(client, "admin", "admin123")

    out = {
        "team_a": team_a,
        "team_b": team_b,
        "alice": {"id": alice["id"], "token": _login(client, alice["username"]),
                  "username": alice["username"]},
        "amy": {"id": amy["id"], "token": _login(client, amy["username"]),
                "username": amy["username"]},
        "bob": {"id": bob["id"], "token": _login(client, bob["username"]),
                "username": bob["username"]},
        "solo": {"id": solo["id"], "token": _login(client, solo["username"]),
                 "username": solo["username"]},
        "admin": {"token": admin_token},
        "cases": [],
    }
    yield out

    # ---- 清理（尽力而为，失败不影响用例结论）----
    # AI 助手会话是**真落在同一张表里**的（``/chat/rag`` 答完就写）。
    # 不清理的话跑完测试打开助手页会看到一串测试提问 —— 软删除即可，
    # 标记删除后列表就干净了，行仍留在库里（与接口的删除语义一致）。
    for key in ("alice", "amy", "bob", "solo"):
        try:
            for cid in _conversations(client, out[key]["token"]):
                client.delete(f"{API}/chat/conversations/{cid}",
                              headers=_auth(out[key]["token"]))
        except Exception:  # noqa: BLE001
            pass
    for case_id in out["cases"]:
        try:
            case_service.delete_case(case_id, case_service.INTERNAL)
        except Exception:  # noqa: BLE001
            pass
    for key in ("alice", "amy", "bob", "solo"):
        try:
            user_service.update_user(out[key]["id"], is_active=False, team_id=None)
        except Exception:  # noqa: BLE001
            pass
    for team in (team_a, team_b):
        try:
            user_service.delete_team(team["id"])
        except Exception:  # noqa: BLE001
            pass


def _new_case(client: TestClient, token: str, *, prefix: str = "case") -> str:
    case_id = _uniq(prefix)
    r = client.post(f"{API}/cases", headers=_auth(token), json={
        "case_id": case_id, "title": f"测试案件 {case_id}", "case_type": "litigation",
        "cause_of_action": "买卖合同纠纷", "client_name": "张三",
    })
    assert r.status_code == 201, f"建案失败：{r.status_code} {r.text}"
    return case_id


# =========================================================================== 系统
def test_root_health_probe_has_no_auth_requirement(client: TestClient) -> None:
    """根路径 ``/health`` 必须无鉴权 —— 它是给 LB / K8s 探针用的。

    探针要登录的话，探针本身就成了一个新的失败点。
    """
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in ("ok", "degraded", "error")
    assert "version" in body


def test_health_exposes_schema_and_backends(client: TestClient) -> None:
    """``/health`` 必须报出「实际生效的后端」，而不是一个笼统的 ok。"""
    r = client.get(f"{API}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "database" in body and body["database"]["backend"] in ("mysql", "sqlite")
    assert "schema" in body and "stale_fks" in body["schema"]
    assert isinstance(body["backends"], dict) and "llm" in body["backends"]


def test_ready_probe_is_cheap_and_true(client: TestClient) -> None:
    r = client.get(f"{API}/health/ready")
    assert r.status_code == 200, r.text
    assert r.json()["ready"] is True


def test_openapi_covers_every_domain(client: TestClient) -> None:
    """路由注册是"接线"的一部分，最容易在重构后静默丢掉。

    这里断言的是**每个业务域至少有一条路径**，而不是断言路径总数 ——
    总数一改就红，会逼着人不断更新一个没有信息量的数字。
    """
    paths = client.get("/openapi.json").json()["paths"]
    required = [
        f"{API}/auth/login",
        f"{API}/cases",
        f"{API}/chat",
        f"{API}/chat/conversations",
        f"{API}/contract/review",
        f"{API}/tasks",
        f"{API}/drafts",
        f"{API}/knowledge/docs",
        f"{API}/admin/users",
        f"{API}/meta/version",
        f"{API}/health",
    ]
    missing = [p for p in required if p not in paths]
    assert not missing, f"这些路径没有注册：{missing}"


def test_request_id_is_echoed_and_generated(client: TestClient) -> None:
    """``X-Request-ID`` 要能透传：律师报障时报这个号，才能直接定位到哪一次请求。"""
    provided = "trace-me-12345"
    r = client.get(f"{API}/meta/version", headers={"X-Request-ID": provided})
    assert r.headers.get("X-Request-ID") == provided

    r2 = client.get(f"{API}/meta/version")
    assert r2.headers.get("X-Request-ID")  # 没传就自己生成一个


def test_validation_error_carries_field_list(client: TestClient) -> None:
    """422 要给「字段 + 原因」，而不是一层套一层的 loc 数组。"""
    r = client.post(f"{API}/auth/login", json={"username": "x"})   # 缺 password
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["fields"] and body["fields"][0]["field"] == "password"
    assert "参数校验失败" in body["detail"]


def test_meta_tools_list_is_consistent(client: TestClient) -> None:
    """工具清单是给前端与管理后台读的，必须与注册表一致且非空。"""
    r = client.get(f"{API}/meta/tools")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"], "工具清单为空 —— 注册表没建起来"
    names = {t["name"] for t in body["items"]}
    assert {"statute_search", "case_search"} <= names


# =========================================================================== 鉴权
def test_login_unknown_user_and_wrong_password_read_identically(client: TestClient) -> None:
    """两种失败必须**一字不差**。

    区分开就等于送了一个免费账号枚举接口：攻击者先摸清有哪些账号，再定向爆破。
    """
    r1 = client.post(f"{API}/auth/login", json={"username": "no_such_user_zzz", "password": "whatever1"})
    r2 = client.post(f"{API}/auth/login", json={"username": "admin", "password": "definitely-wrong"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_protected_endpoint_requires_token(client: TestClient) -> None:
    assert client.get(f"{API}/auth/me").status_code == 401
    assert client.get(f"{API}/cases").status_code == 401
    assert client.get(f"{API}/tasks").status_code == 401
    # 会话列表尤其不能匿名可读：它是"这位律师在查什么问题"
    assert client.get(f"{API}/chat/conversations").status_code == 401


def test_me_never_leaks_password_hash(client: TestClient, actors) -> None:
    """``/auth/me`` 的响应里不能出现任何口令相关字段 —— 这类泄露一旦发生就是全量。"""
    r = client.get(f"{API}/auth/me", headers=_auth(actors["alice"]["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == actors["alice"]["username"]
    text = r.text.lower()
    for bad in ("password", "hashed", "pbkdf2", "secret"):
        assert bad not in text, f"响应里出现了 {bad}"


def test_permissions_scope_differs_by_role(client: TestClient, actors) -> None:
    """非管理员是 team 范围，管理员是 all —— 前端据此决定是否显示管理入口。"""
    r_lawyer = client.get(f"{API}/auth/permissions", headers=_auth(actors["alice"]["token"]))
    r_admin = client.get(f"{API}/auth/permissions", headers=_auth(actors["admin"]["token"]))
    assert r_lawyer.status_code == r_admin.status_code == 200
    assert r_lawyer.json()["case_scope"] == "team"
    assert r_lawyer.json()["can_manage_users"] is False
    assert r_admin.json()["case_scope"] == "all"
    assert r_admin.json()["can_manage_users"] is True


def test_admin_only_endpoint_rejects_lawyer(client: TestClient, actors) -> None:
    r = client.get(f"{API}/admin/users", headers=_auth(actors["alice"]["token"]))
    assert r.status_code == 403, r.text
    r2 = client.get(f"{API}/admin/users", headers=_auth(actors["admin"]["token"]))
    assert r2.status_code == 200, r2.text


def test_login_and_me_expose_is_admin_flag(client: TestClient, actors) -> None:
    """登录与当前用户接口都必须给出 ``is_admin``。

    这条是回归守卫：前端 store 用 ``user.is_admin`` 判断要不要显示「管理后台」。
    登录响应里没有这个字段时，**管理员也看不到管理后台**，而且整条链路一声不响 ——
    `role` 字段还在，只是没人判它。只断言 `/auth/permissions` 是拦不住这个错的：
    它的 `is_admin` 是另一条代码路径（`Principal.to_dict`）。
    """
    for token, expect_admin in ((actors["admin"]["token"], True),
                               (actors["alice"]["token"], False)):
        me = client.get(f"{API}/auth/me", headers=_auth(token))
        assert me.status_code == 200, me.text
        assert me.json().get("is_admin") is expect_admin, me.json()

    login = client.post(f"{API}/auth/login",
                        json={"username": "admin", "password": "admin123"})
    assert login.status_code == 200, login.text
    user = login.json()["user"]
    assert user.get("is_admin") is True, f"登录响应缺 is_admin，前端管理入口会一直隐藏：{user}"
    assert "hashed_password" not in user and "password" not in user, "登录响应泄露了口令字段"


def test_change_password_validates_old_one(client: TestClient) -> None:
    """**改口令必须先验原口令** —— 否则令牌泄露会直接升级为账号被完全接管。

    用一个临时账号做，不动种子账号：测试跑完不该改变开发者的登录方式。
    """
    name = _uniq("pwd")
    user = user_service.create_user(name, PASSWORD, role="lawyer", full_name="改密测试")
    token = _login(client, name, PASSWORD)

    bad = client.post(f"{API}/auth/change-password", headers=_auth(token),
                      json={"old_password": "wrong-old-pass", "new_password": NEW_PASSWORD})
    assert bad.status_code == 400, bad.text
    assert client.post(f"{API}/auth/login",
                       json={"username": name, "password": PASSWORD}).status_code == 200

    ok = client.post(f"{API}/auth/change-password", headers=_auth(token),
                     json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert ok.status_code == 200, ok.text
    assert client.post(f"{API}/auth/login",
                       json={"username": name, "password": NEW_PASSWORD}).status_code == 200
    assert client.post(f"{API}/auth/login",
                       json={"username": name, "password": PASSWORD}).status_code == 401

    user_service.update_user(user["id"], is_active=False)


def test_short_password_is_rejected_with_422_field(client: TestClient, actors) -> None:
    """口令过短由 pydantic 拦在最外层，应当返回 **422 + 字段名**。

    比 400 更有用：前端能把原因直接标在"口令"这个输入框下面，
    而 400 只有一个笼统的 detail 字符串。
    """
    token = _login(client, "admin", "admin123")
    r = client.post(f"{API}/admin/users", headers=_auth(token),
                    json={"username": _uniq("short"), "password": "123"})
    assert r.status_code == 422, f"期望 422，实际 {r.status_code}：{r.text}"
    assert any(f["field"] == "password" for f in r.json()["fields"]), r.text


def test_service_error_maps_to_400_not_500(client: TestClient, actors) -> None:
    """服务层抛 ``UserError``（重名）时接口必须返回 **400 而不是 500**。

    这条用例真正防的是"异常处理器没注册"：那样会返回 500，
    而 500 的响应体不带 detail，前端只能弹一句"系统错误"。
    重名这一条走到了服务层的 ``create_user`` 里，pydantic 拦不住 ——
    正好用来验证 ``_business_error`` 这条链路是通的。
    """
    token = _login(client, "admin", "admin123")
    name = _uniq("dup")
    payload = {"username": name, "password": "Test-Pa55word!"}

    first = client.post(f"{API}/admin/users", headers=_auth(token), json=payload)
    assert first.status_code in (200, 201), first.text

    second = client.post(f"{API}/admin/users", headers=_auth(token), json=payload)
    assert second.status_code == 400, f"期望 400（业务异常），实际 {second.status_code}：{second.text}"
    assert "已存在" in second.json()["detail"], second.text

    user_service.update_user(first.json()["id"], is_active=False)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (user_service.UserError("入参不对"), 400),
        (PermissionError("无权限"), 403),
        (LookupError("找不到"), 404),
        (ValueError("兜底"), 400),
    ],
)
def test_business_exception_handler_mapping(exc: Exception, expected: int) -> None:
    """**全局异常处理器的兜底映射要单独验证。**

    绝大多数路由自己 catch 了业务异常并就地转 HTTP（见各文件的 ``_guard``），
    所以"处理器有没有注册"在集成路径上测不出来 —— 但它是唯一的安全网：
    哪天有人新写一个路由忘了 catch，没有它就会变成 500 + 无 detail。
    这里直接对处理器下断言，不绕 HTTP。

    ``asyncio.run`` 而不是 ``pytest.mark.asyncio``：这个仓库的 pytest 配置是
    ``asyncio_mode = auto``，普通同步用例里直接跑协程最省事，也不依赖插件行为。
    """
    import asyncio

    from backend.app.main import _business_error

    scope = {"type": "http", "method": "POST", "path": "/api/v1/x", "headers": []}
    request = Request(scope)
    resp = asyncio.run(_business_error(request, exc))
    assert resp.status_code == expected, f"{type(exc).__name__} -> {resp.status_code}"


# =========================================================================== 案件隔离
def test_case_create_read_and_delete(client: TestClient, actors) -> None:
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    r = client.get(f"{API}/cases/{case_id}", headers=_auth(actors["alice"]["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    # 委托人姓名会被脱敏 —— 库里存的是原文还是脱敏文本都行，但响应里不能是明文全名
    assert body["case_id"] == case_id
    assert body["owner_id"] == actors["alice"]["id"]

    r2 = client.delete(f"{API}/cases/{case_id}", headers=_auth(actors["alice"]["token"]))
    assert r2.status_code == 200, r2.text
    actors["cases"].remove(case_id)
    assert client.get(f"{API}/cases/{case_id}",
                      headers=_auth(actors["alice"]["token"])).status_code == 404


def test_case_is_isolated_between_teams(client: TestClient, actors) -> None:
    """**本项目最核心的一条安全不变式。**"""
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    same_team = client.get(f"{API}/cases/{case_id}", headers=_auth(actors["amy"]["token"]))
    assert same_team.status_code == 200, f"同团队应当可见：{same_team.text}"

    other_team = client.get(f"{API}/cases/{case_id}", headers=_auth(actors["bob"]["token"]))
    assert other_team.status_code == 403, f"跨团队应当 403，实际 {other_team.status_code}"

    # 无团队律师既不是承办人也不是同团队 → 同样拒绝
    solo = client.get(f"{API}/cases/{case_id}", headers=_auth(actors["solo"]["token"]))
    assert solo.status_code == 403, f"无团队律师应当 403，实际 {solo.status_code}"

    admin = client.get(f"{API}/cases/{case_id}", headers=_auth(actors["admin"]["token"]))
    assert admin.status_code == 200, "管理员应当可以跨团队查看"


def test_case_list_is_filtered_per_user(client: TestClient, actors) -> None:
    """列表必须在 SQL 上就加归属条件 —— 取回来再过滤的写法一旦漏一处就是全量泄露。"""
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    def _ids(token: str) -> set[str]:
        r = client.get(f"{API}/cases", headers=_auth(token), params={"limit": 200})
        assert r.status_code == 200, r.text
        return {c["case_id"] for c in r.json()["items"]}

    assert case_id in _ids(actors["alice"]["token"])
    assert case_id in _ids(actors["amy"]["token"]), "同团队应当看得到"
    assert case_id not in _ids(actors["bob"]["token"]), "跨团队不该出现在列表里"
    assert case_id in _ids(actors["admin"]["token"])


def test_case_progress_flow(client: TestClient, actors) -> None:
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    r = client.post(f"{API}/cases/{case_id}/progress", headers=_auth(actors["alice"]["token"]),
                    json={"stage": "evidence", "note": "已完成证据交换"})
    assert r.status_code == 200, r.text

    got = client.get(f"{API}/cases/{case_id}/progress", headers=_auth(actors["alice"]["token"]))
    assert got.status_code == 200
    body = got.json()
    assert body["stage"] == "evidence"
    assert any(p.get("stage") == "evidence" for p in body["items"]), body

    # 跨团队不能写进度（否则成了"绕过隔离改别人案子"）
    denied = client.post(f"{API}/cases/{case_id}/progress", headers=_auth(actors["bob"]["token"]),
                         json={"stage": "hearing", "note": "越权写入"})
    assert denied.status_code == 403, denied.text


# =========================================================================== 材料
def test_material_upload_dedupes_by_content(client: TestClient, actors) -> None:
    """同一份文件传两次应当只有一条记录（按内容摘要去重）。

    否则同一份合同会以不同 ``material_id`` 反复入库，检索时命中一堆重复片段，
    律师无法分辨"这是同一份材料的两个片段"还是"有两份相似材料"。
    """
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)
    content = f"这是一份测试合同。条款一……{uuid.uuid4().hex}".encode()

    def _upload() -> dict:
        r = client.post(
            f"{API}/cases/{case_id}/materials",
            headers=_auth(actors["alice"]["token"]),
            files={"file": ("合同.txt", content, "text/plain")},
            data={"doc_type": "contract"},
        )
        assert r.status_code == 201, f"上传失败：{r.status_code} {r.text}"
        return r.json()

    first = _upload()
    second = _upload()
    assert first["id"] == second["id"], "同内容材料被重复登记了"

    # **上传响应体也要查。** 服务端的 ``register_material`` 返回的是内部视图
    # （带 ``stored_path``，解析管线要用），接口层整包回传过一次 ——
    # 只查列表接口的话这个泄露完全测不出来。
    for resp in (first, second):
        assert "stored_path" not in resp, "上传响应泄露了服务端路径"

    listed = client.get(f"{API}/cases/{case_id}/materials",
                        headers=_auth(actors["alice"]["token"]))
    assert listed.status_code == 200
    assert listed.json()["count"] == 1, listed.text

    # 服务端内部可见的绝对路径不该出现在对外响应里
    assert "stored_path" not in listed.json()["items"][0], "对外响应泄露了服务端路径"


def test_contract_review_accepts_material_ids_with_access_control(
    client: TestClient, actors,
) -> None:
    """合同审查要能直接消费**已上传的案件材料**，且不能绕过案件隔离。

    Web/EXE 端拿不到 ``stored_path``（接口刻意不外泄），所以只能传 ``material_ids``，
    由服务端查库解析路径。这条链路如果只支持 ``material_paths``，
    "上传合同 → 一键审查"这个主场景在 Web 上根本走不通 —— 只能手工粘贴正文。

    同时必须验证：**猜到别人的 material_id 不能读别人的合同**。
    路径解析走的是 ``case_service.material_path``，权限校验在里面。
    """
    token = actors["alice"]["token"]
    case_id = _new_case(client, token)
    actors["cases"].append(case_id)

    up = client.post(
        f"{API}/cases/{case_id}/materials",
        headers=_auth(token),
        files={"file": ("合同.txt", "第一条 服务内容。第二条 价款 10 万元。".encode(), "text/plain")},
        data={"doc_type": "contract"},
    )
    assert up.status_code == 201, up.text
    mid = up.json()["id"]

    # 跨团队：403，不是"解析出路径然后照常执行"
    denied = client.post(f"{API}/contract/review", headers=_auth(actors["bob"]["token"]),
                         json={"query": "审查这份合同", "material_ids": [mid]})
    assert denied.status_code == 403, denied.text

    # 不存在的 id：404（区分开"没有权限"和"不存在"，前者不该被当成后者吞掉）
    missing = client.post(f"{API}/contract/review", headers=_auth(token),
                          json={"query": "审查", "material_ids": [999_999_999]})
    assert missing.status_code == 404, missing.text

    # 只有 material_ids 也算"提供了材料"—— 不能因为 material_paths 为空就 400
    #
    # ``no_cache: True`` 是**必须的**，不是图快：下面那句断言要证明"材料真的被读
    # 进去了"（解析出条款），而命中缓存时整条链路被短路，``clauses`` 恒为 0 ——
    # 断言必然失败。``qcache`` 是跨用例、跨进程共享的持久集合（正式跑过这个查询
    # 之后就会留下一条），所以不带这个开关时本用例的结果取决于缓存里碰巧有什么，
    # 是个会随环境飘的假失败。要验的是"读不读材料"，就该绕开缓存。
    ok = client.post(f"{API}/contract/review", headers=_auth(token),
                     json={"query": "请审查这份合同的风险点", "case_id": case_id,
                           "material_ids": [mid], "no_cache": True})
    assert ok.status_code == 200, f"{ok.status_code} {ok.text}"
    body = ok.json()
    assert body["intent"] == "contract_review", body.get("intent")
    # 材料真的被读进去了：解析出条款是"材料参与分析"的最低证据
    assert (body.get("counts") or {}).get("clauses", 0) > 0, body.get("counts")


def test_material_upload_denied_for_other_team(client: TestClient, actors) -> None:
    """**先权限校验，再写磁盘。** 反过来会在别人案件目录下留下不该存在的文件。"""
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    r = client.post(
        f"{API}/cases/{case_id}/materials",
        headers=_auth(actors["bob"]["token"]),
        files={"file": ("越权.txt", b"should not land on disk", "text/plain")},
        data={"doc_type": "other"},
    )
    assert r.status_code == 403, r.text

    listed = client.get(f"{API}/cases/{case_id}/materials",
                        headers=_auth(actors["alice"]["token"]))
    assert listed.json()["count"] == 0, "被拒的上传仍然落库了"


def test_evidence_chain_is_litigation_only(client: TestClient, actors) -> None:
    """证据链（上传证据材料 + 生成）**只对诉讼案件开放**。

    非诉/合同 与 咨询 两个入口都要挡住。界面同样整组隐藏
    （见 ``web/app.js`` 的 ``evidenceBlocks``），但那只是"别把做不到的按钮摆出来"，
    规则本身在后端 —— 直接打 API、或者开着一个改版前就加载好的旧页面，都从这里挡。

    **反向那半段不能省**：这道门修成"谁都进不来"是最坏的失败方式，
    而它恰恰不会在任何"非诉被挡住"的断言里露出来。
    """
    token = actors["alice"]["token"]
    cases: dict[str, str] = {}
    for ctype in ("litigation", "contract", "consult"):
        cid = _uniq("ev" + ctype)
        r = client.post(f"{API}/cases", headers=_auth(token), json={
            "case_id": cid, "title": f"证据链准入 {ctype}", "case_type": ctype,
        })
        assert r.status_code == 201, r.text
        actors["cases"].append(cid)
        cases[ctype] = cid

    for ctype in ("contract", "consult"):
        cid = cases[ctype]
        gen = client.post(f"{API}/cases/{cid}/evidence-chain", headers=_auth(token))
        assert gen.status_code == 400, f"{ctype} 不该能生成证据链：{gen.status_code} {gen.text}"
        assert "只对诉讼" in gen.json()["detail"], gen.text

        up = client.post(
            f"{API}/cases/{cid}/materials", headers=_auth(token),
            files={"file": ("证据.txt", "2026-01-01 借款合同一份。".encode(), "text/plain")},
            data={"doc_type": "evidence"},
        )
        assert up.status_code == 400, f"{ctype} 不该能上传证据材料：{up.status_code} {up.text}"

    # 诉讼案件照旧：材料能传、证据链能生成（模型不可用时会退化成骨架，同样是 200）
    lit = cases["litigation"]
    up = client.post(
        f"{API}/cases/{lit}/materials", headers=_auth(token),
        files={"file": ("证据.txt", "借款合同一份，载明 2026-01-01 出借 10 万元。".encode(), "text/plain")},
        data={"doc_type": "evidence"},
    )
    assert up.status_code == 201, up.text

    gen = client.post(f"{API}/cases/{lit}/evidence-chain", headers=_auth(token))
    assert gen.status_code == 200, f"诉讼案件被误伤：{gen.status_code} {gen.text}"
    assert "chain" in gen.json(), gen.text


# =========================================================================== 胜诉把握
def test_score_win_rate_is_monotonic() -> None:
    """打分必须**单调**：补证据只会让分数升，永远不该降。

    这条就是整个功能的反馈回路 —— 用户传了一份材料，数字反而掉了，
    他下次就不会再传了。所以直接打纯函数（不碰 DB、不调模型），把边界钉死。
    """
    assert case_service.score_win_rate([])["status"] == "no_chain"
    assert case_service.score_win_rate([])["score"] is None, "空链不能给 0 分"

    def one(ev, gap=""):
        return {"claim": "主张", "evidence": ev, "gap": gap}

    assert case_service.score_win_rate([one([], "缺凭证")])["score"] == 0

    indirect = case_service.score_win_rate([one([{"support": "间接"}])])["score"]
    direct = case_service.score_win_rate([one([{"support": "直接"}])])["score"]
    assert 0 < indirect < direct, (indirect, direct)

    # 再挂一条间接证据不该把已有直接证据那条拖下来
    mixed = case_service.score_win_rate([one([{"support": "间接"}, {"support": "直接"}])])
    assert mixed["score"] == direct, mixed

    # 缺口是打折不是清零：填掉缺口分数必须升
    gapped = case_service.score_win_rate([one([{"support": "直接"}], "缺合同")])["score"]
    assert 0 < gapped < direct, (gapped, direct)

    # 逐条补证，全程单调不减
    chain = [one([], "缺证据") for _ in range(5)]
    prev = case_service.score_win_rate(chain)["score"]
    assert prev == 0, prev
    for c in chain:
        c["evidence"] = [{"support": "间接"}]
        cur = case_service.score_win_rate(chain)["score"]
        assert cur >= prev, f"补了证据反而掉分：{prev} -> {cur}"
        prev = cur
        c["gap"] = ""
        cur = case_service.score_win_rate(chain)["score"]
        assert cur >= prev, f"填了缺口反而掉分：{prev} -> {cur}"
        prev = cur
    # 五条都补上间接证据、缺口也填了 = 单条间接证据那个分数
    assert prev == indirect, (prev, indirect)


def test_win_assessment_is_litigation_only(client: TestClient, actors) -> None:
    """胜诉把握评估**只对诉讼案件开放**，与证据链同一道闸门。

    「非诉被挡住」那半段之外，**反向那半段不能省**（见证据链那条用例的说明）：
    这道门修成"谁都进不来"不会在任何一条断言里露出来。
    数字是纯代码算的，所以模型不可用时也必须给得出结果、不能 500。
    """
    token = actors["alice"]["token"]
    cases: dict[str, str] = {}
    for ctype in ("litigation", "contract", "consult"):
        cid = _uniq("win" + ctype)
        r = client.post(f"{API}/cases", headers=_auth(token), json={
            "case_id": cid, "title": f"胜诉把握准入 {ctype}", "case_type": ctype,
            "summary": "2023 年 8 月被以不胜任工作为由辞退，未安排培训或调岗。",
        })
        assert r.status_code == 201, r.text
        actors["cases"].append(cid)
        cases[ctype] = cid

    for ctype in ("contract", "consult"):
        r = client.post(f"{API}/cases/{cases[ctype]}/win-assessment", headers=_auth(token))
        assert r.status_code == 400, f"{ctype} 不该能生成胜诉评估：{r.status_code} {r.text}"
        assert "只对诉讼" in r.json()["detail"], r.text

    lit = cases["litigation"]
    # 还没生成证据链：**不给 0 分**，而是明确说"还算不了"
    got = client.get(f"{API}/cases/{lit}/win-assessment", headers=_auth(token))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["supported"] is True and body["status"] == "no_chain", body
    assert body["score"] is None, f"空链不该给数字：{body}"
    assert body["disclaimer"], "免责声明不能省"

    # 传材料 + 生成链（模型不可用时会退化成骨架，同样是 200）
    up = client.post(
        f"{API}/cases/{lit}/materials", headers=_auth(token),
        files={"file": ("考勤.txt", "2023 年 1-8 月考勤记录，工作日 21 点后打卡。".encode(), "text/plain")},
        data={"doc_type": "evidence"},
    )
    assert up.status_code == 201, up.text
    assert client.post(f"{API}/cases/{lit}/evidence-chain",
                       headers=_auth(token)).status_code == 200

    out = client.post(f"{API}/cases/{lit}/win-assessment", headers=_auth(token))
    assert out.status_code == 200, f"诉讼案件被误伤：{out.status_code} {out.text}"
    got = out.json()
    assert got["supported"] is True, got

    # 接口必须忠实反映纯函数的结果 —— 不断言具体分值：
    # 链的内容会随模型/降级骨架两条路径漂移，写死分值等于每次调权重都变红。
    chain = client.get(f"{API}/cases/{lit}/evidence-chain",
                       headers=_auth(token)).json()["chain"]
    expected = case_service.score_win_rate(chain)
    assert got["status"] == expected["status"], (got, expected)
    assert got["score"] == expected["score"], (got, expected)
    # 数字是现算不落库的，两次读必须一致
    again = client.get(f"{API}/cases/{lit}/win-assessment", headers=_auth(token)).json()
    assert again["score"] == got["score"], "数字是现算的，两次读应当一致"
    # 模型那半可以降级，但四个列表的结构必须在
    assert set(case_service.WIN_FIELDS) <= set(again["analysis"] or {}) or not again["stored_at"], again


def test_stage_advance_moves_case_to_hearing_and_dashboard(client: TestClient, actors) -> None:
    """「推进到开庭审理」：案件阶段要真的变，工作台那张卡要跟着 +1。

    那张卡此前恒为 0 —— 不是统计写错了，而是**全库没有一件案子到过 hearing**：
    后端 ``STAGES`` 里早就有它、``add_progress`` 也早就会改 ``case.stage``，
    但唯一的入口是新建案件弹窗里那个下拉，建完就再也改不动了。
    """
    token = actors["alice"]["token"]
    case_id = _new_case(client, token, prefix="hear")
    actors["cases"].append(case_id)

    def hearing_count() -> int:
        r = client.get(f"{API}/cases/stats/summary", headers=_auth(token))
        assert r.status_code == 200, r.text
        return int((r.json().get("by_stage") or {}).get("hearing") or 0)

    before = hearing_count()

    r = client.post(f"{API}/cases/{case_id}/progress", headers=_auth(token),
                    json={"stage": "hearing", "note": "推进到开庭审理"})
    assert r.status_code == 200, r.text

    detail = client.get(f"{API}/cases/{case_id}", headers=_auth(token)).json()
    assert detail["stage"] == "hearing", detail
    assert hearing_count() == before + 1, "工作台「开庭审理阶段」卡没跟着变"

    # 详情页的阶段与流水同源，别只改了一个
    items = client.get(f"{API}/cases/{case_id}/progress", headers=_auth(token)).json()["items"]
    assert any(p.get("stage") == "hearing" for p in items), items

    # 非法阶段必须被挡在门外。以前它是**静默地只记流水、不动阶段** ——
    # 界面提示"已记录"、用户以为推进成功了，其实没有；
    # 库里那两行中文 stage「分析完成」就是这么进来的。
    bad = client.post(f"{API}/cases/{case_id}/progress", headers=_auth(token),
                      json={"stage": "分析完成", "note": "脏数据"})
    assert bad.status_code == 422, f"非法阶段应当被拒，实际 {bad.status_code}：{bad.text}"
    assert client.get(f"{API}/cases/{case_id}",
                      headers=_auth(token)).json()["stage"] == "hearing", "非法写入不该改动阶段"


# =========================================================================== 对话与任务
def test_contract_review_refuses_without_material(client: TestClient, actors) -> None:
    """**必须拒绝，而不是返回一份通用风险清单。**

    不校验时接口会"正常返回"一份命中全部检查项的报告，看起来完整但与合同无关，
    律师很可能直接交给客户 —— 一份看起来对的空报告比明确的 400 更危险。
    """
    r = client.post(f"{API}/contract/review", headers=_auth(actors["alice"]["token"]),
                    json={"query": "帮我审查这份合同"})
    assert r.status_code == 400, f"应当拒绝空材料，实际 {r.status_code}：{r.text}"
    assert "合同" in r.json()["detail"]


def test_chat_produces_persisted_task_with_trace_and_citations(client: TestClient, actors) -> None:
    """跑一次真实流程，然后验证**落库的产物**都能查回来。

    这里刻意不 mock 编排：这条用例的价值就是"整条链路是否真的通了"。
    """
    r = client.post(f"{API}/chat", headers=_auth(actors["alice"]["token"]), json={
        "query": "员工入职三个月未签书面劳动合同，能否主张二倍工资？",
        "case_id": "", "doc_type": "consult",
    })
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    task_id = body["task_id"]
    assert task_id, body

    # 任务详情（含 Trace）
    got = client.get(f"{API}/tasks/{task_id}", headers=_auth(actors["alice"]["token"]))
    assert got.status_code == 200, got.text
    task = got.json()
    assert task["status"] in ("succeeded", "degraded", "failed")

    # 列表里也要有它（列表与详情读的是同一张表，但走的是不同查询路径）
    listed = client.get(f"{API}/tasks", headers=_auth(actors["alice"]["token"]),
                        params={"limit": 200})
    assert listed.status_code == 200
    assert task_id in {t["task_id"] for t in listed.json()["items"]}

    # 引用查询：即使这次没有任何可采信引用，接口也必须是"正常返回空列表 + 说明"，
    # 而不是报错（空引用是合法结果，说明的是检索没命中，不是接口坏了）
    cites = client.get(f"{API}/tasks/{task_id}/citations", headers=_auth(actors["alice"]["token"]))
    assert cites.status_code == 200, cites.text
    assert "items" in cites.json()

    # 证据缺口：只读接口，不能因为 state 里没有 gap 就 500
    gaps = client.get(f"{API}/tasks/{task_id}/gaps", headers=_auth(actors["alice"]["token"]))
    assert gaps.status_code == 200, gaps.text
    assert "items" in gaps.json() or "gaps" in gaps.json()


def test_task_is_visible_only_to_owner_team_and_admin(client: TestClient, actors) -> None:
    """任务可见性与案件同一套规则（管理员 / 本人 / 本团队）。"""
    token = actors["admin"]["token"]
    r = client.post(f"{API}/chat", headers=_auth(token), json={
        "query": "诉讼时效期间是多久？", "doc_type": "consult",
    })
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    assert client.get(f"{API}/tasks/{task_id}",
                      headers=_auth(token)).status_code == 200
    assert client.get(f"{API}/tasks/{task_id}",
                      headers=_auth(actors["alice"]["token"])).status_code == 403, \
        "别的团队的律师看到了管理员发起的任务"


def test_task_not_found_returns_404(client: TestClient, actors) -> None:
    r = client.get(f"{API}/tasks/task_does_not_exist_{uuid.uuid4().hex[:6]}",
                   headers=_auth(actors["alice"]["token"]))
    assert r.status_code == 404, r.text


def test_chat_stream_emits_sse_frames(client: TestClient, actors) -> None:
    """SSE 至少要出一条 ``meta`` 和一条终结事件（``done`` / ``error``）。

    只断言"有 meta 且有终结事件"而不是断言事件条数：条数取决于编排跑了几个节点，
    硬编码条数会让用例在每次调整流程后变红，而它真正该防的是"流根本没建立"。
    """
    with client.stream("POST", f"{API}/chat/stream",
                       headers=_auth(actors["alice"]["token"]),
                       json={"query": "什么是不可抗力？", "doc_type": "consult"}) as resp:
        assert resp.status_code == 200, resp.text
        raw = "".join(resp.iter_text())

    assert "event: meta" in raw, raw[:500]
    assert ("event: done" in raw) or ("event: error" in raw), raw[-500:]


def test_chat_stream_meta_carries_task_id(client: TestClient, actors) -> None:
    """``meta`` 必须带 ``task_id``，而且要在**第一条**就给。

    客户端靠它做两件事：断流后回任务中心找回结果、把界面进度与库里那条任务
    对上号。只在 ``done`` 事件里给是不够的 —— 任务跑到一半连接断了，
    那时正是最需要它的时候，却拿不到。

    这条是回归守卫：``_sse_stream`` 的注释早就写着"前端才能立刻拿到
    task_id"，而实际发了 ``client`` / ``username`` / ``intent`` 三样、
    唯独没有 ``task_id``。注释与代码不符时，按注释写客户端的人会一直拿到
    undefined，且没有任何报错。
    """
    import json

    with client.stream("POST", f"{API}/chat/stream",
                       headers=_auth(actors["alice"]["token"]),
                       json={"query": "试用期最长多久？", "doc_type": "consult"}) as resp:
        assert resp.status_code == 200, resp.text
        lines = []
        for line in resp.iter_lines():
            lines.append(line)
            if line.startswith("data:") and len(lines) > 1:
                break

    meta_line = next((l for l in lines if l.startswith("data:")), "")
    meta = json.loads(meta_line[5:].strip())
    assert meta.get("task_id", "").startswith("task_"), f"meta 缺 task_id：{meta}"


def test_tool_body_flattens_tool_dict_not_its_keys() -> None:
    """``_tool_body`` 必须把工具 ``data`` 里的**命中列表**放进 ``items``。

    这条是回归守卫：工具返回的 ``data`` 是 dict，
    ``list(data)`` 拿到的是**键名**（``["query", "count", "statutes", "trace"]``）。
    写成那样时接口仍然返回 200、``count`` 也正确，一条命中都没有却不报错，
    前端只会渲染出一排字段名 —— 只写 ``isinstance(items, list)`` 的断言拦不住。
    """
    from backend.app.api.v1.chat import _tool_body
    from backend.tools.base import ToolResult

    hits = [{"doc_name": "《中华人民共和国民法典》", "article_no": "第577条"}]
    result = ToolResult(
        ok=True, tool="statute_search", degraded=False,
        data={"query": "违约", "count": 1, "statutes": hits,
              "exact_requests": [], "exact_hits": [], "trace": {"vector_hits": 3}},
    )
    body = _tool_body(result)
    assert body["items"] == hits
    assert body["count"] == 1
    # 工具原始字段也要透出：前端按类型分别渲染，并需要 trace 里的降级说明。
    assert body["statutes"] == hits

    # 无 data / data 是裸 list / data 是别的形状时都不该崩，且 items 恒为 list。
    for data in (None, [], [{"a": 1}], {"unknown": [1, 2]}, "text"):
        b = _tool_body(ToolResult(ok=False, tool="case_search", data=data))
        assert isinstance(b["items"], list), data
        assert all(isinstance(x, dict) for x in b["items"]), data


def test_statute_search_returns_tool_body(client: TestClient, actors) -> None:
    """纯检索通道：``degraded`` / ``fallback_note`` 必须原样透出。

    降级了却看起来像正常结果，是法律场景最不该发生的事。
    """
    r = client.post(f"{API}/search/statute", headers=_auth(actors["alice"]["token"]),
                    json={"query": "劳动合同 二倍工资", "top_k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == "statute_search"
    assert isinstance(body["items"], list)
    assert "degraded" in body and "fallback_note" in body
    # 每一条命中必须是字典。元素是字符串就说明把 ``data`` 的键当成了结果。
    for item in body["items"]:
        assert isinstance(item, dict), f"命中项不是字典：{item!r}（疑似返回了字段名清单）"


def test_search_material_requires_case_access(client: TestClient, actors) -> None:
    """材料检索必须先过案件权限，否则它就成了绕过案件隔离的读取通道。"""
    case_id = _new_case(client, actors["alice"]["token"])
    actors["cases"].append(case_id)

    ok = client.post(f"{API}/search/material", headers=_auth(actors["alice"]["token"]),
                     json={"query": "条款", "case_id": case_id, "top_k": 3})
    assert ok.status_code == 200, ok.text

    denied = client.post(f"{API}/search/material", headers=_auth(actors["bob"]["token"]),
                         json={"query": "条款", "case_id": case_id, "top_k": 3})
    assert denied.status_code == 403, denied.text

    no_case = client.post(f"{API}/search/material", headers=_auth(actors["alice"]["token"]),
                          json={"query": "条款", "top_k": 3})
    assert no_case.status_code == 400, no_case.text


# =========================================================================== 草稿
def test_draft_crud_and_markdown_export(client: TestClient, actors) -> None:
    """草稿：新建 → 改版自增 → 列表 → 导出 md。

    **改版必须新增版本而不是覆盖** —— 覆盖会让"上次导出的是哪一版"无从判断。
    """
    token = actors["alice"]["token"]
    created = client.post(f"{API}/drafts", headers=_auth(token), json={
        "content": "# 民事起诉状\n\n原告：张三\n\n诉讼请求：……",
        "doc_type": "complaint",
        "title": _uniq("测试起诉状"),
        "citations": [{"article_no": "《民法典》第一百八十八条"}],
    })
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]
    v1 = created.json()["version"]

    updated = client.put(f"{API}/drafts/{draft_id}", headers=_auth(token),
                         json={"content": "# 民事起诉状\n\n原告：张三\n\n诉讼请求：……（补充）"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] > v1, "改版没有自增版本号（会覆盖旧版）"

    listed = client.get(f"{API}/drafts", headers=_auth(token), params={"limit": 200})
    assert listed.status_code == 200
    assert draft_id in {d["draft_id"] for d in listed.json()["items"]}

    exported = client.post(f"{API}/drafts/{draft_id}/export", headers=_auth(token),
                           params={"fmt": "md"})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["filename"].endswith(".md"), body
    assert body["size_bytes"] > 0, "导出报告写了 0 字节"
    # **导出回执不得带服务端路径。** `path` 会泄露部署目录与用户名，
    # 而 Web 端拿到它什么也做不了 —— 下文件走 `/download`。
    assert "path" not in body, f"导出回执泄露了服务端路径：{body.get('path')}"
    assert "exported_path" not in body.get("record", {}), body.get("record")

    # 导出后草稿上要记下"上次导出的是哪个文件"，否则界面分不清"看不到的是哪一版"
    got = client.get(f"{API}/drafts/{draft_id}", headers=_auth(token))
    assert got.status_code == 200, got.text
    detail = got.json()
    assert detail["exported"] is True, "导出没有回写导出状态"
    assert detail["exported_name"].endswith(".md"), detail

    # 列表与详情都不许带磁盘路径（`exported_path` 是 DB 列名，不是接口字段）。
    # 只查列表查不出来：导出回执、详情、列表是三处独立的响应体。
    rows = [d for d in listed.json()["items"] if d["draft_id"] == draft_id]
    assert rows, "列表里找不到刚建的草稿"
    for resp_body in (detail, rows[0]):
        assert "exported_path" not in resp_body, f"草稿响应泄露了磁盘路径：{resp_body}"
        assert "stored_path" not in resp_body, resp_body

    # 导出物要能真的下载下来（否则 Web 端拿到路径也没用）
    dl = client.get(f"{API}/drafts/{draft_id}/download", headers=_auth(token))
    assert dl.status_code == 200, dl.text
    assert dl.content.decode("utf-8").startswith("# 民事起诉状")

    # 别人的草稿不可见（不可读、不可导出）
    denied = client.get(f"{API}/drafts/{draft_id}", headers=_auth(actors["bob"]["token"]))
    assert denied.status_code in (403, 404), denied.text

    assert client.delete(f"{API}/drafts/{draft_id}",
                         headers=_auth(token)).status_code == 200


def test_draft_download_before_export_reports_404(client: TestClient, actors) -> None:
    """没导出过就下载 —— 必须 404 且说清原因，而不是抛 500 或返回空文件。"""
    token = actors["alice"]["token"]
    created = client.post(f"{API}/drafts", headers=_auth(token), json={
        "content": "未导出的草稿", "doc_type": "opinion", "title": _uniq("未导出"),
    })
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]

    r = client.get(f"{API}/drafts/{draft_id}/download", headers=_auth(token))
    assert r.status_code == 404, r.text
    assert "导出" in r.json()["detail"]

    client.delete(f"{API}/drafts/{draft_id}", headers=_auth(token))


def test_draft_docx_export_never_silently_falls_back(client: TestClient, actors) -> None:
    """未装 python-docx 时应当明确 501，**不能悄悄导出 .md 却顶着 .docx 扩展名**。

    装了的话就走通并产出文件 —— 两种情况都断言，避免这条用例在装了依赖的机器上
    变成"必然跳过"的死用例。
    """
    token = actors["alice"]["token"]
    created = client.post(f"{API}/drafts", headers=_auth(token), json={
        "content": "测试内容", "doc_type": "opinion", "title": _uniq("测试意见书"),
    })
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]

    r = client.post(f"{API}/drafts/{draft_id}/export", headers=_auth(token),
                    params={"fmt": "docx"})
    if r.status_code == 501:
        assert "python-docx" in r.json()["detail"]
    else:
        assert r.status_code == 200, r.text
        assert str(r.json()["filename"]).endswith(".docx")

    client.delete(f"{API}/drafts/{draft_id}", headers=_auth(token))


# =========================================================================== 纯 RAG 问答
class _FakeLLM:
    """替掉 ``llm_client.chat``。

    **只替模型、不替检索。** 检索栈（Milvus + BM25 + RRF）就在本机，跑真实的一条
    才验得到"这个端点真的接在 RAG 上"；而模型是计费的外部网络调用、单次上百秒、
    输出还不确定，让它进测试等于把用例挂在一个别人家的服务上。
    所以这一层替的是一条**外部边界**，不是本项目自己的接线。
    """

    text = "根据检索到的条文，用人单位违法解除应当支付赔偿金。"
    provider = "fake"
    degraded = False

    def __init__(self, text: str = "") -> None:
        if text:
            self.text = text
        self.seen: list[dict] = []

    async def chat(self, msgs, **kw):  # noqa: ANN001, ANN003
        self.seen.append({"msgs": msgs, "kw": kw})
        return self


@pytest.fixture()
def fake_llm(monkeypatch):
    """把模型调用换成假的，返回那个假对象（可读 ``seen`` 核对发出去的消息）。"""
    from backend.app.services import rag_answer

    fake = _FakeLLM()
    monkeypatch.setattr(rag_answer.llm_client, "chat", fake.chat)
    return fake


def test_rag_chat_endpoint_shape_and_no_path_leak(client: TestClient, actors, fake_llm) -> None:
    """``/chat/rag`` 的返回体形状 + **不泄漏任何服务端路径**。

    这一层不 mock 检索：路由注册、依赖注入、服务层异常翻译成状态码、
    ``source_path`` 有没有被白名单拦在外面 —— 这些只有跑真链路才验得到。
    而 ``source_path`` 是 ``StatuteHit`` / ``CaseHit`` 自带的字段，
    只要序列化那一步漏了白名单，它会**安静地**出现在响应里，本地单测发现不了。
    """
    token = actors["alice"]["token"]
    r = client.post(f"{API}/chat/rag", headers=_auth(token),
                    json={"query": "公司违法辞退我，2N赔偿怎么计算？", "top_k": 5})

    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("answer", "citations", "ref_check", "retrieval", "disclaimer", "elapsed_ms"):
        assert key in body, f"返回体缺 {key}：{list(body)}"
    assert body["answer"], "答案不能是空的"
    assert body["disclaimer"] in body["answer"], "免责声明必须落在正文里"
    assert isinstance(body["citations"], list)
    assert {"total", "backfilled", "unsupported", "ok"} <= set(body["ref_check"])

    # 第 13 个键 ``conversation_id`` **恒存在**：答完就顺手落一段会话（见 ``chat.py`` 的落库段）。
    # 这里断言的是**真的写进去了**而不是"字段在就行" —— 新表忘了在
    # ``db/models/__init__.py`` 里 import 时 ``create_all`` 会静默不建表，
    # 落库失败被 ``except`` 兜住，响应里就是空串，看上去一切正常。
    assert "conversation_id" in body, f"返回体缺 conversation_id：{list(body)}"
    assert body["conversation_id"].startswith("conv_"), (
        f"这一轮没有落库（conversation_id={body['conversation_id']!r}）—— "
        "新表可能没建起来，看后端日志里的「RAG 会话落库失败」"
    )

    # ``source_path`` 会暴露服务端目录结构。查响应原文而不是某个字段 ——
    # 只要它出现在任意一层（含 errors / notes 里的字符串），这条就该红。
    assert "source_path" not in r.text, f"响应里泄漏了 source_path：{r.text[:500]}"
    for leak in ("data/raw", "\\\\raw\\\\", "/app/data"):
        assert leak not in r.text, f"响应里出现了疑似路径片段 {leak!r}"

    # 发出去的必须带 RAG 契约（四节结构 + 报告脚手架的禁令），不是光秃秃的通用纪律
    system = fake_llm.seen[0]["msgs"][0]["content"]
    assert "（一）核心结论" in system and "引用清单" in system, "端点没接上 RAG 专用提示词"


def test_rag_chat_does_not_write_a_task(client: TestClient, actors, fake_llm) -> None:
    """**``/chat/rag`` 不写 ``la_task``。** 这条钉的是一个有意的取舍。

    接了任务表就等于把 orchestrator 的语义又搬回来一遍（任务中心、进度轮询、
    相似问题缓存都要跟着长出来），而那正是这个入口要摆脱的东西。
    将来有人"顺手"把它接上去，任务中心会多出一批点进去什么都没有的记录，
    而这条用例会在那之前先红。
    """
    # 注意取的是**实例**：``backend.app.services.task_service`` 是个模块，
    # 模块里那个同名对象才是单例。``from ... import task_service`` 拿到的是模块，
    # 调 ``.count`` 会 AttributeError —— 第一次就是这么写错的。
    from backend.app.services.task_service import task_service

    token = actors["alice"]["token"]
    before = task_service.count(user_id=actors["alice"]["id"])

    r = client.post(f"{API}/chat/rag", headers=_auth(token),
                    json={"query": "试用期最长可以约定多久？"})
    assert r.status_code == 200, r.text

    after = task_service.count(user_id=actors["alice"]["id"])
    assert after == before, (
        f"``/chat/rag`` 写了任务表（{before} → {after}）—— "
        "这个入口的定位是「随手问一句」，需要留痕的提问应当走 ``/chat``。"
    )


def test_rag_stream_emits_stage_never_trace(client: TestClient, actors, fake_llm) -> None:
    """SSE 首帧 ``meta``、末帧 ``done``，且**全程没有 ``trace``**。

    事件名是 ``stage`` 不是 ``trace``：``trace`` 在本项目里专指 LangGraph 的**节点**事件，
    任务详情（``/tasks/{id}``）与桌面端都把它当节点列表展示 ——
    这条链路上根本没有节点，发 ``trace`` 等于凭空编出一串不存在的执行节点。
    """
    token = actors["alice"]["token"]
    r = client.post(f"{API}/chat/rag/stream", headers=_auth(token),
                    json={"query": "竞业限制补偿金的最低标准是多少？"})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = re.findall(r"^event: (\w+)$", r.text, flags=re.M)

    assert events, f"SSE 没有发出任何命名事件：{r.text[:400]}"
    assert events[0] == "meta", f"首帧必须是 meta（前端靠它初始化）：{events}"
    assert events[-1] == "done", f"末帧必须是 done：{events}"
    assert "error" not in events, f"流里出现了 error：{r.text[:800]}"
    assert "trace" not in events, (
        f"出现了 trace 事件 —— 前端会把它当 LangGraph 节点画出来：{events}"
    )
    assert "stage" in events, f"没有 stage 事件，检索进度推不出来：{events}"


# =========================================================================== AI 助手会话
def _conversations(client: TestClient, token: str, **params) -> dict[str, dict]:
    """会话列表 → ``{conversation_id: 列表项}``。多次用到，省得每处都写一遍解包。"""
    r = client.get(f"{API}/chat/conversations", headers=_auth(token),
                   params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return {it["conversation_id"]: it for it in r.json()["items"]}


def _ask(client: TestClient, token: str, query: str, conversation_id: str = "") -> dict:
    r = client.post(f"{API}/chat/rag", headers=_auth(token),
                    json={"query": query, "conversation_id": conversation_id})
    assert r.status_code == 200, r.text
    return r.json()


def _nan_free(value):
    """把 NaN 换成 None，好让"响应里的"与"库里存的"能逐项比。

    落库那一刻过 ``_json_safe`` 做了同样的替换，所以对比前要先抹平 ——
    否则检索一降级（``score`` 是 NaN）这条用例就红，而它红得毫无信息量。
    """
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, dict):
        return {k: _nan_free(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nan_free(v) for v in value]
    return value


def test_conversation_saved_listed_replayed(client: TestClient, actors, fake_llm) -> None:
    """提问 → 自动落库 → 列表可见 → 详情能完整重放 → 追问接在同一段上。

    这条走的是**真链路**（真检索、真落库、假模型），因为它要验的正是接线：
    落库位置对不对、``done`` 帧带回的会话号能不能用于下一轮、
    存下去的 payload 够不够前端完整重渲染。
    """
    token = actors["alice"]["token"]

    first = _ask(client, token, "违法辞退的赔偿金怎么算？")
    cid = first["conversation_id"]
    assert cid.startswith("conv_"), f"提问没有落库（conversation_id={cid!r}）"

    # 标题 = 首句截断，不调模型、不让用户改名
    rows = _conversations(client, token)
    assert cid in rows, "刚问完的会话不在列表里 —— 落库没落在 done 帧之前？"
    assert rows[cid]["title"] == "违法辞退的赔偿金怎么算", rows[cid]

    # 详情回的是**整份** payload：citations 丢一个，正文里的 [S1] 就点不开，
    # 而且不会报错 —— 只是静默变成死文本，所以这里逐项比对。
    detail = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(token))
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["ok"] is True and d["count"] == 1 and d["status"] == "active"
    turn = d["turns"][0]
    assert turn["query"] == first["query"]
    assert turn["answer"] == first["answer"]
    assert turn["elapsed_ms"] == first["elapsed_ms"]
    # 逐项比对时先抹平 NaN：检索降级时 ``score`` 可能是 NaN，
    # 而落库那一刻会被换成 None（见 ``_json_safe``）。直接写 ``==`` 会随机红，
    # 且 ``nan != nan`` 连"两边都是 NaN"都会判不等。
    for key in ("citations", "ref_check", "retrieval"):
        assert _nan_free(turn[key]) == _nan_free(first[key]), (
            f"{key} 没能原样存下来 —— citations 丢了正文里的 [S1] 会点不开，"
            "ref_check 丢了「查无此条」的告警会消失，都不报错"
        )
    # 换到 MySQL 就炸的那类：JSON 列拒收裸 NaN，所以落库前必须已经清干净。
    # allow_nan=True 是为了让它**写得出**这三个字面量，才查得到。
    raw = json.dumps(turn, ensure_ascii=False, allow_nan=True)
    for bad in ("NaN", "Infinity"):   # -Infinity 里也含 Infinity，一并能拦住
        assert bad not in raw, (
            f"落库的 payload 里还留着 {bad} —— SQLite 存得下，MySQL 的 JSON 列会直接拒收"
        )

    # 追问：带上会话号 → 接在同一段的第 2 轮
    second = _ask(client, token, "那医疗期呢？", conversation_id=cid)
    assert second["conversation_id"] == cid, "追问开了一段新会话（历史里会碎成两条）"
    d2 = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(token)).json()
    assert d2["count"] == 2, d2
    assert [t["query"] for t in d2["turns"]] == [first["query"], "那医疗期呢？"]
    assert d2["turns"][1]["answer"], "第 2 轮的答案没存下来"
    assert d2["title"] == "违法辞退的赔偿金怎么算", "追问把标题改掉了（标题只认首轮）"
    # 列表时间要跟着最后一次提问走，否则聊了一下午的会话会永远排在最底下。
    # 用 >= 而不是 >：SQLite 的 CURRENT_TIMESTAMP 只到秒，同一秒内的两次提问
    # 时间戳相同，严格的 > 会随机红。
    assert d2["updated_at"] >= d["updated_at"], (d2["updated_at"], d["updated_at"])

    assert client.delete(f"{API}/chat/conversations/{cid}",
                         headers=_auth(token)).status_code == 200


def test_conversation_is_private_per_user(client: TestClient, actors, fake_llm) -> None:
    """会话按 ``user_id`` 隔离：读、写、删三条路径都不能越过。

    会话记录泄露的不是"别人的文书"，而是**"别人在查什么问题"** ——
    对律所来说这本身就是敏感信息（谁在碰哪个案子、哪类纠纷）。
    所以这里三个方向都断言，且要求 403 与 404 **分得开**：
    越权给 404 会让用户在"这段会话是不是被我自己删了"上瞎猜。
    """
    alice, bob = actors["alice"]["token"], actors["bob"]["token"]

    cid = _ask(client, alice, "竞业限制补偿金的最低标准是多少？")["conversation_id"]

    # 读：别人的会话不可见
    denied = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(bob))
    assert denied.status_code == 403, denied.text
    assert denied.json()["kind"] == "ChatHistoryForbidden", denied.json()
    # 与「不存在」必须给不同的码
    missing = client.get(f"{API}/chat/conversations/conv_nosuch00000", headers=_auth(bob))
    assert missing.status_code == 404, missing.text
    assert missing.json()["kind"] == "ChatHistoryNotFound", missing.json()

    # 删：别人的会话删不掉
    assert client.delete(f"{API}/chat/conversations/{cid}",
                         headers=_auth(bob)).status_code == 403

    # 写：**越权追加不能成功，但也不许报错**。报错会丢掉一个已经跑完的答案，
    # 所以处置是"另起一段"—— 关键是绝不能写进别人的会话里。
    before = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(alice)).json()["count"]
    injected = _ask(client, bob, "塞一条到别人的会话里", conversation_id=cid)
    assert injected["conversation_id"] != cid, "把轮次写进了别人的会话"
    assert injected["conversation_id"].startswith("conv_"), "越权追加应当降级为新建一段"
    after = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(alice)).json()["count"]
    assert after == before, f"别人的会话被追加了内容（{before} → {after}）"

    # 列表：默认只看自己；``all_users`` 对非管理员**不生效**（接口层按 is_admin 收口）
    assert cid not in _conversations(client, bob), "会话列表泄漏了别人的问答记录"
    assert cid not in _conversations(client, bob, all_users=True), (
        "非管理员传 all_users=true 就看到了全所会话 —— 越权开关"
    )

    # 管理员是唯一能跨用户查的角色（``all_users`` 只在 is_admin 时把过滤置空）
    assert cid in _conversations(client, actors["admin"]["token"], all_users=True)

    client.delete(f"{API}/chat/conversations/{cid}", headers=_auth(alice))


def test_conversation_soft_delete_and_revive(client: TestClient, actors, fake_llm) -> None:
    """删除是**标记删除**：行留在库里，``status`` 改成 ``deleted``；再追问则整段复活。

    为什么不做物理删除：会话是"这位律师问过什么"的凭据，而删除是用户可发起的、
    不可逆的语义变更。真要清理只能由 DBA 走库 —— 与审计表「只插不删」同调性。
    """
    token = actors["alice"]["token"]
    cid = _ask(client, token, "试用期最长可以约定多久？")["conversation_id"]
    _ask(client, token, "那试用期工资呢？", conversation_id=cid)

    deleted = client.delete(f"{API}/chat/conversations/{cid}", headers=_auth(token))
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["status"] == "deleted" and body["turns"] == 2, body
    assert "path" not in body, f"删除回执泄露了服务端路径：{body}"

    # 删完就查不到、也不在列表里 —— 但**库里还在**
    assert client.get(f"{API}/chat/conversations/{cid}",
                      headers=_auth(token)).status_code == 404
    assert cid not in _conversations(client, token)

    from sqlalchemy import select as sa_select

    from backend.app.db.models import ChatTurn
    from backend.app.db.session import session_scope

    with session_scope() as db:
        rows = db.execute(
            sa_select(ChatTurn).where(ChatTurn.conversation_id == cid)
        ).scalars().all()
        assert len(rows) == 2, "标记删除把行真删了 —— 无法追溯这位律师问过什么"
        assert {r.status for r in rows} == {"deleted"}, [(r.turn_index, r.status) for r in rows]

    # 重复删除幂等：前端的删除按钮可能点两次，第二次弹"不存在"会让用户以为删错了东西
    again = client.delete(f"{API}/chat/conversations/{cid}", headers=_auth(token))
    assert again.status_code == 200, again.text
    assert again.json()["already"] is True, again.json()

    # 在已删除的会话里继续提问 = 撤销删除：整段复活并接着往下排轮次。
    # 不复活的话这一轮会掉进一段**列表里看不见**的会话里 —— 答案在，但用户找不到。
    revived = _ask(client, token, "再问一句，医疗期工资怎么发？", conversation_id=cid)
    assert revived["conversation_id"] == cid, "续问被删的会话时另起了一段（这一轮成了孤儿）"
    back = client.get(f"{API}/chat/conversations/{cid}", headers=_auth(token))
    assert back.status_code == 200, back.text
    assert back.json()["status"] == "active" and back.json()["count"] == 3, back.json()
    assert cid in _conversations(client, token), "复活的会话没回到列表里"

    client.delete(f"{API}/chat/conversations/{cid}", headers=_auth(token))


def test_conversation_append_to_unknown_id_never_fails(client: TestClient, actors, fake_llm) -> None:
    """会话号查不到时**另起一段**，绝不报错 —— 答案已经生成完了。

    这是 ``append_turn`` 的核心纪律：落库失败不能反过来让用户丢答案。
    前端拿到新会话号照常点亮侧栏，用户视角只是"历史里多了一条"。
    """
    token = actors["alice"]["token"]
    out = _ask(client, token, "经济补偿金和赔偿金能同时主张吗？",
               conversation_id="conv_doesnotexist")
    assert out["conversation_id"].startswith("conv_"), out
    assert out["conversation_id"] != "conv_doesnotexist"
    assert out["answer"], "降级路径把答案也弄丢了"
    assert out["conversation_id"] in _conversations(client, token), "降级建的新会话没进列表"

    client.delete(f"{API}/chat/conversations/{out['conversation_id']}", headers=_auth(token))


def test_conversation_title_rules() -> None:
    """标题 = 首句截断（纯函数，不碰库）。

    **不切逗号**："公司违法辞退我，医疗期怎么算" 是一个问题，
    切在逗号上会得到半句话，列表里认不出是哪一段。
    """
    from backend.app.services.chat_history_service import TITLE_CHARS, conversation_title

    assert conversation_title("公司违法辞退我，医疗期怎么算") == "公司违法辞退我，医疗期怎么算"
    assert conversation_title("第一句。第二句就不进标题了") == "第一句"
    assert conversation_title("换行\n和   多空格") == "换行 和 多空格"
    assert conversation_title("") == "未命名会话"
    assert conversation_title("   ") == "未命名会话"

    long = "这是一个非常长的提问" * 5
    title = conversation_title(long)
    assert len(title) == TITLE_CHARS + 1 and title.endswith("…"), (len(title), title)
    # 只有首句超长时才补省略号；短标题补了就变成"……？"
    assert not conversation_title("短问题？").endswith("…")


# ================================ 4 个产品 Agent 的入口（侧边栏那 4 个）
# 目录/钉意图/参数通路那些**不调模型**的断言在 ``tests/test_product_agents.py``；
# 这里放端到端的三条 —— 本文件的既定做法是不 mock 编排（见模块 docstring），
# 因为这层要验的恰恰是"整条链路真的通了"。
def _sse_done(raw: str) -> dict:
    """从 SSE 原文里取 ``done`` 帧的数据包。

    取不到直接把原文尾巴带进断言信息里：SSE 出问题时的症状是"流里没有那条帧"，
    只回一句 KeyError 的话，看的人还得自己去复现一遍才知道流里到底有什么。
    """
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines()
                 if ln.startswith("event:") or ln.startswith("data:")]
        event = next((ln[6:].strip() for ln in lines if ln.startswith("event:")), "")
        if event != "done":
            continue
        return json.loads("\n".join(ln[5:].strip() for ln in lines if ln.startswith("data:")))
    raise AssertionError(f"SSE 流里没有 done 帧：{raw[-800:]}")


def test_agent_case_search_stream_pins_intent_and_returns_hits(
        client: TestClient, actors) -> None:
    """类案检索 Agent：钉住意图、按 ``top_k`` 召回、``done`` 帧**带命中明细**。

    三件事一起验，因为它们在改动前是同一个症状：页面上只剩下"命中 N 件"，
    一件都列不出来，而且选几条都一样。
    - ``RunRequest.intent`` 原先从没进过 State，``IntentNode`` 又会拿分类器
      覆写意图 —— 一条"帮我写份起诉状"式的提问能把类案检索页带偏到别的路线；
    - ``CaseNode`` 里 ``top_k=5`` 是**写死**的，页面上的「返回条数」是假功能；
    - ``_sse_stream`` 只补了 risk_points 那几样，检索结果只给 ``counts`` 计数。
    """
    r = client.post(f"{API}/agents/case-search/stream",
                    headers=_auth(actors["alice"]["token"]),
                    json={"query": "竞业限制纠纷 违约金 裁判倾向", "top_k": 3})
    assert r.status_code == 200, r.text
    done = _sse_done(r.text)

    # 整条流的**原文**里不许出现服务端路径。查原文而不是某个字段：只要它出现在
    # 任意一层（``citations`` 的某一项、嵌套的 metadata 里），这条就该红。
    # 这条出口以前是漏的 —— 见 ``chat.py._public`` 的说明。
    assert "source_path" not in r.text, f"SSE 里泄漏了 source_path：{r.text[:400]}"

    # 命中明细必须有，且是**经终审回填过**的那一份（每张卡上要显示核验角标）
    assert isinstance(done.get("cases"), list), f"done 帧没有 cases 明细：{list(done)}"
    assert len(done["cases"]) <= 3, (
        f"返回了 {len(done['cases'])} 条，超过了页面选的 3 条 —— top_k 没进 state"
    )
    for key in ("statutes", "knowledges", "citations", "evidence_gap"):
        assert key in done, f"done 帧缺 {key}：{list(done)}"

    # 意图是**钉住**的，不是分类器猜的：任务表里也应当是 case_search。
    task = client.get(f"{API}/tasks/{done['task_id']}",
                      headers=_auth(actors["alice"]["token"]))
    assert task.status_code == 200, task.text
    assert task.json()["intent"] == "case_search", (
        f"落库的意图是 {task.json()['intent']}，说明入口钉的意图没生效"
    )


def test_agent_assistant_stream_leaves_a_replayable_turn(client: TestClient, actors) -> None:
    """AI 助手 Agent：``done`` 帧要能重放，会话栏打开来**不能是空白**。

    这条钉的是换链路最容易踩的坑：助手页的历史会话重放读的是 ``payload.answer``，
    而 Agent 链路的交付物叫 ``report``（报告模板：引用清单 / 证据缺口 / 置信度三节）。
    不归一化就落库的话，库里明明有这一轮、界面上却一片空白，
    而且不会有任何一处报错 —— 律师只会以为"这东西没保存"。
    """
    r = client.post(f"{API}/agents/assistant/stream",
                    headers=_auth(actors["alice"]["token"]),
                    json={"query": "员工入职三个月未签书面劳动合同，能否主张二倍工资？"})
    assert r.status_code == 200, r.text
    done = _sse_done(r.text)

    assert done.get("answer"), f"done 帧没有可显示的正文：{list(done)}"
    conv_id = done.get("conversation_id") or ""
    assert conv_id.startswith("conv_"), (
        f"这一轮没有落库（conversation_id={conv_id!r}）—— 助手页的历史会话栏会缺一条"
    )

    replay = client.get(f"{API}/chat/conversations/{conv_id}",
                        headers=_auth(actors["alice"]["token"]))
    assert replay.status_code == 200, replay.text
    turns = replay.json()["turns"]
    assert turns and turns[-1].get("answer"), (
        "重放的 payload 里没有 answer —— 打开历史会话会是一片空白"
    )
    assert turns[-1]["answer"] == done["answer"], "落库的正文与当次返回的不一致"


def test_doc_compose_keeps_its_contract_and_adds_evidence(client: TestClient, actors) -> None:
    """文书中心：**既有键一个不少**，另外多一块可核验的依据。

    这条守两件事：
    1. 改造是**纯增量** —— ``template`` / ``content`` / ``fills`` / ``doc_type``
       这些前端渲染直接读的键，少一个页面就白屏或渲染出 undefined；
    2. 编排那一段**失败也必须交付文书**（检索挂了照样出正文，缺口如实标明），
       所以这里断言的是"正文非空"，而不是"依据非空"（依据随检索质量浮动）。
    """
    text = ("原告张三于 2023 年 1 月入职被告公司，双方未签订书面劳动合同，"
            "月工资 8000 元。2023 年 6 月被告无故辞退原告，未支付经济补偿。")
    r = client.post(f"{API}/doc/compose", headers=_auth(actors["alice"]["token"]),
                    json={"text": text, "template_id": "", "doc_type": ""})
    assert r.status_code == 200, r.text
    body = r.json()

    for key in ("template", "content", "fills", "doc_type", "filled_count",
                "field_count", "missing"):
        assert key in body, f"/doc/compose 少了既有键 {key}：{list(body)}"
    assert body["content"].strip(), "套模板的正文是空的 —— 交付物没了"

    ev = body.get("evidence")
    assert isinstance(ev, dict), f"没有 evidence 段：{list(body)}"
    for key in ("task_id", "statutes", "knowledges", "citations",
                "citation_check", "evidence_gap", "elapsed_ms"):
        assert key in ev, f"evidence 段缺 {key}：{list(ev)}"
    assert ev["task_id"].startswith("task_"), "检索终审那一段没真的跑起来"
    assert ev["citation_check"] in ("passed", "failed", "pending")
    # 同一条出口纪律：依据明细里不许带服务端路径（见 ``chat.py._public``）
    assert "source_path" not in r.text, f"响应里泄漏了 source_path：{r.text[:400]}"


# =========================================================================== WebSocket
def test_ws_rejects_missing_or_bad_token(client: TestClient, actors) -> None:
    """未授权连接必须**握手后立刻断开**，不能"连上之后再慢慢校验"。

    那样未授权连接会一直占着资源；而且鉴权散在每条消息里，
    很容易出现"某条消息忘了校验"的漏洞。
    """
    from starlette.websockets import WebSocketDisconnect

    for url in (
        f"/ws/tasks/nonexistent?token=",
        "/ws/tasks/nonexistent?token=not-a-jwt",
    ):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url) as ws:
                ws.receive_text()   # 服务端会先发一条 error 再关闭
                ws.receive_text()   # 若没断开，这里会一直等 → 超时说明没断开


def test_ws_accepts_valid_token_and_sends_snapshot(client: TestClient, actors) -> None:
    """合法令牌应当立刻收到一份落库快照 —— 前端不必等第一次进度事件才有内容。"""
    token = actors["alice"]["token"]
    r = client.post(f"{API}/chat", headers=_auth(token),
                    json={"query": "合同解除的条件有哪些？", "doc_type": "consult"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    with client.websocket_connect(f"/ws/tasks/{task_id}?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "snapshot", msg
        assert msg["data"]["task_id"] == task_id


__all__ = ["actors", "client"]
