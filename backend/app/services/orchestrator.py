"""任务编排服务：把一次 ``graph.run`` 包成完整的任务生命周期。

这一层要回答的问题，是"直接在接口里调 ``graph.run`` 回答不了的那几个"：

1. **任务什么时候开始、跑到哪一步、结束没有** —— 都要落到 ``TaskRecord``，
   否则刷新页面就丢；WebSocket/SSE 断线后也接不回来。
2. **中途能看见进度** —— 通过 ``Tracer.subscribe`` 把事件推给订阅者（SSE 用），
   而不是让前端轮询数据库。
3. **失败也要有交付物** —— 节点异常不能让接口 500 了事。已跑出来的部分
   （哪怕只到 StatuteNode）对律师仍有价值，所以异常路径也要落库并返回
   已完成的部分，同时把失败原因写进证据缺口。
4. **归档不阻塞交付** —— ``MemoryNode`` 的归档写入失败不该影响报告，
   它是"事后沉淀"，不是交付物的一部分。
5. **沉淀要落独立表，不能只留在快照 JSON 里** —— 引用终审结果与文书草稿
   各写 ``la_citation`` / ``la_draft``（见 :meth:`Orchestrator._settle`）。
   快照是会被裁剪的，而"哪条引用被拦了"正是最长的部分。

与 ``TaskService`` 的分工：本模块负责**流程**（何时建记录、何时写进度、何时收尾），
``TaskService`` 负责**存储**（怎么落库、怎么裁剪、怎么查询）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.agents.graph import run as graph_run
from backend.agents.harness import Harness
from backend.agents.harness.router import INTENT_ROUTES
from backend.agents.state import LegalState, new_state
from backend.app.config import settings
from backend.app.services.task_service import task_service

logger = logging.getLogger("legal_agent.orchestrator")

# 报告正文进入 ``result`` 的字段。与 ``TaskRecord.result`` 的用途一致：
# 交付物 + 引用清单，不含 Trace（Trace 单独一列）。
RESULT_FIELDS = ("report", "confidence", "citations", "citation_summary",
                 "citation_check", "evidence_gap", "disclaimer", "risk_points",
                 "missing_clauses", "strategy", "draft", "revision_draft",
                 "draft_type", "limitation_check", "knowledges", "statutes", "cases",
                 # 命中相似问题缓存时的溯源信息：这份结论**来自哪次任务**，
                 # 前端与任务中心都要能看见，否则复用会变成"来历不明的答案"。
                 "cache_hit")


def _cache_wanted(req: "RunRequest") -> bool:
    """本次是否该去查相似问题缓存。

    判断本身只有一行，单独成函数是为了让它**可被单测触达**：
    它留在 ``execute()`` 中间时，``no_cache`` 到底有没有生效只能靠端到端跑一次
    才知道 —— 而这个开关恰恰是"缓存命中之后律师唯一的出路"，失效了没人发现。
    """
    return bool(settings.QUERY_CACHE_ENABLED and not req.no_cache)


def _apply_cache_hit(state: LegalState, hit: dict[str, Any], harness: Harness | None) -> None:
    """把缓存里的结论搬进本次 State。

    只搬**交付物字段**，不搬 route/steps/trace —— 那些是本次执行的记录，
    抄一份会让轨迹面板显示"跑了 9 个节点"而实际一个都没跑。
    """
    result = dict(hit.get("result") or {})
    for key in RESULT_FIELDS:
        if key == "cache_hit":
            continue
        val = result.get(key)
        if val not in (None, "", [], {}):
            state[key] = val  # type: ignore[literal-required]
    state["cache_hit"] = {
        "task_id": hit.get("task_id", ""),
        "similarity": hit.get("similarity", 0.0),
        "cached_query": hit.get("cached_query", ""),
        "cached_intent": hit.get("cached_intent", ""),
        "from_cache": True,
    }
    # 意图沿用缓存那次的：报告里的"业务类型"与结论要对得上
    if hit.get("cached_intent") and not state.get("intent"):
        state["intent"] = hit["cached_intent"]  # type: ignore[typeddict-item]
    if harness is not None:
        harness.tracer.info(
            "Orchestrator", "cache_hit",
            f"命中相似问题缓存（相似度 {hit.get('similarity', 0):.3f}，"
            f"源自任务 {hit.get('task_id', '')}）：{str(hit.get('cached_query') or '')[:80]}"
            "；本次未重跑检索与生成，如需重新分析请在提问中补充新的事实",
        )

# 归档节点：**不在** INTENT_ROUTES 里，由本模块在流程结束后单独触发。
ARCHIVE_NODE = "MemoryNode"


@dataclass
class RunRequest:
    """一次任务请求。字段与 ``new_state`` 对齐，避免两层各有一套参数名。

    **``task_id`` 是"在既有任务上继续跑"，不是"再建一条任务"。**
    留空（默认）时 :func:`new_state` 会现生成一个，这是接口直接调用的路径。
    worker 走的是另一条路：它先从库里认领一行 ``pending`` 任务，
    再把 ``task_id`` 带进来执行 —— 不带的话 ``execute`` 会**另起一个 task_id**，
    于是被认领的那行永远停在 ``running``（前端一直转圈），
    而结果落在一个凭空多出来的新任务上，两边都"看起来正常"。
    """

    query: str
    task_id: str = ""
    case_id: str = ""
    user_id: str = ""
    team_id: str = ""
    material_text: str = ""
    # 范本合同正文（**比对基准，可选**）。只被 ``ReviewNode`` 消费：
    # 有它时按范本逐条比对，缺失/改写条款对着它判。
    #
    # ⚠️ 它**不算 "有材料"**：``_has_material`` 只看 ``material_text`` /
    #    ``material_paths`` / ``material_ids``，只有范本仍然是 400。
    #    这是有意的 —— 没有待审合同，"审查"对象就不存在。
    #
    # ⚠️ 它**必须折进缓存指纹**，见 :func:`_cache_material`。
    template_text: str = ""
    # 审查立场（甲方/乙方/……），留空=中立。决定风险点往哪边判、修改建议替谁争取。
    # 界面显式选的那一方走这里；留空时 ReviewNode 会退回从 query 正则抓（老路）。
    # ⚠️ 和范本一样**必须折进缓存指纹**，见 :func:`_cache_material`。
    client_role: str = ""
    material_paths: list[str] = field(default_factory=list)
    doc_type: str = "unknown"
    # 调用方**显式钉住**的意图（留空 = 由 IntentNode 判定）。
    #
    # ⚠ 这个字段原先只写进任务表当元信息，**从来没有进过 State**：``execute()``
    #   构造 State 时没传它，而 ``IntentNode`` 又无条件重跑分类并覆写
    #   ``state["intent"]``。于是 ``ChatRequest.intent`` 注释里那句
    #   「显式指定意图」是不成立的 —— 传什么都会被分类器覆盖掉，
    #   而界面上看不出任何异常（路由只是"恰好"和你想的不一样）。
    #   ``/contract/review`` 之所以一直走对，靠的是 ``doc_type="contract"``
    #   在 ``IntentRouter.classify`` 里拿的那 0.5 分加权，不是这个字段。
    #
    #   现在它经 ``execute()`` 落到 ``state["intent_override"]``，
    #   由 IntentNode 优先采用（见 ``nodes/intent_node.py``）。
    intent: str = ""
    # 检索条数（0 = 用节点默认值）。类案检索页的「返回条数」面板走这里。
    top_k: int = 0
    # 用户**显式指定**的文书类型（complaint / defense / opinion / revision / appeal）。
    #
    # 它和 ``doc_type`` 是两件事：``doc_type`` 说的是"材料是什么"
    # （contract / litigation / consult），这个是"要产出哪种文书"。
    # 留空时 :func:`_default_draft_type` 按关键词猜。
    #
    # **必须有这个字段。** ``POST /doc/draft`` 早就校验了用户传的 ``doc_type``，
    # 却在构造请求时把它丢掉了 —— 而接口 docstring 写着"``doc_type``
    # **优先于关键词推断**"。实际行为是：用户选「答辩状」，节点按提问关键词
    # 猜成「民事起诉状」，接口还把结果照实回报成起诉状。
    # 交付物形态错了，而校验、日志、响应体三处都显示"一切正常"。
    draft_type: str = ""
    # 交付物形态（``report`` / ``conversational``），见 ``LegalState.answer_style``。
    #
    # 命名上与 State 同键，是为了让「入口 → RunRequest → State」这条链上
    # 只有一个词 —— 这一层已经出过好几次"字段名对不上就静默用默认值"的事。
    # ⚠ **必须折进缓存指纹**（见 :func:`_cache_material`），它改变交付物。
    answer_style: str = ""
    # 专家人设（来自 ``/experts`` 的 ``description``，进入专家对话时由入口注入）。
    # 与 State 同键；留空 = 普通 AI 助手。
    # ⚠ **必须折进缓存指纹**（见 :func:`_cache_material`）：人设改变答案，
    #   不折进去会让不同专家问同一句话时静默领回同一个缓存答案。
    expert_persona: str = ""
    # 专家增强配置（与 ``expert_persona`` 同源，来自 ``expert_registry.enhance_for``）：
    # - expert_domains：定向检索领域（对齐 rag/domain.py 的 DOMAINS 枚举）；
    # - expert_hints：检索增强词（追加进检索 query 提升召回聚焦度）；
    # - expert_template：专属回答模板（注入生成阶段的回答结构指令）。
    # 三者全空时专家与普通助手逐字一致。
    # ⚠ **同样必须折进缓存指纹**：它们改变检索口径与回答结构，不折进去
    #   会让「配置了增强的专家」与「普通助手/未配置专家」问同一句话时
    #   静默领回同一个缓存答案。
    expert_domains: list[str] = field(default_factory=list)
    expert_hints: list[str] = field(default_factory=list)
    expert_template: str = ""
    use_graph: bool = True
    # 强制重跑：跳过相似问题缓存，本次一定走完整链路。
    #
    # 为什么律师需要一个开关：缓存命中的是**上一次的结论**，而上次之后
    # 事实、法规、口径都可能变；他若觉得这份结论不对，唯一能做的是换个说法
    # 再问一遍 —— 而换个说法极可能又命中同一条缓存，等于卡死。
    # 缓存本身没有错，错在"命中之后无法拒绝"，所以入口必须存在。
    # （`execute()` 里那句 getattr(req, "no_cache", False) 早就写好了，
    #   但字段一直没人定义，于是这个开关从来没生效过。）
    no_cache: bool = False
    client: str = "api"
    username: str = ""
    ip: str = ""

    # ------------------------------------------------------------------ 序列化
    def to_payload(self) -> dict[str, Any]:
        """转成能进 JSON 列的字典（入队快照用）。

        用 ``dataclasses.fields`` 自动遍历而不是手写字段名：手写的那份
        每加一个字段就得记得同步改，忘了的表现是**新字段在 worker 侧静默变成默认值**
        —— 例如将来给 ``RunRequest`` 加个 ``temperature``，入队时带了、执行时没有，
        而两边都不报错。这一层没有别的办法保证对齐，只能让"字段清单"只有一份。
        """
        import dataclasses

        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}

    @classmethod
    def from_payload(cls, payload: Any) -> RunRequest:
        """从入队快照重建。

        两条宽容规则，都是为了"API 与 worker 不是同一时刻部署的"：

        - **不认识的键忽略**（不抛）：worker 是旧版、API 加了新字段时，
          报错退出会让整个任务卡在队列里反复重试，而旧 worker 其实跑得动。
        - **缺的键用默认值**（``query`` 除外）：``query`` 是唯一的必填项，
          它都没有就说明这条快照本身就是坏的，抛 ``ValueError``
          让调用方 ``release()`` 放回队列，而不是拿一个空查询去跑出一条空报告。
        """
        import dataclasses

        if not isinstance(payload, dict):
            raise ValueError(f"请求快照不是字典，而是 {type(payload).__name__}")
        known = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in payload.items() if k in known}
        if not str(kw.get("query") or "").strip():
            raise ValueError("请求快照缺少 query，无法重建")
        if not isinstance(kw.get("material_paths"), list):
            kw["material_paths"] = []
        return cls(**kw)


# 缓存材料指纹的版本号。**改指纹的算法时必须一起改它** —— 它唯一的作用是
# 让旧算法存下的条目一次性全部失效，而不是留在桶里被新请求误领。
#
# v1 → v2（2026-10-07）：v1 只在 `material_text` 非空时才写指纹，而 AI 助手 /
# 类案检索 / 文书中心这些入口都不传 material_text，于是它们的指纹一律是空串，
# 全部挤进同一个桶（详见 :func:`_cache_material`）。
#
# v2 → v3（2026-10-07）：指纹加了 ``style=`` 维度。**这一跳不能省**：
# ``material_fp`` 是整串指纹的一部分，旧条目里没有 ``style=`` 这一段，
# 若版本号不动，助手的 conversational 请求会与合同审查页 v2 时代的条目
# 撞上同一条（两者都是"无材料 + 同文本 + 同 intent"），静默领回报告型交付物。
# **回滚本改动时不回退版本号** —— v2 的条目本来就没有形态维度，留着就是等着被误领。
FP_VERSION = "v3"


def _cache_material(req: "RunRequest") -> str:
    """喂给 ``query_cache`` 算指纹的那段材料。

    **范本、立场、钉住的意图、召回条数，四样都必须折进来。** 缓存靠
    ``fingerprint(material_text)`` 做 ``material_fp``（:func:`query_cache.lookup`
    里是**精确比对**，不是相似度），用来拒绝"材料不一致"的命中。少折一样，
    命中就是错的，而界面上只会写着"命中缓存"、一切正常：

    - 「同一份待审合同 + 不同范本」→ 命中第一次的缓存，返回**不带范本**的旧结论；
    - 「同一份合同 + 不同立场」→ 同上，返回**按另一个立场**审出来的结论。
      而这条更隐蔽：两份结论都言之成理、都引对了法条，光看结果发现不了，
      只有对着"我选的是乙方、它怎么在替甲方说话"才看得出来。
    - 「同一句话 + 钉住的意图不同」→ **两个页面的缓存互相串用**。类案检索页
      钉着 ``case_search``，AI 助手页不钉（交给分类器），两者提问文本一样时
      指纹也一样，先跑的那次被后跑的那次原样领走。而 AI 助手的路线里
      常没有 CaseNode（动态规划按问题裁剪），于是类案检索页领到一份
      **没有命中列表**的报告，底部照旧写着"命中缓存"。
    - 「同一句话 + 面板里选的返回条数不同」→ 面板形同虚设：从 3 改到 10，
      第二次直接命中第一次的缓存，回来还是 3 条。

    后两条是 2026-10-07 补的。``top_k`` 与显式 ``intent`` 是那一天才拿到
    通往 State 的路的（见 ``RunRequest`` 上两个字段的注释），而指纹没跟着动 ——
    管子接上了、缓存还在原地堵着，现象和改动前一模一样。

    用 ``\\x00`` 而不是 ``\\n`` 当分隔符：正文里换行是常态，
    换行拼接会让「A 的尾 + B」和「A 尾换行 + B」撞成同一个指纹。
    （``query_cache._norm`` 只吃标点与空白，不吃 ``\\x00``，这个分隔符能活到哈希那一步。）

    **五个维度一律写全，值为空的也写**（``intent=`` / ``top_k=0``），并带版本前缀。
    不写全就会漏，而且是往最坏的方向漏：

    ``lookup`` 里那句 ``fp = fingerprint(material_text) if material_text else ""``
    意味着**没材料的请求算出来是空串**。而"没材料"恰恰是多数入口的常态
    （AI 助手、类案检索、文书中心都不传 ``material_text``）。
    于是空串对空串，上面那道 ``fp != meta.material_fp`` 的闸门对它们整个失效 ——
    **所有无材料的请求共用一个指纹桶**。提问文本一样时（相似度阈值 0.90，
    同一句话必然过线），先跑的那次被后跑的那次原样领走。

    2026-10-07 实测撞上过一次：在类案检索页问过的问题，AI 助手页再问一遍，
    领回一份 ``case_search`` 的结论 —— ``Route=['MemoryNode']``（一个分析节点
    都没跑）、``intent`` 被改写成 ``case_search``，而界面上只写着"命中缓存"。

    所以这里加了 ``FP_VERSION``：它让**改动前存下的条目一律不再命中**。
    这是**有意作废**，不是副作用 —— 那些条目的指纹本来就没编码意图与条数，
    留在桶里就是等着被别的页面领走。一次性的重建成本，换掉一类查不出来的错。
    （旧实现里那句"空值不写进指纹、保持键形状不变"正是为了不静默作废；
    事实证明那份兼容得让位于正确性。缓存只是优化，结论错了才是真代价。）
    """
    text = req.material_text or ""
    tpl = req.template_text or ""
    role = req.client_role or ""
    pin = str(getattr(req, "intent", "") or "")
    topk = int(getattr(req, "top_k", 0) or 0)
    style = str(getattr(req, "answer_style", "") or "report")
    persona = str(getattr(req, "expert_persona", "") or "")
    # 专家增强配置（定向检索 / 专属回答模板）也必须参与指纹：它们改变
    # 检索口径与回答结构，不折进去会让配置了增强的专家与普通助手
    # 问同一句话时静默领回同一个缓存答案。
    e_domains = ",".join(str(d) for d in (getattr(req, "expert_domains", None) or []))
    e_hints = ",".join(str(h) for h in (getattr(req, "expert_hints", None) or []))
    e_tpl = str(getattr(req, "expert_template", "") or "")
    # ``键=值`` 的形式而非光写值：「意图恰好叫 10」这种事一旦发生，
    # 它和同值的 ``top_k`` 会撞成同一个指纹。
    return "\x00".join([
        FP_VERSION, text, tpl,
        f"role={role}", f"intent={pin}", f"top_k={topk}", f"style={style}",
        f"persona={persona}",
        f"e_domains={e_domains}", f"e_hints={e_hints}", f"e_tpl={e_tpl}",
    ])


@dataclass
class RunOutcome:
    task_id: str
    state: LegalState
    ok: bool
    elapsed_ms: int
    error: str = ""
    # Agent 自己做的**关键决策**（约束 4 的"过程中"层）。
    # 三条流式入口走 SSE 的 ``decision`` 帧实时推；而 ``/doc/compose`` **不是 SSE**
    # （它一次性返回 JSON），压根没有"过程中"这个时刻 —— 它的决策只能随最终 body
    # 一起回。这一项就是那条路，别把它当成"SSE 推过的东西又存了一份"。
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, with_trace: bool = True) -> dict[str, Any]:
        s = self.state
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "intent": s.get("intent") or "",
            "route": list(s.get("route") or []),
            # 实际跑过的节点（去重保序）。``route`` 是**计划**、这一项是**实际**：
            # 回环重跑与自主委派都会让两者分叉，而律师看轨迹时问的正是"到底跑了哪几个"。
            # ⚠ 它**不是**步数：同一节点回环跑两遍仍只算一个，预算是下面的 ``steps``。
            "used_nodes": list(s.get("used_nodes") or []),
            "steps": int(s.get("steps") or 0),
            "confidence": s.get("confidence") or 0.0,
            "citation_check": s.get("citation_check") or "pending",
            "citation_summary": s.get("citation_summary") or {},
            # 命中缓存时的溯源（来自哪次任务、相似度多少）。没有这一项，
            # 律师看到的是一份"凭空出现的答案"，无法判断该不该信。
            "cache_hit": s.get("cache_hit") or None,
            "evidence_gap": list(s.get("evidence_gap") or []),
            "report": s.get("report") or "",
            "disclaimer": s.get("disclaimer") or "",
            "counts": {
                "clauses": len(s.get("clauses") or []),
                "statutes": len(s.get("statutes") or []),
                "cases": len(s.get("cases") or []),
                "knowledges": len(s.get("knowledges") or []),
                "risk_points": len(s.get("risk_points") or []),
                "citations": len(s.get("citations") or []),
            },
        }
        # 一行执行摘要（约束 4 的"结束层"）：SSE 的 done 帧与前端那一行都读它，
        # 让律师不点开轨迹也知道"这次花了多久、动了几个节点"。
        #
        # 命中缓存时**不说"N 步"**：那条路上 ``graph_run`` 压根没被调用（见 execute），
        # ``used_nodes`` 是空的，照搬会渲染成"本次执行：0 步"——读起来像流程空转了一遍，
        # 而实际是"这次没跑，用的上次核验过的结论"。两者必须能一眼分开。
        _secs = self.elapsed_ms / 1000
        out["summary"] = (
            f"本次执行：命中缓存 · {_secs:.1f}s" if out["cache_hit"]
            else f"本次执行：{len(out['used_nodes'])} 步 · {_secs:.1f}s"
        )
        # 决策留痕。**只在真有决策时才带上**：多数任务一条都没有（LLM 降级时恒无），
        # 给每个响应都塞一个空数组只会让"这次到底想没想过"更难一眼看出来。
        if self.decisions:
            out["decisions"] = list(self.decisions)
        if self.error:
            out["error"] = self.error
        if with_trace:
            out["trace"] = list(s.get("trace") or [])
        return out


class Orchestrator:
    """任务编排。无状态，可并发调用（每次 ``execute`` 各自独立）。"""

    async def execute(
        self,
        req: RunRequest,
        *,
        on_event: Any = None,
        persist: bool = True,
    ) -> RunOutcome:
        """跑一次完整流程。

        ``on_event`` 传入时，每条 Trace 事件都会回调它（SSE 进度推送用）。
        回调**必须快且不抛异常** —— 它跑在节点调用栈里。
        """
        started = time.monotonic()
        state = new_state(
            req.query,
            # 带 task_id 时**沿用既有记录**（worker 认领的那行），
            # 留空才现生成。见 ``RunRequest.task_id`` 的说明。
            task_id=req.task_id or None,
            case_id=req.case_id,
            user_id=req.user_id,
            team_id=req.team_id,
            material_text=req.material_text,
            template_text=req.template_text,
            client_role=req.client_role,
            material_paths=req.material_paths,
            doc_type=req.doc_type or "unknown",  # type: ignore[arg-type]
            # 显式意图与检索条数：入口钉住的这两项必须**进 State** 才有意义，
            # 只写任务表的话 IntentNode 与 CaseNode 都看不到它们。
            intent_override=req.intent or "",
            top_k=int(req.top_k or 0),
            # 交付物形态：入口不传就是 report（改前行为逐字不变）
            answer_style=req.answer_style or "report",
            # 专家人设：入口不传就是普通助手（改前行为逐字不变）
            expert_persona=req.expert_persona or "",
            # 专家增强配置（定向检索 / 专属回答模板）：与 expert_persona
            # 同源，全空时专家与普通助手逐字一致。
            expert_domains=list(req.expert_domains or []),
            expert_hints=list(req.expert_hints or []),
            expert_template=req.expert_template or "",
        )
        task_id = str(state.get("task_id") or "")

        if persist:
            task_service.create(
                task_id=task_id,
                query=req.query,
                case_id=req.case_id,
                user_id=_as_int(req.user_id),
                team_id=_as_int(req.team_id),
                source_client=req.client or "api",
                kind=_kind_of(req),
                intent=req.intent,
            )

        harness: Harness | None = None
        cache_hit: dict[str, Any] | None = None
        error = ""
        emitted: list[dict[str, Any]] = []

        def _listener(ev: Any) -> None:
            payload = ev.model_dump(mode="json") if hasattr(ev, "model_dump") else dict(ev)
            # 附上"此刻的进度与路线"，让 SSE 前端不必再轮询数据库。
            #
            # 读的是 ``harness.state`` 而不是 ``graph_run`` 的返回值：
            # 节点通过 ``harness.merge`` / ``mark_route`` **就地**写入这个 dict，
            # 所以它在流程跑到一半时就已经是"到目前为止的全貌"，
            # 而 ``graph_run`` 的返回值要等整个流程结束才拿得到。
            # 取不到就算了 —— 这是给进度条用的附带信息，
            # 不能因为算不出百分比就把业务节点带崩（监听器异常虽会被 Trace 吞掉，
            # 但白吞一条 warning 也是噪声）。
            if harness is not None:
                try:
                    live = harness.state
                    payload["progress"] = Orchestrator.progress_of(live)
                    payload["route"] = list(live.get("route") or [])
                except Exception:  # noqa: BLE001
                    pass
            emitted.append(payload)
            if on_event is not None:
                on_event(payload)

        try:
            harness = Harness.start(
                state,
                client=req.client,
                username=req.username,
                ip=req.ip,
                case_id=req.case_id,
                user_id=_as_int(req.user_id),
            )
            if on_event is not None:
                harness.tracer.subscribe(_listener)

            # ---- 产品 Agent 运行时绑定（2026-10-07）
            # 4 个产品入口（AI 助手 / 类案检索 / 合同审查 / 文书中心）从"门面 +
            # 共享固定管线"改造为"各自独立的真正 Agent"：每个任务按入口产品
            # 绑定**自己的运行时**（独立节点池 / 工具池 / 必需节点 / 自主规划与
            # 裁决提示词，见 backend/agents/products/runtime.py）。
            # 绑定后**预生成产品级自主规划**（LLM 在该产品节点池内决策本次路线），
            # IntentNode 钉意图分支据此执行；未绑定产品 / 未启用规划 / 规划失败
            # 时全部回退原静态路由 —— 行为与改造前逐字一致（前端零变动兜底）。
            from backend.agents.products.runtime import (
                resolve_runtime,
            )
            from backend.agents.products.planner import product_plan as product_plan_llm

            product = resolve_runtime(intent=req.intent or "")
            if product is not None:
                harness.product = product
                harness.product_plan = await product_plan_llm(
                    product,
                    query=req.query or "",
                    doc_type=req.doc_type or "unknown",
                    has_material=bool(req.material_text or req.material_paths),
                    material_text=req.material_text or "",
                )
                if harness.product_plan:
                    harness.tracer.info(
                        "Orchestrator", "product_bound",
                        f"产品 Agent 运行时已绑定：{product.id}，"
                        f"自主规划路线：{' → '.join(harness.product_plan)}",
                    )
                else:
                    harness.tracer.info(
                        "Orchestrator", "product_bound",
                        f"产品 Agent 运行时已绑定：{product.id}（规划未启用/回退静态路由）",
                    )

            # ---- 语义缓存：相似问题直接复用上次核验过的结论
            # 放在图**之前**：命中就不必再跑链路（这才是"拒绝重复造轮子"的收益）。
            cache_hit = None
            if _cache_wanted(req):
                cache_hit = await self._try_cache(req, harness)
            elif req.no_cache:
                # 留痕：否则"这次为什么跑了 8 个节点、上次只跑了 2 个"在轨迹里无解
                harness.tracer.info(
                    "Orchestrator", "cache_skipped",
                    "本次为强制重跑（no_cache=true），已跳过相似问题缓存，走完整链路",
                )

            # 意图显式指定时先写进 State：Planner 据此选路，
            # 否则 IntentNode 会自己再判一次、把用户指定的意图覆盖掉。
            if req.intent:
                state["intent"] = req.intent
            # 文书类型同理，且必须**在 DraftNode 之前**写进 State ——
            # DraftNode 读 ``state["draft_type"]``，为空才去猜关键词。
            # 放在这里（而不是 DraftNode 里读请求）是因为节点看不到请求，
            # 它只看 State。
            if req.draft_type:
                state["draft_type"] = req.draft_type

            if cache_hit is not None:
                # 命中：把上次的结果**搬进**本次 State（仍走后面的落库/收尾，
                # 这样"这次任务"在任务中心里是可查的一条，而不是凭空消失）
                _apply_cache_hit(state, cache_hit, harness)
            else:
                state = await graph_run(state, harness=harness, use_graph=req.use_graph)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("任务 %s 执行失败", task_id)
            # 失败要落成**证据缺口**而不是只写 error 字段：
            # 律师看到的交付物里必须有一条"这次没跑完、缺了什么"，
            # 而不是一份看起来完整、实际中途断掉的报告。
            try:
                harness.gaps("tool" if harness is None else "skill",
                             f"流程执行中断：{error}",
                             "本次交付物不完整，缺失部分需重新执行或人工补齐")
            except Exception:  # noqa: BLE001
                state.setdefault("evidence_gap", [])
                state["evidence_gap"].append({
                    "kind": "skill",
                    "reason": f"流程执行中断：{error}",
                    "impact": "本次交付物不完整，缺失部分需重新执行或人工补齐",
                    "degraded": True,
                })
        finally:
            if harness is not None:
                if on_event is not None:
                    harness.tracer.unsubscribe(_listener)
                # 收尾：置信度 + 快照。异常路径也要走 ——
                # 否则失败任务在库里永远停在 "running"，前端一直转圈。
                try:
                    harness.finish()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("任务 %s 收尾失败：%s", task_id, exc)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        state["elapsed_ms"] = elapsed_ms
        outcome = RunOutcome(
            task_id=task_id, state=state, ok=not error,
            elapsed_ms=elapsed_ms, error=error,
            # 从**主 Harness** 上取，不是从收尾那个归档 Harness 上取：
            # 归档另起一个 Harness、而且直接调节点函数（不走 ``_execute``），
            # 它的控制器从没被问过话、恒为空。
            # 取 ``visible_decisions()`` 而不是 ``state.decisions``：
            # 后者含"模型不可用/裁决超时"这类排障用语，与 SSE 推的 decision 帧
            # 必须是同一个集合（理由见 ``Controller.visible_decisions``）。
            decisions=harness.controller.visible_decisions() if harness else [],
        )

        if persist:
            self._persist(outcome)
            self._settle(outcome)
            # 缓存写入放在落库**之后**：只有确认这次结果已经存下来了，
            # 才值得让下一次相似提问复用它（否则可能指向一条查不到的任务）。
            if cache_hit is None:
                await self._maybe_cache(req, outcome)

        # 归档（MemoryNode）放在交付物落库**之后**：
        # 归档失败不能把已经跑好的报告一起拖垮。
        # 把 SSE 监听器交给它 —— 归档另起 Harness 跑，新 Tracer 默认没有订阅者，
        # 而主 Harness 的订阅在 finally 里**已经摘掉了**（见 _archive 的注释）。
        await self._archive(state, _listener if on_event is not None else None)

        return outcome

    # ---------------------------------------------------------------- 语义缓存
    @staticmethod
    async def _try_cache(req: "RunRequest", harness: Harness) -> dict[str, Any] | None:
        """查一次相似问题缓存。

        刻意**不带** ``req.intent``：多数请求是 auto（意图还没判），
        拿空意图去比对会把所有缓存条目都挡掉。命中后由 :func:`_apply_cache_hit`
        再校验一次（缓存里记了当时的意图）。
        """
        try:
            from backend.app.services import query_cache

            if not query_cache.enabled():
                return None
            return await query_cache.lookup(
                req.query, intent="", material_text=_cache_material(req),
            )
        except Exception as exc:  # noqa: BLE001
            # 缓存是**优化**：它挂了必须退化成"照常跑"，不能让任务失败
            logger.warning("缓存查询异常，按未命中处理：%s", exc)
            return None

    @staticmethod
    async def _maybe_cache(req: "RunRequest", outcome: RunOutcome) -> None:
        """把本次结论登记进缓存。准入条件不满足就**不写**。

        四条准入（详见 query_cache 模块 docstring）：
        1. 跑成功了；
        2. 引用终审**通过**（`citation_check == "passed"`）；
        3. 终审**确实核过东西**（`inspected > 0`）；
        4. 置信度达标，且不是"与法律无关"的寒暄。
        """
        try:
            from backend.app.services import query_cache

            if not query_cache.enabled() or not outcome.ok:
                return
            s = outcome.state
            intent = str(s.get("intent") or "")
            if intent in ("off_topic", ""):
                return
            if float(s.get("confidence") or 0.0) < float(settings.QUERY_CACHE_MIN_CONFIDENCE):
                logger.info("置信度 %.2f 低于缓存阈值，不缓存：%s",
                            float(s.get("confidence") or 0.0), outcome.task_id)
                return
            # ---- 2) 终审必须**通过**
            #
            # 这里原先是"只排明确失败，pending 也放行"，理由是"复用一条未核验
            # 的结论，与再跑一次又得到同样的未核验结论风险相同"。那个理由只
            # 在"pending 是偶发"时成立；实测里 pending 是**常态**（终审在多数
            # 路径上根本没跑），于是缓存里躺着的会全是未经核验的结论 ——
            # 而这正是 query_cache 模块 docstring 明写要排除的一类
            # （「引用终审没过…不入库」）。文档与实现必须一致，取严的那一头：
            # 法律结论的复用，前提是它被核过。
            check = str(s.get("citation_check") or "")
            if check != "passed":
                logger.info("引用终审未通过或未执行（%s），不缓存：%s",
                            check or "pending", outcome.task_id)
                return

            # ---- 3) 终审必须**真的核过东西**
            #
            # `passed == (blocking == 0)`，什么都没核时它天然为真 ——
            # 不堵这个口子，"零校验也通过"的结论会被当成已核验的缓存起来，
            # 比不缓存更危险（界面上写着"命中缓存"，律师会以为有人核过）。
            # inspected = 正文引用核验数 + 证据池核验数，两者都为 0 才算空转。
            summary = s.get("citation_summary") or {}
            if int(summary.get("inspected") or 0) <= 0:
                logger.info("本次终审未实际核验任何条目（inspected=0），不缓存：%s",
                            outcome.task_id)
                return
            await query_cache.store(
                req.query, task_id=outcome.task_id, intent=intent,
                material_text=_cache_material(req),
                confidence=float(s.get("confidence") or 0.0),
                citation_check=check,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入缓存异常（不影响交付物）：%s", exc)

    # ------------------------------------------------------------------ 落库
    @staticmethod
    def _persist(outcome: RunOutcome) -> None:
        s = outcome.state
        result = {k: s.get(k) for k in RESULT_FIELDS if s.get(k) not in (None, "", [], {})}
        try:
            task_service.finish(
                outcome.task_id,
                state=s,
                result=result,
                status="succeeded" if outcome.ok else "failed",
                elapsed_ms=outcome.elapsed_ms,
                error=outcome.error,
            )
        except Exception as exc:  # noqa: BLE001
            # 落库失败**不上抛**：报告已经生成好了，用户该拿到它。
            # 但必须吵闹 —— 否则任务在库里停在 running，前端永远转圈，
            # 而日志里什么都没有，排查时完全找不到线索。
            logger.error("任务 %s 落库失败（交付物仍已生成）：%s", outcome.task_id, exc)

    @staticmethod
    def _settle(outcome: RunOutcome) -> None:
        """把引用清单与文书草稿沉淀到各自的表。

        **为什么不把它们留在 ``result`` JSON 里就够了**：任务快照会被裁剪
        （``shrink_state``：列表超 400 条即截断、超长文本掐尾）。
        被引用校验拦截的那几条引用恰好是最长也最容易先被截掉的部分，
        而它们是"这份输出为什么少引了一条法条"的合规证据。
        草稿同理 —— 律师要在 EXE 上接着改，不能依赖一份被截断的 JSON。

        两个沉淀各自独立 try：引用落表失败不该把草稿也一起丢掉。
        """
        try:
            from backend.app.services.citation_service import save_from_state as save_citations

            info = save_citations(outcome.task_id, outcome.state)
            logger.info("任务 %s 引用留住：保存 %d 条（拦截 %d）",
                        outcome.task_id, info.get("saved", 0), info.get("rejected", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("引用落表失败（不影响交付物）：%s | %s", outcome.task_id, exc)

        try:
            from backend.app.services.draft_service import save_from_state as save_draft

            info = save_draft(outcome.task_id, outcome.state,
                              user_id=_as_int(outcome.state.get("user_id")))
            if info.get("saved"):
                logger.info("任务 %s 文书已存为草稿：%s（第 %s 版）",
                            outcome.task_id, info.get("draft_id"), info.get("version"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("草稿落表失败（不影响交付物）：%s | %s", outcome.task_id, exc)

        # 自动归档成案件：只归成功的（succeeded / degraded）。
        # 失败的分析没有可交付结论，归档只会制造空壳案件。
        if outcome.ok:
            try:
                from backend.app.services.case_service import auto_archive_from_state

                info = auto_archive_from_state(outcome.task_id, outcome.state,
                                               user_id=_as_int(outcome.state.get("user_id")))
                if info:
                    logger.info("任务 %s 已自动归档为案件：%s（新建=%s）",
                                outcome.task_id, info.get("case_id"), info.get("created"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("案件自动归档失败（不影响交付物）：%s | %s", outcome.task_id, exc)

    @staticmethod
    async def _archive(state: LegalState, listener: Any = None) -> None:
        """跑归档节点（MemoryNode）。``listener`` 非空时把它的 Trace 事件推给 SSE。

        ⚠ **必须显式把监听器传进来**（2026-09-21 修）。归档刻意另起一个 Harness 跑
        （原因见 ``harness.flush_trace`` 的注释），而新 Harness 的 Tracer 没有任何订阅者。
        更隐蔽的是：主 Harness 的订阅在 ``execute`` 的 ``finally`` 里**已经摘掉了** ——
        所以"让新 Harness 继承旧 Tracer 的订阅者"也拿不到人（这里踩过一次）。

        症状：MemoryNode 照常执行、``steps`` 也加到了 10，但它那条 ``step`` 事件
        谁都收不到 —— 界面出现自相矛盾的一屏：页脚写「10 步」，
        展开的轨迹列表只有 9 个节点（缺 MemoryNode）。归档是交付链路的一环
        （案件档案/经验落库），它跑没跑必须看得见。
        """
        from backend.agents.nodes import NODE_FUNCS

        fn = NODE_FUNCS.get(ARCHIVE_NODE)
        if fn is None:
            return
        try:
            wrap = Harness.start(state, client="worker", username="archive")
            if listener is not None:
                try:
                    wrap.tracer.subscribe(listener)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("归档 Trace 订阅失败（不影响归档本身）：%s", exc)
            try:
                await fn(state, wrap)
            finally:
                wrap.finish()
                # 归档跑在 ``graph.run`` 的快照**之后**，又是**另一个 Harness**，
                # 而且是**直接调节点函数**（没走 ``graph._execute``）——
                # 所以 ``wrap.executed`` **恒为空**（``Harness.step()`` 只写 route 与
                # ``steps``，往 ``executed`` 里追加的是图的 ``_execute``）——
                # 它这一笔落不进 ``used_nodes``；可 ``harness.step()`` 照常把
                # MemoryNode 追进 ``state["route"]``。不补这一行，界面就出现
                # "route 里有 MemoryNode、used_nodes 里没有"的自相矛盾，
                # 与 2026-09-21 修掉的那个（页脚 10 步 / 轨迹 9 个节点）同源同因：
                # 归档是交付链路的一环，它跑没跑必须两边都看得见。
                #
                # 判据取 route 而不是"调用过 fn"：``step()`` 在节点**真正开跑**时才
                # 把它记进 route，是"确实进过这个节点"的可靠信号；而 fn 抛异常时
                # ``finally`` 照样会走到这里。
                if ARCHIVE_NODE in (state.get("route") or []):
                    state["used_nodes"] = list(dict.fromkeys(  # type: ignore[typeddict-unknown-key]
                        list(state.get("used_nodes") or []) + [ARCHIVE_NODE]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("归档未完成（不影响交付物）：%s", exc)

    # ------------------------------------------------------------------ 只读
    @staticmethod
    def progress_of(state: dict[str, Any]) -> int:
        """从 State 推算进度（供 SSE 事件与轮询共用）。

        返回的是 **0~99 的整数百分比**，不是 ``{"percent": n}`` 这种对象 ——
        注解原先是 ``dict[str, Any]``，前端照着它写了 ``progress.percent``，
        于是进度条一次都没亮过（静默：取到 undefined，不报错）。
        """
        return task_service._progress(state)

    @staticmethod
    def route_label(intent: str) -> str:
        return (INTENT_ROUTES.get(intent) or {}).get("label", intent)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _kind_of(req: RunRequest) -> str:
    if req.material_paths:
        return "material"
    if req.material_text:
        return "contract" if (req.doc_type or "") == "contract" else "text"
    return "chat"


orchestrator = Orchestrator()


def run_sync_state(query: str, **kw: Any) -> LegalState:
    """同步跑一次流程（脚本/测试用）。"""
    req = RunRequest(query=query, **{k: v for k, v in kw.items()
                                     if k in RunRequest.__dataclass_fields__})
    return asyncio.run(orchestrator.execute(req, persist=False)).state


__all__ = [
    "ARCHIVE_NODE",
    "RESULT_FIELDS",
    "Orchestrator",
    "RunOutcome",
    "RunRequest",
    "orchestrator",
    "run_sync_state",
]
