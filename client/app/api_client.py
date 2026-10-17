"""桌面端 HTTP 客户端。

**只做三件事**：发请求、把错误翻成人话、把 SSE 拆成事件。
界面逻辑一概不在这里 —— 否则"改一处界面要动网络层"是迟早的事。

几个刻意的选择：

- **同步 httpx + 工作线程**，而不是 asyncio。Qt 的事件循环与 asyncio 循环
  要桥接才能共存，而桌面端一次只跑一两个请求，开线程比维护两套循环省事得多。
- **超时分段给**：连接 10 秒、读 30 分钟。合同审查一次要跑几分钟，
  用统一的 30 秒会在任务跑到一半时抛 ReadTimeout，
  而界面上看起来像"服务器挂了"。
- **错误必须带状态码与服务端 detail**。只抛 "请求失败" 的话，
  "口令错了" 和 "服务器没起" 在界面上长得一样，用户没法自己处理。
- **上传走 httpx 的 multipart**，不用 curl。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import httpx

# 连接要快失败（服务器没起时别让用户等半分钟）；
# 读要能等很久（长任务是常态，不是异常）。
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 1800.0


def new_case_id() -> str:
    """造一个本地唯一的案件编号：``case_20260917_a1b2c3``。

    带日期是有用的：律师在列表里扫一眼就知道是哪天建的，
    而纯随机串做不到。不追求全局唯一 —— 撞号时服务端会报重复，
    那时让用户改一个比我们在这里加分布式 ID 生成器划算。
    """
    import secrets
    import time

    return f"case_{time.strftime('%Y%m%d')}_{secrets.token_hex(3)}"


class ApiError(Exception):
    """接口错误。``detail`` 是服务端给的中文说明，可以直接显示给律师看。"""

    def __init__(self, message: str, *, status: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail

    @property
    def user_message(self) -> str:
        """给界面用的一句话。**服务端 detail 优先** —— 它比客户端猜的准。"""
        if self.detail:
            return self.detail
        if self.status == 401:
            return "登录已失效，请重新登录"
        if self.status == 403:
            return "当前账号没有这个权限"
        if self.status == 0:
            return f"连不上服务器：{self}"
        return f"请求失败（HTTP {self.status}）"


class ApiClient:
    """与后端通信。``token`` 变了就换 header，不需要重建对象。

    ``token_provider`` 是**单一数据源**机制：传一个带 ``.token`` 属性的对象
    （通常是 :class:`~client.app.state.AppState`），本类就每次读它，而不是
    自己留一份。

    **为什么非要有它。** 凭证存两份时，"退出登录"只清了其中一份是很容易
    写出来的 bug —— 界面回到登录框，但客户端仍握着旧 token，
    下一个请求照样通过。这种 bug 不会报错，只表现为"退出后还能操作"。
    """

    def __init__(self, base_url: str, token: str = "",
                 token_provider: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self._token = token
        self._token_provider = token_provider
        # **本机地址不走系统代理**。HTTP_PROXY 这类环境变量是"给出网用的"，
        # 连用户自己敲进去的 127.0.0.1 也要绕一圈代理的话，请求会被代理改写
        # 成 404（实测：{"detail":"Not Found","path":"/http://..."} 全拼在
        # path 里）—— 界面上表现为所有接口 Not Found，而 Web 端同一后端
        # 一切正常，排查方向会被完全带偏。
        host = self.base_url.split("//")[-1].split("/")[0].split(":")[0]
        local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
        self._client = httpx.Client(
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
            follow_redirects=True,
            trust_env=not local,
        )

    @property
    def token(self) -> str:
        if self._token_provider is not None:
            return str(getattr(self._token_provider, "token", "") or "")
        return self._token

    @token.setter
    def token(self, value: str) -> None:
        if self._token_provider is not None:
            self._token_provider.token = str(value or "")
        else:
            self._token = str(value or "")

    # ------------------------------------------------------------------ 基础
    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method: str, path: str, *, auth: bool = True,
                **kwargs: Any) -> Any:
        """发一个请求并解析 JSON。失败时抛 :class:`ApiError`。"""
        url = f"{self.api}{path}"
        try:
            resp = self._client.request(
                method, url, headers=self._headers(auth=auth), **kwargs)
        except httpx.ConnectError as exc:
            raise ApiError(f"连不上服务器 {self.base_url}（{exc}）") from exc
        except httpx.TimeoutException as exc:
            raise ApiError(f"等服务器响应超时（{exc}）") from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"网络错误：{exc}") from exc

        if resp.status_code >= 400:
            raise ApiError(
                f"{method} {path} -> {resp.status_code}",
                status=resp.status_code,
                detail=_extract_detail(resp),
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            # 拿到 HTML 通常意味着地址写错（比如后面少了一截路径）或被网关拦了。
            # 直接把前 200 字符报出来，比 "JSON 解析失败" 好查得多。
            raise ApiError(f"{method} {path} 返回的不是 JSON：{resp.text[:200]}",
                           status=resp.status_code)

    # ------------------------------------------------------------------ 认证
    def login(self, username: str, password: str) -> dict[str, Any]:
        """/auth/login。**登录不走 Bearer**（还没有 token）。"""
        data = self.request("POST", "/auth/login", auth=False,
                            json={"username": username, "password": password})
        # 字段是 ``access_token``（连同 ``expires_in`` / ``token_type``，
        # 是 OAuth 风格的三件套），**不是** ``token``。取错名字的后果很坑：
        # 登录这一步显示成功，之后每个接口都 401 —— 看起来像"服务器不认我"，
        # 而实际上是把空字符串当成了凭证。
        self.token = str(data.get("access_token") or data.get("token") or "")
        return data

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/auth/me")

    def permissions(self) -> dict[str, Any]:
        return self.request("GET", "/auth/permissions")

    # ------------------------------------------------------------------ 案件与材料
    def list_cases(self, **params: Any) -> dict[str, Any]:
        return self.request("GET", "/cases", params=params)

    def create_case(self, title: str, case_id: str = "", **extra: Any) -> dict[str, Any]:
        """建案件。**``case_id`` 由客户端给**（服务端不代生成）。

        这是刻意的设计：案件编号往往要对齐律所自己的卷宗号，
        让服务端自动生成就等于剥夺了这个能力。代价是客户端必须先造一个
        唯一编号 —— 所以下面有个 :func:`new_case_id`。
        """
        return self.request("POST", "/cases",
                            json={"case_id": case_id or new_case_id(), "title": title, **extra})

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self.request("GET", f"/cases/{case_id}")

    def material_ids_payload(self, case_id: str, material_ids: list[int],
                             query: str = "") -> dict[str, Any]:
        """组装"用已上传材料审查"的请求体。**只给 id，不给路径。**

        后端 ``_resolve_material_paths`` 由 id 查库拿路径并做权限校验。
        客户端**绝不能**自己拼服务端路径再传 ``material_paths`` ——
        那等于承认"路径由客户端指定"，是任意文件读取的入口。
        """
        return {"query": query, "case_id": case_id, "material_ids": list(material_ids)}

    def upload_material(self, case_id: str, path: str | Path) -> dict[str, Any]:
        """上传材料。**走 multipart**（后端用 ``UploadFile`` 接）。

        用 httpx 而不是命令行 curl 的 multipart：后者在这台机器上会被服务端
        记成 ``Invalid HTTP request received``，且客户端这边看不出任何异常。
        """
        p = Path(path)
        with p.open("rb") as fh:
            files = {"file": (p.name, fh, "application/octet-stream")}
            return self.request("POST", f"/cases/{case_id}/materials", files=files)

    # ------------------------------------------------------------------ 任务
    def review_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/contract/review", json=payload)

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/chat", json=payload)

    def list_tasks(self, **params: Any) -> dict[str, Any]:
        return self.request("GET", "/tasks", params=params)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/tasks/{task_id}")

    def task_trace(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/tasks/{task_id}/trace")

    def task_gaps(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/tasks/{task_id}/gaps")

    # ------------------------------------------------------------------ 文书
    def list_drafts(self, **params: Any) -> dict[str, Any]:
        return self.request("GET", "/drafts", params=params)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self.request("GET", f"/drafts/{draft_id}")

    def save_draft(self, draft_id: str, title: str, content: str) -> dict[str, Any]:
        return self.request("PUT", f"/drafts/{draft_id}",
                            json={"title": title, "content": content})

    def export_draft(self, draft_id: str, fmt: str = "docx") -> dict[str, Any]:
        return self.request("POST", f"/drafts/{draft_id}/export", params={"fmt": fmt})

    def download_draft(self, draft_id: str, dest: str | Path) -> Path:
        """下载导出好的文件，存到 ``dest``。**这是拿文件的唯一正当途径。**

        不要自己去拼服务器上的路径：接口不返回服务端绝对路径（那是刻意的），
        而且浏览器/客户端也读不到服务器的文件系统。
        """
        url = f"{self.api}/drafts/{draft_id}/download"
        dest = Path(dest)
        with self._client.stream("GET", url, headers=self._headers()) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise ApiError("下载失败", status=resp.status_code,
                               detail=_extract_detail(resp))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest

    # ------------------------------------------------------------------ SSE
    def stream(self, method: str, path: str, payload: dict[str, Any]
               ) -> Iterator[tuple[str, Any]]:
        """逐条产出 SSE 事件 ``(事件名, 数据)``。**会阻塞**，要放在工作线程里。

        帧格式是后端定的：``event: <名>\\ndata: <json>\\n\\n``，
        另有 ``: keep-alive`` 心跳（注释帧，没有 data，要跳过）。
        """
        url = f"{self.api}{path}"
        try:
            with self._client.stream(method, url, json=payload,
                                     headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise ApiError(f"{method} {path} -> {resp.status_code}",
                                   status=resp.status_code, detail=_extract_detail(resp))
                event = ""
                for line in resp.iter_lines():
                    if line.startswith(":"):
                        continue                      # 心跳注释帧
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        raw = line[5:].strip()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = raw
                        yield event, data
                        event = ""
        except httpx.HTTPError as exc:
            raise ApiError(f"流式请求中断：{exc}") from exc

    def stream_review(self, payload: dict[str, Any]) -> Iterator[tuple[str, Any]]:
        return self.stream("POST", "/contract/review/stream", payload)

    def stream_chat(self, payload: dict[str, Any]) -> Iterator[tuple[str, Any]]:
        return self.stream("POST", "/chat/stream", payload)

    def close(self) -> None:
        self._client.close()


def _extract_detail(resp: httpx.Response) -> str:
    """从错误响应里抠出**能给人看**的那句话。

    后端错误体有两种形状：``{"detail": "..."}`` 与
    ``{"detail": {"message": "...", ...}}``。只取 detail 的话后者会显示成一坨
    JSON —— 比没有说明还难读。
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:200]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)[:300]
    return str(detail)[:300]
