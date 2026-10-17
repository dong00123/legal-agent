"""微信机器人接入服务（2026-10-11）。

一句话分工：**法律逻辑全在这里，微信机器人只做搬运。**
机器人（WeChat-AI，Node 侧）把「谁发的 + 文字 + 附件」转成一次
``POST /api/v1/wechat/events``，本模块决定怎么答、答什么，机器人把返回的几句话
原样发回微信。这样"证据链只对诉讼案件开放""案件归属隔离"这些规则
**只有一处实现**，不会在机器人侧长出第二套判据 —— 两套判据迟早分叉，
而分叉的那一套就是越权入口。

绑定的意义：机器人不是某个律师，它没有"以谁的名义办案"的信息。
所以先用一次性绑定码把 ``peer_id``（微信用户）绑到 ``la_user.id``（律师账号），
之后**每一次操作都以该律师的身份进服务层**（``CurrentUser``），
权限判定完全复用 ``case_service.assert_access``。

三条纪律：

1. **未绑定 = 只回复引导，不碰任何案件数据。** 不做"先看看有没有案件"的兜底：
   那等于给未绑定用户开了一个只读通道。
2. **附件按"证据材料"落库，与 Web 端上传走同一个 ``register_material``。**
   不另写一条存储路径，否则同内容不同路径会产出两份材料记录。
3. **失败要让律师看懂。** 微信里没有控制台、没有错误码，一句
   "只对诉讼案件开放"远比 400 有用；回复里带上"怎么办"。
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import logging
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.app.config import settings
from backend.app.db.models import User, WechatBindCode, WechatBinding
from backend.app.db.session import session_scope
from backend.app.security import CurrentUser
from backend.app.services import case_service

logger = logging.getLogger("legal_agent.services.wechat")

# 绑定码字符集：去掉 0/O/1/I/L 这些看着像的，律师是在微信里手打这串码。
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
# 上面那个受限字符集，让"这条消息是不是一个绑定码"几乎不会误判 ——
# **机器人侧有一份等价判据**（apps/api/src/legal-bridge.ts 的 BIND_CODE_RE），
# 两边必须一致：宽了机器人会把普通聊天转过来，窄了"只发码"就进不了门。
_CODE_RE = re.compile(rf"^[{_CODE_ALPHABET}]{{{_CODE_LEN}}}$")


def looks_like_bind_code(text: str) -> bool:
    """整条消息是否就是一个绑定码（大小写不敏感、允许首尾空白）。"""
    return bool(_CODE_RE.match((text or "").strip().upper()))

# 一次最多列几个案件（微信里超过 10 条要翻页，等于没法选）。
CASE_LIST_LIMIT = 10


class WechatError(ValueError):
    """业务校验失败（接口层转 400）。"""


class WechatNotConfigured(WechatError):
    """功能未开启或缺桥接密钥（接口层转 503）。"""


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _stamp(dt: _dt.datetime | None = None) -> str:
    """ISO 秒级字符串，与 ``case.evidence_chain_at`` 同一口径。

    用字符串而非 ``DateTime``：过期判定要在 SQLite 与 MySQL 上给出**相同**结果，
    而两个后端对时区/精度的处理并不一致。
    """
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_stamp(raw: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime((raw or "").strip(), "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 绑定
def _viewer_of(user: User) -> CurrentUser:
    """把 ORM 用户转成服务层认的登录主体。

    ``CurrentUser`` 才是 ``case_service`` 认识的东西 —— 直接传 ORM 对象也能跑，
    但那样等于绕过了 ``security.CurrentUser`` 这层收口，字段名改一次就静默失效。
    """
    role = user.role if user.role in ("admin", "lawyer") else "lawyer"
    return CurrentUser(user.id, user.username, role, user.team_id, user.full_name or "")


def issue_bind_code(user: CurrentUser) -> dict[str, Any]:
    """为当前律师签发一次性绑定码。同一个人重复取码时旧码作废。"""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    expires = _now() + _dt.timedelta(minutes=max(1, int(settings.WECHAT_BIND_CODE_TTL_MINUTES)))
    with session_scope() as db:
        # 作废该用户此前的未用码：留着多张有效码，等于把绑定窗口叠起来，
        # "一次性"这件事就没意义了。
        for old in db.execute(
            select(WechatBindCode).where(
                WechatBindCode.user_id == user.id, WechatBindCode.used == 0
            )
        ).scalars().all():
            old.used = 1
        db.add(WechatBindCode(code=code, user_id=user.id, expires_at=_stamp(expires)))
        db.flush()
    return {
        "code": code,
        "expires_at": _stamp(expires),
        "ttl_minutes": max(1, int(settings.WECHAT_BIND_CODE_TTL_MINUTES)),
        "hint": f"在微信里给机器人发送：/绑定 {code}",
    }


def bind_peer(peer_id: str, code: str, *, bot_id: str = "",
              display_name: str = "") -> dict[str, Any]:
    """用绑定码把微信 peer 绑到律师账号。码用掉即失效。"""
    peer_id = (peer_id or "").strip()
    code = (code or "").strip().upper()
    if not peer_id:
        raise WechatError("缺少微信用户标识（peer_id）")
    if not code:
        raise WechatError("缺少绑定码")

    with session_scope() as db:
        row = db.execute(
            select(WechatBindCode).where(WechatBindCode.code == code)
        ).scalar_one_or_none()
        if row is None:
            raise WechatError("绑定码不存在，请在 Web 工作台重新取一个")
        if row.used:
            raise WechatError("绑定码已被使用，请在 Web 工作台重新取一个")
        exp = _parse_stamp(row.expires_at)
        if exp is not None and exp < _now():
            raise WechatError("绑定码已过期，请在 Web 工作台重新取一个")

        user = db.execute(select(User).where(User.id == row.user_id)).scalar_one_or_none()
        if user is None or not user.is_active:
            raise WechatError("该绑定码对应的账号已停用，请联系管理员")

        binding = db.execute(
            select(WechatBinding).where(WechatBinding.peer_id == peer_id)
        ).scalar_one_or_none()
        if binding is None:
            binding = WechatBinding(peer_id=peer_id, bot_id=bot_id or "",
                                    user_id=user.id, case_id="",
                                    display_name=display_name or "")
            db.add(binding)
        else:
            if binding.user_id != user.id:
                # 换绑：绑定码是身份凭证，拿着有效码就可以改绑。
                # 但仍要记一行日志 —— 换绑是"这个微信号换了主人"的信号。
                logger.warning("微信 peer %s 换绑：user %s -> %s",
                               peer_id, binding.user_id, user.id)
            binding.user_id = user.id
            binding.bot_id = bot_id or binding.bot_id
            if display_name:
                binding.display_name = display_name
            binding.case_id = ""
        row.used = 1
        db.flush()

        return {
            "peer_id": peer_id,
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name or user.username,
        }


def unbind_peer(peer_id: str) -> dict[str, Any]:
    with session_scope() as db:
        row = db.execute(
            select(WechatBinding).where(WechatBinding.peer_id == (peer_id or "").strip())
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "尚未绑定"}
        db.delete(row)
        db.flush()
    return {"ok": True}


def get_binding(peer_id: str) -> dict[str, Any] | None:
    with session_scope() as db:
        row = db.execute(
            select(WechatBinding).where(WechatBinding.peer_id == (peer_id or "").strip())
        ).scalar_one_or_none()
        if row is None:
            return None
        user = db.execute(select(User).where(User.id == row.user_id)).scalar_one_or_none()
        return {
            "peer_id": row.peer_id,
            "bot_id": row.bot_id or "",
            "user_id": row.user_id,
            "username": (user.username if user else ""),
            "full_name": (user.full_name if user else ""),
            "active": bool(user and user.is_active),
            "case_id": row.case_id or "",
            "display_name": row.display_name or "",
            "bound_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
        }


def list_bindings(user: CurrentUser) -> list[dict[str, Any]]:
    """某个律师绑了哪些微信号（Web 工作台展示用）。"""
    with session_scope() as db:
        rows = db.execute(
            select(WechatBinding).where(WechatBinding.user_id == user.id)
            .order_by(WechatBinding.id)
        ).scalars().all()
        return [{
            "peer_id": r.peer_id,
            "bot_id": r.bot_id or "",
            "case_id": r.case_id or "",
            "display_name": r.display_name or "",
            "bound_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
        } for r in rows]


def _resolve(peer_id: str) -> tuple[CurrentUser, str] | None:
    """peer → (登录主体, 当前案件号)。未绑定或账号停用返回 None。"""
    info = get_binding(peer_id)
    if info is None or not info["active"]:
        return None
    with session_scope() as db:
        user = db.execute(select(User).where(User.id == info["user_id"])).scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        return _viewer_of(user), info["case_id"]


def _set_case(peer_id: str, case_id: str) -> None:
    with session_scope() as db:
        row = db.execute(
            select(WechatBinding).where(WechatBinding.peer_id == (peer_id or "").strip())
        ).scalar_one_or_none()
        if row is not None:
            row.case_id = case_id or ""
            db.flush()


def _litigation_cases(viewer: CurrentUser, *, limit: int = CASE_LIST_LIMIT) -> list[dict[str, Any]]:
    """本律师可见的**诉讼**案件。

    证据链只对诉讼案件开放（``assert_evidence_chain_allowed``），
    所以列表按 ``case_type=litigation`` 过滤 —— 让律师看见一堆选不了的非诉案件
    只会让人以为"我点错了"。
    """
    return case_service.list_cases(viewer, case_type="litigation", limit=limit)


# --------------------------------------------------------------------------- 文本工具
# 本助手**自己**的命令。
#
# ⚠️ 这里**没有** ``/绑定`` 与 ``/解绑``（虽然那是最顺手的两个词）：机器人里还有
# 另一个子系统占着它们 —— P2P 用户互转，见 WeChat-AI 的
# ``packages/core/src/p2p-service.ts`` 里的 ``BIND_RE = /^\/绑定\s+([A-Za-z0-9]{4,12})$/``
# 与 ``UNBIND_RE = /^\/解绑$/``，语义是"绑定 LINUX DO 账号"。
# 同一个词有两种含义时，谁在前面谁赢，另一个功能就静默失效了（实测：律师发
# 绑定码收到的是 P2P 那句「绑定码无效或已过期。请到用户中心重新生成绑定码」）。
# 所以这里改用不会冲突的 ``/律师绑定``、``/律师解绑``；而**日常路径其实是直接
# 发那串码**（见 ``looks_like_bind_code``），那条路径 P2P 完全不碰。
_OWN_COMMANDS = frozenset({
    "律师绑定", "律师解绑", "案件", "选", "证据链", "状态", "证据", "上传", "材料",
})
_HELP_COMMANDS = frozenset({"帮助", "help", "?", "？"})
# 属于机器人里**别的子系统**的命令：必须原样交回去（handled=False）。
# 尤其不能"不认识就回一句未识别" —— 那等于把它们从另一个功能手里抢走。
_PASSTHROUGH_COMMANDS = frozenset({
    "我的身份", "同意", "拒绝", "断开", "取消请求", "拉黑", "取消拉黑", "黑名单",
})

HELP_TEXT = (
    "【衡律工作台 · 微信证据助手】\n"
    "1) 绑定：在 Web 工作台「证据链」页取码，把那串码直接发过来即可\n"
    "2) /案件 —— 列出我的诉讼案件\n"
    "3) /选 <序号> —— 选择当前办理的案件\n"
    "4) 直接发图片/文件 —— 作为证据材料上传（会落到当前案件）\n"
    "5) /证据链 —— 基于已上传材料生成证据链\n"
    "6) /状态 —— 查看绑定与当前案件\n"
    "7) /律师解绑 —— 解除本微信号与律师账号的绑定\n"
    "注：/绑定 与 /解绑 属于本机器人的另一项功能（用户互转），"
    "本助手用 /律师绑定 与 /律师解绑。\n"
    "（AI 辅助整理，不构成法律意见）"
)


def _reply_case_list(viewer: CurrentUser, current: str) -> list[str]:
    cases = _litigation_cases(viewer)
    if not cases:
        return ["你名下还没有「诉讼」案件。\n请先在 Web 工作台「案件管理」新建一件诉讼案件，"
                "再回来上传证据。\n（证据链只对诉讼案件开放）"]
    lines = [f"你名下的诉讼案件（共 {len(cases)} 件，最多显示 {CASE_LIST_LIMIT} 件）："]
    for i, c in enumerate(cases, 1):
        mark = "★ " if c["case_id"] == current else ""
        title = c.get("title") or "（未命名）"
        lines.append(f"{mark}{i}. {title}\n   案号 {c['case_id']}")
    lines.append("回复 /选 <序号> 选择当前办理的案件。")
    if current:
        lines.append(f"当前：{current}")
    return ["\n".join(lines)]


def _reply_status(peer_id: str) -> list[str]:
    info = get_binding(peer_id)
    if info is None:
        return ["尚未绑定。请在 Web 工作台「证据链」页取绑定码，然后发 /绑定 <绑定码>。"]
    lines = [f"已绑定：{info['full_name'] or info['username']}（{info['username']}）"]
    lines.append(f"微信昵称：{info['display_name'] or '—'}")
    case_id = info["case_id"]
    if not case_id:
        return ["\n".join(lines + ["当前案件：未选择（回复 /案件 查看并选择）"])]
    viewer = _resolve(peer_id)
    cur = viewer[0] if viewer else None
    try:
        detail = case_service.get_case(case_id, cur, with_materials=False)
        mats = case_service.list_materials(case_id, cur)
        lines.append(f"当前案件：{detail.get('title') or '（未命名）'}（{case_id}）")
        lines.append(f"已上传材料：{len(mats)} 份")
        # ⚠️ 走 ``get_evidence_chain`` 而不是 ``detail``：``case_to_dict`` 不返回
        #    ``evidence_chain_at``，用它的话这里永远显示"尚未生成" —— 而链明明在。
        got = case_service.get_evidence_chain(case_id, cur)
        if got.get("chain"):
            lines.append(f"证据链：{len(got['chain'])} 条主张（生成于 {got.get('generated_at') or '—'}）")
        else:
            lines.append("证据链：尚未生成（回复 /证据链）")
    except Exception as exc:  # noqa: BLE001 - 状态查询不该因为案件被删而炸
        lines.append(f"当前案件：{case_id}（读取失败：{exc}）")
    return ["\n".join(lines)]


def _select_case(peer_id: str, viewer: CurrentUser, arg: str) -> list[str]:
    arg = (arg or "").strip()
    if not arg:
        return ["用法：/选 <序号>，序号来自 /案件 的列表。"]
    cases = _litigation_cases(viewer)
    target = None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(cases):
            target = cases[idx]
        else:
            return [f"序号超出范围（1~{len(cases)}）。回复 /案件 重新查看。"]
    else:
        for c in cases:
            if c["case_id"] == arg:
                target = c
                break
        if target is None:
            return [f"没找到案件 {arg}。回复 /案件 查看可选的诉讼案件。"]
    _set_case(peer_id, target["case_id"])
    return [f"已选择案件：{target.get('title') or '（未命名）'}\n案号 {target['case_id']}\n"
            "现在可以直接发图片/文件上传证据材料了。"]


# --------------------------------------------------------------------------- 附件
def _decode_attachment(att: dict[str, Any]) -> tuple[str, bytes]:
    """把桥接传来的一个附件解码成 (文件名, 字节)。"""
    b64 = att.get("data_base64") or att.get("data") or ""
    if isinstance(b64, (bytes, bytearray)):
        raw = bytes(b64)
    else:
        b64 = str(b64).strip()
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise WechatError(f"附件「{att.get('filename') or '未命名'}」解码失败：{exc}") from exc
    if not raw:
        raise WechatError(f"附件「{att.get('filename') or '未命名'}」是空文件")

    limit = max(1, int(settings.WECHAT_MAX_ATTACHMENT_MB)) * 1024 * 1024
    if len(raw) > limit:
        raise WechatError(
            f"附件「{att.get('filename') or '未命名'}」超过 {settings.WECHAT_MAX_ATTACHMENT_MB} MB 上限"
        )

    name = case_service.safe_filename(
        att.get("filename") or att.get("file_name") or ""
    )
    if not name:
        # 微信图片常常没有文件名：按 mime 推一个后缀，否则解析器认不出类型。
        mime = str(att.get("mime") or "").lower()
        ext = {".jpg": ".jpg", ".png": ".png", ".jpeg": ".jpeg", ".webp": ".webp",
               ".gif": ".gif", ".bmp": ".bmp", ".pdf": ".pdf"}.get(
            "." + mime.split("/")[-1] if mime else "", ".bin")
        name = f"wechat_{_dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}{ext}"
    return name, raw


async def _store_evidence(case_id: str, viewer: CurrentUser, peer_id: str,
                          attachments: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """把附件登记为案件材料并解析。返回 (成功描述, 失败描述)。"""
    ok: list[str] = []
    failed: list[str] = []
    cap = max(1, int(settings.WECHAT_MAX_ATTACHMENTS))
    for att in attachments[:cap]:
        name = "未命名"
        try:
            name, raw = _decode_attachment(att)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(name).suffix) as tmp:
                    tmp.write(raw)
                    tmp_path = Path(tmp.name)
                # 上传前再过一次权限 + 准入：与 Web 端上传接口同一条闸门。
                case = case_service.get_case(case_id, viewer, with_materials=False)
                case_service.assert_evidence_chain_allowed(case.get("case_type"))
                info = case_service.register_material(
                    case_id, tmp_path, filename=name, doc_type="evidence",
                    uploaded_by=viewer.id, move=True,
                )
            finally:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

            parsed = await _parse_material(int(info["id"]), case_id, viewer)
            suffix = "（已解析）" if parsed else "（已上传，解析未完成）"
            ok.append(f"{info.get('filename') or name}{suffix}")
            logger.info("微信上传材料 case=%s material=%s user=%s peer=%s",
                        case_id, info.get("id"), viewer.username, peer_id)
            # 审计：方案第十四章要求材料访问全链路留痕。
            # **上传接口那一份审计写在路由里**（见 api/v1/cases.py），而这条路
            # 不经过那个路由，所以必须在这里补一次 —— 否则"从微信进来的材料"
            # 在审计里查不到，而审计恰恰是"谁把这份材料放进案卷"的唯一凭据。
            # 文件那一跳失败只记日志，不该让上传本身失败（与 audit 模块一致）。
            try:
                from backend.agents.harness.audit import audit_writer

                audit_writer.material_access(
                    case_id=case_id, material=info.get("filename") or name,
                    action="material.upload", user_id=viewer.id,
                    username=viewer.username, team_id=viewer.team_id,
                    client="wechat", ip="", resource_id=str(info["id"]),
                    detail=f"微信上传 {len(raw)} 字节，peer={peer_id}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("微信上传审计写入失败（不影响上传）：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 单个附件失败不该拖垮整批
            failed.append(f"{name}：{exc}")
            logger.warning("微信上传材料失败 case=%s user=%s：%s", case_id, viewer.username, exc)
    if len(attachments) > cap:
        failed.append(f"本次只处理前 {cap} 个附件，其余请分开再发")
    return ok, failed


async def _parse_material(material_id: int, case_id: str, viewer: CurrentUser) -> bool:
    """解析一份材料并回写状态。与 ``POST /materials/{id}/parse`` 同一套动作。

    解析失败**不算上传失败**：材料已经在案了，律师可以在 Web 端重试解析。
    但证据链是从解析产物里读正文的，没解析 = 链上全是缺口，所以要在回复里说清。
    """
    try:
        path = case_service.material_path(material_id, viewer)
        from backend.parser.pipeline import parse_document

        doc = await parse_document(path, do_vision=True)
        case_service.mark_parsed(
            material_id,
            parsed_path=getattr(doc, "parsed_path", "") or "",
            doc_type=getattr(doc, "doc_type", "") or "evidence",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信材料解析失败 material=%s：%s", material_id, exc)
        return False


# --------------------------------------------------------------------------- 证据链
def _chain_reply(case_title: str, result: dict[str, Any]) -> str:
    """把证据链压成微信里能读的几句话。

    微信没有富文本，一条上千字的消息读起来很累。所以只给「主张 → 支撑 → 缺口」
    三行式，最多 8 条；完整内容让律师回 Web 工作台看。
    """
    chain = result.get("chain") or []
    head = f"《{case_title or '本案'}》证据链（{len(chain)} 条主张）"
    if not chain:
        return head + "\n未生成任何主张：卷宗可能还没有材料，请先发图片/文件。"
    lines = [head]
    for i, c in enumerate(chain[:8], 1):
        claim = (c.get("claim") or "（未命名主张）").strip()
        evs = c.get("evidence") or []
        if evs:
            mats = []
            for e in evs[:3]:
                support = (e.get("support") or "直接").strip()
                mats.append(f"{(e.get('material') or '材料').strip()}（{support}）")
            ev_line = "支撑：" + "、".join(mats)
        else:
            ev_line = "支撑：无"
        lines.append(f"{i}. {claim}\n   {ev_line}")
        gap = (c.get("gap") or "").strip()
        if gap:
            lines.append(f"   缺口：{gap}")
    if len(chain) > 8:
        lines.append(f"（其余 {len(chain) - 8} 条请在 Web 工作台查看）")
    summ = (result.get("summary") or "").strip()
    if summ:
        lines.append(f"总评：{summ}")
    lines.append("（AI 辅助整理，非法律意见）")
    return "\n".join(lines)


async def _generate_chain(viewer: CurrentUser, case_id: str) -> str:
    case: dict[str, Any] | None = None
    try:
        case = case_service.get_case(case_id, viewer, with_materials=False)
        case_service.assert_evidence_chain_allowed(case.get("case_type"))
        result = await case_service.generate_evidence_chain(case_id, viewer)
    except Exception as exc:  # noqa: BLE001
        return f"证据链生成失败：{exc}"

    title = str((case or {}).get("title") or case_id)
    chain = result.get("chain") or []
    if chain:
        return _chain_reply(title, result)

    # ---- 空链分两种，回话必须分开说
    # `generate_evidence_chain` 只在**抛异常**时才退回确定性骨架；当 LLM 客户端
    # 只是"降级到规则引擎并返回了非 JSON"时（模型不可用时的常态），它是**正常返回
    # 一个空链**的。这时如果照旧回"未生成任何主张，请先发文件"，律师会以为
    # 自己刚传的材料白传了 —— 而材料其实好好地在案卷里。
    try:
        mats = case_service.list_materials(case_id, viewer)
    except Exception:  # noqa: BLE001
        mats = []
    if mats:
        names = "、".join(m.get("filename") or "材料" for m in mats[:5])
        more = f"等 {len(mats)} 份" if len(mats) > 5 else ""
        return (f"《{title}》已归档 {len(mats)} 份材料（{names}{more}），"
                "但本次没能生成结构化证据链 —— 通常是模型服务暂不可用。\n"
                "材料已在案卷中，可直接在 Web 工作台「证据链」页重试生成，"
                "或稍后再发 /证据链。")
    return _chain_reply(title, result)


# --------------------------------------------------------------------------- 入口
def _split_command(text: str) -> tuple[str, str]:
    """``/证据链 参数`` → ``("证据链", "参数")``；非命令返回 ``("", text)``。"""
    t = (text or "").strip()
    if not t.startswith("/"):
        return "", t
    body = t[1:].strip()
    if not body:
        return "", ""
    parts = re.split(r"\s+", body, maxsplit=1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


async def handle_event(*, peer_id: str, bot_id: str = "", text: str = "",
                       attachments: list[dict[str, Any]] | None = None,
                       display_name: str = "") -> dict[str, Any]:
    """处理一条来自微信机器人的入站事件。

    返回 ``{"handled": bool, "replies": [str]}``。

    ``handled=False`` 表示"这条不是给我的"，机器人应把消息交回它自己的对话链路
    （该机器人同时还是个角色扮演机器人）。判据**刻意窄**：只有``/``开头的命令
    和带附件的消息才接手 —— 宽了会把普通聊天全吃掉。
    """
    peer_id = (peer_id or "").strip()
    attachments = list(attachments or [])
    cmd, arg = _split_command(text)

    if not peer_id:
        return {"handled": False, "replies": []}

    # 只有命令、附件、或"整条消息就是一个绑定码"才接手。
    if not cmd and not attachments:
        if not looks_like_bind_code(text):
            return {"handled": False, "replies": []}
        # 用户行为实测：Web 端把码显示成一个大字符串，律师直接粘进微信，不会去补
        # `/绑定 ` 前缀（曾经因此收到角色扮演链路的"我这边出了点问题"）。
        # 但**已绑定**的 peer 发来一个码形状的串，多半是在正常聊天 —— 交回本机，
        # 不抢（也因此不会把一次闲聊变成一次换绑）。
        if get_binding(peer_id) is not None:
            return {"handled": False, "replies": []}
        cmd, arg = "绑定", (text or "").strip()

    # ---- 别的子系统的命令：原样交回（P2P 的 /我的身份 /同意 /断开 …）
    if cmd in _PASSTHROUGH_COMMANDS:
        return {"handled": False, "replies": []}

    # ---- 绑定类命令不需要已绑定
    if cmd in _HELP_COMMANDS:
        return {"handled": True, "replies": [HELP_TEXT]}

    # ``/绑定 <码>`` 两个子系统共用这个词，按**码的形状**分派：
    # 本助手的码是"8 位、去掉 0/O/1/I/L"的受限字符集，P2P 的是 4~12 位任意字母数字。
    # 形状是我们就归我们（过期/不存在也由我们来回，理由才说得准）；
    # 不是就原样交回 P2P —— 这样两边的绑定都不会被对方吃掉。
    if cmd == "绑定" and not looks_like_bind_code(arg):
        return {"handled": False, "replies": []}
    if cmd in ("绑定", "律师绑定"):
        if not arg:
            return {"handled": True, "replies": [
                "用法：把 Web 工作台「证据链」页生成的那串绑定码直接发过来即可"
                "（也可以写 /律师绑定 <码>）。"]}
        try:
            info = bind_peer(peer_id, arg, bot_id=bot_id, display_name=display_name)
        except WechatError as exc:
            return {"handled": True, "replies": [
                f"绑定失败：{exc}\n如已过期，请在 Web 工作台「证据链」页重新取码。"]}
        return {"handled": True, "replies": [
            f"绑定成功：{info['full_name']}（{info['username']}）\n回复 /案件 选择要办的诉讼案件。"]}
    # ``/解绑`` 同理按**当前状态**分派：绑定过本助手才算本助手的，
    # 否则交回 P2P（两个功能都有"解绑"，对没绑过我们的 peer 只可能是 P2P 的意图）。
    if cmd == "解绑" and get_binding(peer_id) is None:
        return {"handled": False, "replies": []}
    if cmd in ("解绑", "律师解绑"):
        result = unbind_peer(peer_id)
        return {"handled": True, "replies": [
            "已解除绑定。" if result.get("ok") else "尚未绑定，无需解绑。"]}
    if cmd == "状态":
        return {"handled": True, "replies": _reply_status(peer_id)}

    # ---- 不认识的 ``/`` 命令：**交回**，不回"未识别的命令"。
    # 这台机器人主要是角色扮演机器人：看到 ``/`` 就回一句未识别，会把它正常的
    # 对话吃掉（有人就是会打 "/笑"），而且不报任何错。判据窄一点，宁可少管。
    if cmd and cmd not in _OWN_COMMANDS:
        return {"handled": False, "replies": []}

    # ---- 以下都要已绑定
    resolved = _resolve(peer_id)
    if resolved is None:
        return {"handled": True, "replies": [
            "本微信号还没绑定律师账号，暂不能查看案件或上传证据。\n"
            "请在 Web 工作台「证据链」页取绑定码，把那串码直接发过来即可"
            "（也可以写 /律师绑定 <码>）。\n"
            "（回复 /帮助 查看全部用法）"]}
    viewer, current_case = resolved

    if cmd == "案件":
        return {"handled": True, "replies": _reply_case_list(viewer, current_case)}
    if cmd == "选":
        return {"handled": True, "replies": _select_case(peer_id, viewer, arg)}

    # ---- 证据链
    if cmd == "证据链":
        if not current_case:
            return {"handled": True, "replies": [
                "还没选择案件。回复 /案件 查看并选择后再生成证据链。"]}
        return {"handled": True, "replies": [await _generate_chain(viewer, current_case)]}

    # ---- 附件 = 证据材料
    if attachments:
        if not current_case:
            # 只有一个诉讼案件时自动选中 —— 律师发图时不想先打两条命令。
            cases = _litigation_cases(viewer)
            if len(cases) == 1:
                current_case = cases[0]["case_id"]
                _set_case(peer_id, current_case)
            else:
                return {"handled": True, "replies": [
                    "还没选择案件，无法归档这份证据。\n回复 /案件 查看并 /选 <序号> 选择案件后再发。"]}

        try:
            case = case_service.get_case(current_case, viewer, with_materials=False)
        except Exception as exc:  # noqa: BLE001
            _set_case(peer_id, "")
            return {"handled": True, "replies": [
                f"当前案件读取失败（{exc}），已清空选择。请回复 /案件 重新选择。"]}
        if (case.get("case_type") or "") != "litigation":
            return {"handled": True, "replies": [
                f"「{case.get('title') or current_case}」不是诉讼案件，证据链只对诉讼案件开放。\n"
                "回复 /案件 选择诉讼案件。"]}

        ok, failed = await _store_evidence(current_case, viewer, peer_id, attachments)
        lines: list[str] = []
        if ok:
            lines.append(f"已归档到《{case.get('title') or current_case}》：\n"
                         + "\n".join(f"· {x}" for x in ok))
        if failed:
            lines.append("未成功：\n" + "\n".join(f"· {x}" for x in failed))
        if not ok:
            return {"handled": True, "replies": lines or ["附件处理失败，请稍后重试。"]}

        # 上传完顺手生成证据链 —— 用户的原话是"上传证据，并进行证据链的生成"。
        # 生成失败不影响"已归档"的结论（材料确实在案了），所以分两条消息回。
        lines.append(await _generate_chain(viewer, current_case))
        return {"handled": True, "replies": lines}

    if cmd in ("证据", "上传", "材料"):
        return {"handled": True, "replies": [
            "请直接发送图片或文件，我会把它作为证据材料归档到当前案件。\n"
            + (f"当前案件：{current_case}" if current_case else "（还没选择案件，先回复 /案件）")]}

    return {"handled": False, "replies": []}
