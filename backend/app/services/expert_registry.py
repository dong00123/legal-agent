"""专家内存注册表（服务层，单一数据源）。

对齐 ``agents/protocol/registry.py`` 的「派生不另写一份」风格：
种子专家在**启动时**从 ``app/data/experts_seed.json`` 一次性加载进内存，
所有专家接口只读这一份注册表，不落业务库。

用户维度的状态（自建专家 / 收藏）以 ``user_id`` 为键存在进程内：
- 自建专家合并进全局清单（``author`` = 创建者），并登记进该用户的 ``created``；
- 收藏只登记「user_id → expert_id」关系，读取时实时从全局清单还原完整卡片。

⚠ 进程重启后自建与收藏回到种子态。这是有意的取舍：专家是门面层数据，
与 ``registry.products()`` 的派生风格一致，不承担持久化职责
（需要持久化时再落表即可，接口形状不变）。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_SEED_PATH = Path(__file__).resolve().parents[2] / "app" / "data" / "experts_seed.json"

# 分类展示顺序（与前端 chip 一致并扩展）。动态分类按此顺序排，
# 未在表中的分类追加到尾部，避免新分类凭空消失。
CATEGORY_ORDER = ["诉讼", "合同", "劳动", "知产", "合规", "婚姻家事", "公司商事", "刑事"]

_SEED_AUTHOR = "LegalAgent"


class ExpertRegistry:
    """专家注册表。线程安全：收藏 / 自建 / 清单读取共用一把锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._experts: dict[str, dict[str, Any]] = {}
        self._created: dict[str, list[str]] = {}       # user_id -> [expert_id]（自建，保序）
        self._favorites: dict[str, set[str]] = {}      # user_id -> {expert_id}

    # ------------------------------------------------------------------ 加载
    def load_seed(self) -> int:
        """启动时加载种子数据，返回加载条数。文件缺失或格式错误时返回 0。"""
        if not _SEED_PATH.exists():
            return 0
        try:
            raw = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
            items = raw.get("experts") or []
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
            return 0
        with self._lock:
            for item in items:
                eid = str(item.get("id") or "").strip()
                if not eid:
                    continue
                self._experts[eid] = _normalize(item, author=_SEED_AUTHOR)
        return len(self._experts)

    # ------------------------------------------------------------------ 读取
    def get(self, expert_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._experts.get(str(expert_id).strip())
            return dict(item) if item else None

    def persona_for(self, expert_id: str) -> str:
        """人设描述（AI 助手注入用）。专家不存在时返回空串，调用方按普通助手处理。"""
        item = self.get(expert_id)
        return (item or {}).get("description") or ""

    def enhance_for(self, expert_id: str) -> dict[str, Any]:
        """专家增强配置：定向检索 + 专属回答模板。

        返回只含**已配置**的键（``domains`` / ``retrieval_hints`` /
        ``answer_template``）；专家不存在或全未配置时返回空 dict，
        调用方按普通 AI 助手处理（改前行为逐字不变）。
        """
        item = self.get(expert_id)
        if not item:
            return {}
        out: dict[str, Any] = {}
        domains = [str(d).strip() for d in (item.get("domains") or []) if str(d).strip()]
        hints = [str(h).strip() for h in (item.get("retrieval_hints") or []) if str(h).strip()]
        tpl = str(item.get("answer_template") or "").strip()
        if domains:
            out["domains"] = domains
        if hints:
            out["retrieval_hints"] = hints
        if tpl:
            out["answer_template"] = tpl
        return out

    def is_favorited(self, user_id: str, expert_id: str) -> bool:
        """当前用户是否已收藏该专家（列表 / 详情卡片渲染用）。"""
        uid = str(user_id or "")
        with self._lock:
            favs = self._favorites.get(uid) or set()
            return str(expert_id or "").strip() in favs

    def list_experts(
        self,
        *,
        keyword: str = "",
        category: str = "",
        sort: str = "default",
        offset: int = 0,
        limit: int = 10,
        user_id: str = "",
    ) -> dict[str, Any]:
        """专家清单：关键词 / 分类过滤 + 排序 + 分页。

        sort：``default`` 综合（种子注册顺序，自建追加在后）/
        ``hot`` 最热（hot 降序）/ ``latest`` 最新（created_at 降序）。
        """
        kw = (keyword or "").strip().lower()
        cat = (category or "").strip()

        with self._lock:
            items = list(self._experts.values())
            favs = self._favorites.get(user_id) or set()

        if cat:
            items = [e for e in items if (e.get("category") or "") == cat]
        if kw:
            items = [e for e in items if _match(e, kw)]

        if sort == "hot":
            items = sorted(items, key=lambda e: int(e.get("hot") or 0), reverse=True)
        elif sort == "latest":
            items = sorted(items, key=_key_time, reverse=True)
        # default：保持注册顺序（种子在前、自建追加在后）

        total = len(items)
        page = items[offset:offset + limit]
        return {
            "items": [_card(e, (e.get("id") or "") in favs) for e in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def categories(self) -> list[str]:
        """动态分类：种子 + 用户自建里出现过的分类，按展示顺序去重。"""
        with self._lock:
            cats = {(e.get("category") or "").strip() for e in self._experts.values()}
        cats.discard("")
        ordered = [c for c in CATEGORY_ORDER if c in cats]
        tail = sorted(cats - set(CATEGORY_ORDER))
        return ordered + tail

    def mine(self, user_id: str) -> dict[str, Any]:
        """我的专家：自建 + 收藏（收藏按收藏先后返回完整卡片）。"""
        uid = str(user_id or "")
        with self._lock:
            created_ids = list(self._created.get(uid) or [])
            fav_ids = list(self._favorites.get(uid) or [])
            experts = self._experts
            created = [dict(experts[i]) for i in created_ids if i in experts]
            favorites = [dict(experts[i]) for i in fav_ids if i in experts]
        return {
            "created": [_card(e, e.get("id") in fav_ids) for e in created],
            "favorites": [_card(e, True) for e in favorites],
        }

    # ------------------------------------------------------------------ 写
    def create(self, user_id: str, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        """新建专家：author = 当前用户，登记进该用户的自建列表。"""
        name = (payload.get("name") or "").strip()
        desc = (payload.get("description") or "").strip()
        category = (payload.get("category") or "").strip()
        if not name or not desc or not category:
            raise ValueError("专家新建需要 name / description / category 均非空")
        tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
        eid = f"exp-{uuid.uuid4().hex[:10]}"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        expert = _normalize({
            "id": eid,
            "name": name,
            "author": (username or "").strip() or "匿名用户",
            "description": desc,
            "tags": tags,
            "category": category,
            "hot": int(payload.get("hot") or 0),
            "created_at": now,
        }, author="")
        with self._lock:
            self._experts[eid] = expert
            self._created.setdefault(str(user_id), []).append(eid)
        return dict(expert)

    def toggle_favorite(self, user_id: str, expert_id: str) -> bool | None:
        """收藏 / 取消收藏。专家不存在返回 None；切换后返回是否已收藏。"""
        eid = str(expert_id or "").strip()
        uid = str(user_id or "")
        with self._lock:
            if eid not in self._experts:
                return None
            favs = self._favorites.setdefault(uid, set())
            if eid in favs:
                favs.discard(eid)
                return False
            favs.add(eid)
            return True


def _normalize(item: dict[str, Any], *, author: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "author": str(item.get("author") or author or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "tags": [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()],
        "category": str(item.get("category") or "").strip(),
        "hot": int(item.get("hot") or 0),
        "created_at": str(item.get("created_at") or "").strip(),
        # 专家增强配置（可选，缺省即普通 AI 助手行为逐字不变）：
        # - domains：定向检索领域，对齐 rag/domain.py 的 DOMAINS 枚举；
        # - retrieval_hints：检索增强词，追加进检索 query 提升召回聚焦度；
        # - answer_template：专属回答模板，注入生成阶段的回答结构指令。
        "domains": [str(d).strip() for d in (item.get("domains") or []) if str(d).strip()],
        "retrieval_hints": [str(h).strip() for h in (item.get("retrieval_hints") or []) if str(h).strip()],
        "answer_template": str(item.get("answer_template") or "").strip(),
    }


def _card(expert: dict[str, Any], favorited: bool) -> dict[str, Any]:
    card = dict(expert)
    card["favorited"] = bool(favorited)
    return card


def _match(expert: dict[str, Any], kw: str) -> bool:
    haystack = " ".join([
        expert.get("name") or "",
        expert.get("description") or "",
        " ".join(expert.get("tags") or []),
    ]).lower()
    return kw in haystack


def _key_time(expert: dict[str, Any]) -> str:
    return expert.get("created_at") or ""


# 模块级单例：接口与 AI 助手人设注入共用同一份内存数据。
expert_registry = ExpertRegistry()
expert_registry.load_seed()
