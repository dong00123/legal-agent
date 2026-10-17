"""技能内存注册表（服务层，单一数据源）。

对齐 ``app/data/experts_seed.json`` 与 ``expert_registry.py`` 的「派生不另写一份」风格：
种子技能在**启动时**从 ``app/data/skills_seed.json`` 一次性加载进内存，
所有技能接口只读这一份注册表，不落业务库。

用户维度的状态（自建技能 / 启用开关 / 已安装）以 ``user_id`` 为键存在进程内：
- 自建技能合并进全局清单（``author`` = 创建者），并登记进该用户的 ``created``；
- 启用 / 关闭只记录「user_id → skill_id → enabled」的覆盖关系，**不回写种子文件**，
  对齐 WorkBuddy「开关状态仅记录用户配置、不改动技能原文件」的语义；
  种子技能未被我覆盖时，取种子里的 ``enabled`` 默认值；
- 「已安装」单独记录「user_id → set(skill_id)」：显式安装（PUT enabled=true）时登记，
  **关闭（enabled=false）不移除** —— 关闭 ≠ 卸载，卡片上仍是「试一试」而非「安装」。

用户态（自建 / 启用覆盖 / 已安装 / 卸载 / 置顶）每次写操作后原子落盘到
``app/data/skill_user_state.json``，启动时 ``load_state()`` 在 ``load_seed()``
之后恢复，重启后「我安装的」数量与安装 / 关闭 / 置顶 / 卸载 / 自建状态保持重启前。

「技能市场」= 全部技能（含启用中与关闭中的已装内置技能，开启状态即卡片上的开关）；
「已安装」= 当前用户安装过的全部技能（种子默认自带 + 显式安装 + 自建），
关闭 ≠ 卸载：关闭中的技能仍在已安装列表，只是不参与调用（前端以开关灰/蓝展示状态）。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_SEED_PATH = Path(__file__).resolve().parents[2] / "app" / "data" / "skills_seed.json"

# 用户技能状态持久化文件：安装/关闭/置顶/卸载/自建技能在每次写操作后原子落盘，
# 重启后由 load_state() 恢复，保证「我安装的」数量与用户态不归零。
_STATE_PATH = Path(__file__).resolve().parents[2] / "app" / "data" / "skill_user_state.json"

# 分类展示顺序（与前端 chip 一致并扩展）。动态分类按此顺序排，
# 未在表中的分类追加到尾部，避免新分类凭空消失。
CATEGORY_ORDER = ["咨询", "检索", "文书", "合同", "案件", "证据", "翻译", "任务"]

_SEED_AUTHOR = "LegalAgent"


class SkillRegistry:
    """技能注册表。线程安全：创建 / 开关 / 清单读取共用一把锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._skills: dict[str, dict[str, Any]] = {}
        self._created: dict[str, list[str]] = {}       # user_id -> [skill_id]（自建，保序）
        self._user_enabled: dict[str, dict[str, bool]] = {}  # user_id -> {skill_id: enabled}
        # user_id -> set(skill_id)：显式安装过（关闭 ≠ 卸载，关闭不移除）
        self._user_installed: dict[str, set[str]] = {}
        # user_id -> set(skill_id)：显式卸载过（优先级最高，覆盖种子默认启用判定）
        self._user_uninstalled: dict[str, set[str]] = {}
        # user_id -> [skill_id]：置顶列表（保序，最新置顶在前；取消置顶即移除）
        self._user_pinned: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ 加载
    def load_seed(self) -> int:
        """启动时加载种子数据，返回加载条数。文件缺失或格式错误时返回 0。"""
        if not _SEED_PATH.exists():
            return 0
        try:
            raw = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
            items = raw.get("skills") or []
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
            return 0
        with self._lock:
            for item in items:
                sid = str(item.get("id") or "").strip()
                if not sid:
                    continue
                self._skills[sid] = _normalize(item, author=_SEED_AUTHOR)
        return len(self._skills)

    # ------------------------------------------------------------------ 持久化
    def load_state(self) -> int:
        """启动时（load_seed 之后）从状态文件恢复用户态与自建技能。

        恢复内容：已安装 / 启用覆盖 / 已卸载 / 置顶 / 自建登记，以及自建技能
        完整数据（用户创建过的技能）。自建技能合并回 ``_skills``，
        id 与种子冲突时以状态文件为准。文件缺失或格式错误时返回 0。
        """
        if not _STATE_PATH.exists():
            return 0
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        with self._lock:
            for item in raw.get("custom_skills") or []:
                sid = str(item.get("id") or "").strip()
                if not sid:
                    continue
                # id 冲突时以状态文件为准（覆盖种子/上一次会话的同名技能）
                self._skills[sid] = _normalize(item, author="")
            self._user_installed = {
                str(k): set(v) for k, v in (raw.get("user_installed") or {}).items()}
            self._user_enabled = {
                str(k): dict(v) for k, v in (raw.get("user_enabled") or {}).items()}
            self._user_uninstalled = {
                str(k): set(v) for k, v in (raw.get("user_uninstalled") or {}).items()}
            self._user_pinned = {
                str(k): list(v) for k, v in (raw.get("user_pinned") or {}).items()}
            self._created = {
                str(k): list(v) for k, v in (raw.get("created") or {}).items()}
        return len(raw.get("custom_skills") or [])

    def save_state(self) -> None:
        """将当前用户态与自建技能原子写入状态文件（临时文件 + os.replace）。

        每次写操作（create / set_enabled / uninstall / edit / set_pinned）后调用；
        线程安全：内存快照在锁内生成，写盘在锁外执行，避免持锁 IO 阻塞读。
        写盘失败仅忽略不抛错（持久化是尽力而为，不影响本次业务结果）。
        """
        with self._lock:
            data = {
                "user_installed": {k: sorted(v) for k, v in self._user_installed.items()},
                "user_enabled": {k: dict(v) for k, v in self._user_enabled.items()},
                "user_uninstalled": {k: sorted(v) for k, v in self._user_uninstalled.items()},
                "user_pinned": {k: list(v) for k, v in self._user_pinned.items()},
                "created": {k: list(v) for k, v in self._created.items()},
                # 自建技能完整数据：仅保存「用户创建过且仍在注册表中」的技能，
                # 避免把种子里第三方作者（author != LegalAgent）的内置技能重复写盘。
                "custom_skills": [
                    dict(e) for e in self._skills.values()
                    if (e.get("id") or "") in _created_ids(self._created)
                ],
            }
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE_PATH.with_name(_STATE_PATH.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, _STATE_PATH)
        except OSError:
            return

    # ------------------------------------------------------------------ 读取
    def _is_installed(self, user_id: str, skill_id: str, item: dict[str, Any]) -> bool:
        """该技能对当前用户是否「已安装」。

        关闭 ≠ 卸载：显式安装过（或默认已装 / 自建）即算已安装，
        ``enabled`` 只是启用状态，不影响 installed。
        卸载 > 一切：只要在 ``_user_uninstalled`` 里，即使种子默认启用 / 显式
        安装过 / 自建，也一律视为未安装（市场按钮恢复「安装」）。
        """
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        if sid in self._user_uninstalled.get(uid, set()):
            return False
        if sid in self._user_installed.get(uid, set()):
            return True
        # 种子默认 enabled=true 的技能 = 开箱自带，视为已安装
        if bool(item.get("enabled", True)) and str(item.get("author") or "") == _SEED_AUTHOR:
            return True
        # 自建技能天然已安装
        return sid in self._created.get(uid, [])

    def _card_for(self, uid: str, item: dict[str, Any], enabled: bool) -> dict[str, Any]:
        """组装前端卡片：含 installed / can_edit / pinned 用户态字段。"""
        sid = item.get("id") or ""
        return _card(
            item,
            enabled,
            self._is_installed(uid, sid, item),
            can_edit=sid in self._created.get(uid, []),
            pinned=sid in self._user_pinned.get(uid, []),
        )

    def get(self, skill_id: str, user_id: str = "") -> dict[str, Any] | None:
        """技能详情：带当前用户的生效启用状态。技能不存在返回 None。"""
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        with self._lock:
            item = self._skills.get(sid)
            if not item:
                return None
            enabled = self._user_enabled.get(uid, {}).get(
                sid, bool(item.get("enabled", True)))
            return self._card_for(uid, item, enabled)

    def list_skills(
        self,
        *,
        keyword: str = "",
        category: str = "",
        sort: str = "default",
        enabled: str = "",            # "" 不过滤 / "true" 仅启用 / "false" 仅关闭
        offset: int = 0,
        limit: int = 10,
        user_id: str = "",
    ) -> dict[str, Any]:
        """技能市场清单：关键词 / 分类 / 启用状态过滤 + 排序 + 分页。

        sort：``default`` 综合（种子注册顺序，自建追加在后）/
        ``hot`` 最热（hot 降序）/ ``latest`` 最新（created_at 降序）。
        返回每项的 ``enabled`` 为当前用户生效状态（开关覆盖优先，否则种子默认）。
        """
        kw = (keyword or "").strip().lower()
        cat = (category or "").strip()
        uid = str(user_id or "")

        with self._lock:
            items = list(self._skills.values())
            overrides = self._user_enabled.get(uid, {})

        if cat:
            items = [e for e in items if (e.get("category") or "") == cat]
        if kw:
            items = [e for e in items if _match(e, kw)]
        if enabled in ("true", "false"):
            want = enabled == "true"
            items = [e for e in items if overrides.get(
                e.get("id") or "", bool(e.get("enabled", True))) is want]

        if sort == "hot":
            items = sorted(items, key=lambda e: int(e.get("hot") or 0), reverse=True)
        elif sort == "latest":
            items = sorted(items, key=_key_time, reverse=True)
        # default：保持注册顺序（种子在前、自建追加在后）

        total = len(items)
        page = items[offset:offset + limit]
        return {
            "items": [
                self._card_for(uid, e, overrides.get(e.get("id") or "", bool(e.get("enabled", True))))
                for e in page
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def categories(self) -> list[str]:
        """动态分类：种子 + 用户自建里出现过的分类，按展示顺序去重。"""
        with self._lock:
            cats = {(e.get("category") or "").strip() for e in self._skills.values()}
        cats.discard("")
        ordered = [c for c in CATEGORY_ORDER if c in cats]
        tail = sorted(cats - set(CATEGORY_ORDER))
        return ordered + tail

    def mine(self, user_id: str) -> dict[str, Any]:
        """已安装技能：当前用户安装过的全部技能（种子默认自带 + 显式安装 + 自建）。

        关闭 ≠ 卸载：关闭中的技能**仍在列表**，只是 ``enabled`` 为 False，
        前端以开关（灰/蓝）展示状态，重新开启无需再次安装。
        排序：置顶技能在前（保序：最新置顶在最前），其余按 created_at 降序。
        """
        uid = str(user_id or "")
        with self._lock:
            overrides = self._user_enabled.get(uid, {})
            pinned = list(self._user_pinned.get(uid, []))
            items = [dict(e) for e in self._skills.values()
                     if self._is_installed(uid, e.get("id") or "", e)]
        pinned_items = [e for e in items if (e.get("id") or "") in pinned]
        rest = [e for e in items if (e.get("id") or "") not in pinned]
        pinned_items.sort(key=lambda e: pinned.index(e.get("id") or ""))
        rest.sort(key=_key_time, reverse=True)
        items = pinned_items + rest
        return {"items": [
            self._card_for(uid, e, overrides.get(e.get("id") or "", bool(e.get("enabled", True))))
            for e in items
        ]}

    # ------------------------------------------------------------------ 写
    def create(self, user_id: str, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        """新建技能：author = 当前用户，默认启用，登记进该用户的自建列表。"""
        name = (payload.get("name") or "").strip()
        desc = (payload.get("description") or "").strip()
        category = (payload.get("category") or "").strip()
        if not name or not desc or not category:
            raise ValueError("技能新建需要 name / description / category 均非空")
        tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
        icon = (payload.get("icon") or "i-zap").strip()
        page = (payload.get("page") or "").strip()
        sid = f"sk-{uuid.uuid4().hex[:10]}"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        skill = _normalize({
            "id": sid,
            "name": name,
            "author": (username or "").strip() or "匿名用户",
            "description": desc,
            "tags": tags,
            "category": category,
            "hot": int(payload.get("hot") or 0),
            "enabled": True,
            "icon": icon,
            "page": page,
            "created_at": now,
        }, author="")
        with self._lock:
            self._skills[sid] = skill
            self._created.setdefault(str(user_id), []).append(sid)
        self.save_state()
        return dict(skill)

    def set_enabled(self, user_id: str, skill_id: str, enabled: bool) -> bool | None:
        """切换启用 / 关闭。技能不存在返回 None；成功返回生效后的启用状态。

        只记录用户级覆盖，**不改动种子文件**（对齐 WorkBuddy「开关仅记配置」）。
        启用（enabled=True）时同时登记为「已安装」；关闭（enabled=False）**不移除**
        已安装记录 —— 关闭 ≠ 卸载，重新启用无需再次安装。
        """
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        with self._lock:
            if sid not in self._skills:
                return None
            self._user_enabled.setdefault(uid, {})[sid] = bool(enabled)
            if enabled:
                self._user_installed.setdefault(uid, set()).add(sid)
                # 重新安装 = 解除之前的卸载标记，恢复「已安装」判定
                self._user_uninstalled.get(uid, set()).discard(sid)
        self.save_state()
        return bool(enabled)

    def uninstall(self, user_id: str, skill_id: str) -> bool | None:
        """卸载技能。技能不存在返回 None；成功返回 True。

        卸载语义（对齐 WorkBuddy）：
        - 记入 ``_user_uninstalled``，``_is_installed`` 优先判 False，
          种子默认启用 / 显式安装过 / 自建的技能卸载后市场按钮都恢复「安装」；
        - 同时清掉该用户对该技能的启用覆盖与置顶记录（卸载 = 移除安装态）；
        - 自建技能卸载时连同注册表一起删除（市场不再出现该技能）；
        - 已安装集合同步移除，避免卸载后残留「已安装」标记。
        """
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        with self._lock:
            if sid not in self._skills:
                return None
            self._user_uninstalled.setdefault(uid, set()).add(sid)
            self._user_installed.get(uid, set()).discard(sid)
            self._user_enabled.get(uid, {}).pop(sid, None)
            lst = self._user_pinned.get(uid, [])
            if lst and sid in lst:
                lst.remove(sid)
            # 自建技能卸载：从市场删除（注册表移除 + 自建登记移除）
            if sid in self._created.get(uid, []):
                self._created.get(uid, []).remove(sid)
                self._skills.pop(sid, None)
        self.save_state()
        return True

    def edit(self, user_id: str, skill_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """编辑技能。技能不存在返回 None；非当前用户自建抛 PermissionError。

        仅 author = 当前用户的自建技能可改 name / description / tags / category /
        icon / page。内置（种子）技能不可编辑。
        """
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        name = (payload.get("name") or "").strip()
        desc = (payload.get("description") or "").strip()
        category = (payload.get("category") or "").strip()
        if not name or not desc or not category:
            raise ValueError("技能编辑需要 name / description / category 均非空")
        with self._lock:
            item = self._skills.get(sid)
            if not item:
                return None
            if sid not in self._created.get(uid, []):
                raise PermissionError("仅创建者可编辑该技能")
            item["name"] = name
            item["description"] = desc
            item["tags"] = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
            item["category"] = category
            item["icon"] = (payload.get("icon") or "i-zap").strip()
            item["page"] = (payload.get("page") or "").strip()
        self.save_state()
        return dict(item)

    def set_pinned(self, user_id: str, skill_id: str, pinned: bool) -> bool | None:
        """置顶 / 取消置顶。技能不存在返回 None；成功返回生效后的置顶状态。

        置顶列表保序：置顶时插到最前（重复置顶 = 重新置顶到最前），
        取消置顶时移除。``mine()`` 按此顺序把置顶技能排在最前。
        """
        sid = str(skill_id or "").strip()
        uid = str(user_id or "")
        with self._lock:
            if sid not in self._skills:
                return None
            lst = self._user_pinned.setdefault(uid, [])
            if pinned:
                if sid in lst:
                    lst.remove(sid)
                lst.insert(0, sid)
            else:
                if sid in lst:
                    lst.remove(sid)
        self.save_state()
        return bool(pinned)


def _created_ids(created: dict[str, list[str]]) -> set[str]:
    """聚合所有用户的自建技能 id 集合（用于判定需持久化的自建技能）。"""
    return {sid for lst in created.values() for sid in lst}


def _normalize(item: dict[str, Any], *, author: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "author": str(item.get("author") or author or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "tags": [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()],
        "category": str(item.get("category") or "").strip(),
        "hot": int(item.get("hot") or 0),
        "enabled": bool(item.get("enabled", True)),
        "icon": str(item.get("icon") or "i-zap").strip(),
        "page": str(item.get("page") or "").strip(),
        "created_at": str(item.get("created_at") or "").strip(),
    }


def _card(skill: dict[str, Any], enabled: bool, installed: bool = True,
          *, can_edit: bool = False, pinned: bool = False) -> dict[str, Any]:
    card = dict(skill)
    card["enabled"] = bool(enabled)
    card["installed"] = bool(installed)
    card["can_edit"] = bool(can_edit)
    card["pinned"] = bool(pinned)
    return card


def _match(skill: dict[str, Any], kw: str) -> bool:
    haystack = " ".join([
        skill.get("name") or "",
        skill.get("description") or "",
        " ".join(skill.get("tags") or []),
    ]).lower()
    return kw in haystack


def _key_time(skill: dict[str, Any]) -> str:
    return skill.get("created_at") or ""


# 模块级单例：接口与前端共用同一份内存数据。
skill_registry = SkillRegistry()
skill_registry.load_seed()
skill_registry.load_state()
