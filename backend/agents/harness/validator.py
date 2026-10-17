"""Harness Validator：Citation 引用终审（**法律场景最关键的组件**）。

对应方案 4.1「Citation Validator：终审：条文号/版本/效力、案号存在性逐条核对，
校验不过直接拦截，不进入输出」与 8.3「反幻觉三条红线」。

三条红线的落地方式：

1. **无来源不出引用** —— 报告/草稿里出现的每一处《法名》第X条、每一个案号，
   都必须能回溯到本次任务的检索证据池。回溯不到 = 幻觉，**拦截**。
   这一条完全在我们可控范围内（证据池是我们自己攒的），所以判"必拦"。
2. **时效性校验** —— 法条效力状态必填；命中「已废止」**拦截**；
   「已修订」放行但强提示核对最新文本；「未知」不拦截但要求人工核实。
3. **定性边界** —— 不输出鉴定类结论、强制免责声明，由
   :func:`~backend.agents.harness.state_manager.finalize` 与报告模板保证。

关于"校验工具本身不可用"的情况：法规库/类案库不可用时不代表引用是假的，
所以**不拦截**，而是标记 ``verified=False`` 并登记证据缺口。否则一次
Milvus 抖动就会让整个产品无法出结论 —— 那是把可用性事故伪装成合规。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.agents.harness.context import RunContext
from backend.agents.harness.executor import ToolInvoker
from backend.agents.harness.state_manager import add_evidence_gap
from backend.agents.state import LegalState
from backend.app.config import settings

logger = logging.getLogger("legal_agent.harness.validator")

# 法规引用：《中华人民共和国劳动合同法》第八十七条
STATUTE_CITE_RE = re.compile(
    r"《\s*([^》]{2,40}?)\s*》\s*(?:第\s*([一二三四五六七八九十百零两\d]+)\s*条)?"
)
# 不带书名号的法规引用：律师写意见书时最常见的写法（「依据劳动合同法第八十七条」）。
# 只认带书名号的那种，红线一就只对"文书体裁"生效 ——
# 而咨询与案件分析的成交物（strategy）里，引用恰恰全是不带书名号的写法，
# 结果是**这一整类任务的引用从来没被核验过**，报告却写着"未执行引用终审"。
BARE_STATUTE_RE = re.compile(
    r"((?:中华人民共和国|最高人民法院)?[\u4e00-\u9fa5]{2,20}?)"
    r"\s*第\s*([一二三四五六七八九十百零两\d]+)\s*条"
)
# 案号：（2023）京01民终12345号
CASE_NO_RE = re.compile(r"[（(]\s*(19|20)\d{2}\s*[)）]\s*[^\s，。；、）)]{1,24}?号")

# 行文回指词开头的"法规名"不是引用，是回指（「上述规定第3条」「本法第5条」）。
# 不去掉它们会把正常行文判成幻觉引用，而**误拦比漏拦更快摧毁信任**：
# 律师只要看到一条被误判的引用，整份拦截清单在他眼里就作废了。
_VAGUE_PREFIX = ("上述", "前述", "该", "本", "此", "相关", "有关", "以上", "如下",
                 "其他", "其它", "涉案", "双方", "当事人", "约定")

# 裸写法里名字**不能含**的字：含了就说明这个"名字"其实是句子片段。
# 例如「《劳动合同法》第八十七条与劳动合同法第87条」，
# 正则会从「八」起头一路吃到「法」，得到「第八十七条与劳动合同法」这个
# 看起来像法规的怪物，然后因为对不上证据池而**误拦一条正确引用**。
_NAME_FORBIDDEN = ("第", "条", "款", "项", "的", "与", "和", "及", "或", "、", "，", "。")

# 引用前面常见的介词/连词：法规名真正的起点在这些词之后。
# 不做这一步，「根据劳动合同法第八十七条」会得到「根据劳动合同法」这个键，
# 而证据池里的键是「劳动合同法|第87条」—— 正确的引用被终审拦下。
# 法规名不会以这些词开头，所以剥离是无损的。
_CITE_LEAD_WORDS = ("根据", "依据", "依照", "按照", "遵照", "参照", "基于", "对于",
                    "关于", "适用", "违反", "符合", "结合", "以及", "并且",
                    "依", "据", "按", "以", "和", "与", "及", "或", "对")

# 命中即拦截的效力状态
BLOCKING_STATUS = ("已废止",)
# 命中则降权并强提示
WARN_STATUS = ("已修订", "尚未生效", "未知")


class ValidationResult:
    def __init__(self) -> None:
        self.citations: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.warnings: list[str] = []
        # ``checked`` / ``verified`` 只统计**正文里真正出现过的引用**。
        # 它们的语义是"这份交付物引用了 N 条、其中 M 条可信"，
        # 报告摘要行（"引用 3/5 通过"）与管理统计都按这个口径读，不能改。
        self.checked = 0
        self.verified = 0
        self.blocking = 0
        # ``pool_statutes`` 是**证据池**的核验条数，与上面两个分开。
        #
        # 为什么必须单独有一份：终审原先只在"正文里有引用表述"时才累加计数，
        # 而咨询/快速问答这类路的结论正文（strategy / report）在终审那一刻
        # 往往还不存在，于是工具调了、结果拿到了、计数却是 0 ——
        # 终审看起来跑了，实际什么都没核。这一份计数是"终审真的核过东西"
        # 的唯一凭据，缓存准入也依赖它（见 ``inspected``）。
        self.pool_statutes = 0

    @property
    def passed(self) -> bool:
        return self.blocking == 0

    @property
    def inspected(self) -> int:
        """本次终审实际核过多少条（正文引用 + 证据池）。

        用作"终审是否真的做了事"的判据：为 0 说明只是走了一遍流程。
        """
        return self.checked + self.pool_statutes

    def to_summary(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for c in self.citations:
            by_type[c.get("type", "")] = by_type.get(c.get("type", ""), 0) + 1
        return {
            "passed": self.passed,
            "checked": self.checked,
            "verified": self.verified,
            "blocking": self.blocking,
            "pool_statutes": self.pool_statutes,
            "inspected": self.inspected,
            "rejected": self.rejected,
            "warnings": self.warnings,
            "by_type": by_type,
            "min_confidence": settings.CITATION_MIN_CONFIDENCE,
        }


class CitationValidator:
    """逐条核对引用。"""

    def __init__(self, ctx: RunContext, state: LegalState, invoker: ToolInvoker) -> None:
        self.ctx = ctx
        self.state = state
        self.invoker = invoker

    # ------------------------------------------------------------------ 入口
    async def validate(self) -> ValidationResult:
        result = ValidationResult()

        pool = self._evidence_pool()
        mentions = self._collect_mentions()

        # ---- 第一轮：来源回溯（无来源不出引用）
        for m in mentions:
            if m["kind"] == "statute":
                key = self._statute_key(m["name"], m["article"])
                if key not in pool["statutes"]:
                    # 只给法规名、条号对不上：可能是同法规其他条文，给出更精确的提示
                    if any(k.startswith(f"{_plain(m['name'])}|") for k in pool["statutes"]):
                        self._reject(
                            result,
                            f"《{m['name']}》{m['article'] or '（未标条文号）'}："
                            f"该条文号不在本次检索证据中，不得引用",
                        )
                    else:
                        self._reject(
                            result,
                            f"《{m['name']}》：未在本次检索证据中找到该法规，疑似无来源引用",
                        )
                    continue
                result.citations.append(self._build_statute_citation(m, pool))

            elif m["kind"] == "case":
                if m["case_no"] and m["case_no"] not in pool["case_nos"]:
                    self._reject(
                        result,
                        f"案号 {m['case_no']}：未在本次检索的类案证据中，疑似编造案号",
                    )
                    continue
                result.citations.append(self._build_case_citation(m, pool))
            else:
                # knowledge 类型引用回溯本次 knowledges 证据池：标题不在池中
                # = 无来源引用，**必拦**（与 statute / case 同口径，红线一）。
                # 证据池键是 ``_plain(title)``（见 :meth:`_evidence_pool`），
                # 所以比较键也要过同一套规范化，否则全称/简称差异会误拦。
                key = _plain(m["name"])
                if key not in pool["knowledges"]:
                    self._reject(
                        result,
                        f"{m['name']}：未在本次检索的本所指引证据中找到该标题，疑似无来源引用",
                    )
                    continue
                result.citations.append({"type": "knowledge", "name": m["name"],
                                         "verified": False, "reason": "知识库片段引用"})

        # ---- 第二轮：池内法条逐条时效校验（含未被正文引用的，用于尽到核对义务）
        pool_statutes = self._pool_statutes()
        if pool_statutes:
            await self._verify_statutes(pool_statutes, result)

        # ---- 第三轮：案号真实性校验
        pool_cases = self._pool_cases()
        if pool_cases:
            await self._verify_cases(pool_cases, result)

        self._finalize(result)
        return result

    # ------------------------------------------------------------------ 证据池
    def _evidence_pool(self) -> dict[str, set[str]]:
        """本次任务检索到的证据键集合（引用回溯的基准）。"""
        statutes: set[str] = set()
        for s in self.state.get("statutes") or []:
            name = _plain(s.get("doc_name") or "")
            article = _norm_article(s.get("article_no") or "")
            if name:
                statutes.add(f"{name}|{article}")
                statutes.add(f"{name}|")   # 只提法规名不提条号也算有来源

        cases: set[str] = set()
        case_nos: set[str] = set()
        for c in self.state.get("cases") or []:
            if c.get("case_name"):
                cases.add(_plain(c["case_name"]))
            if c.get("case_no"):
                case_nos.add(_norm_case_no(c["case_no"]))
                cases.add(_norm_case_no(c["case_no"]))

        knowledges: set[str] = set()
        for k in self.state.get("knowledges") or []:
            if k.get("title"):
                knowledges.add(_plain(k["title"]))

        return {"statutes": statutes, "cases": cases,
                "case_nos": case_nos, "knowledges": knowledges}

    def _statute_key(self, name: str, article: str) -> str:
        """引用的比较键：``法规名|条号``。

        必须与 :meth:`_evidence_pool` 的建键方式逐字一致，**并且**法规名与条号
        都要过同一套规范化（``_plain`` / ``_norm_article``）——
        以前这里没有这个方法（一接线就抛 AttributeError），而规范化又是各处各写，
        两边任一处不一致就会把正确引用判成"无来源"。
        """
        return f"{_plain(name)}|{_norm_article(article)}"

    @staticmethod
    def _reject(result: ValidationResult, message: str) -> None:
        """登记一条**必拦**引用。

        存在的意义是保证不变式 ``passed == (len(rejected) == 0)``。
        此前这里只有 ``result.rejected.append(...)``，而 ``blocking`` 只在
        "已废止"那条路径上加过 —— 于是第一轮"无来源不出引用"（红线一，方案里
        写明"必拦"）拦下来的引用只进了 rejected 列表，``blocking`` 仍是 0，
        ``passed`` 依然是 True、``citation_check`` 依然写 "passed"。
        也就是说终审**看起来在拦、实际从不拦截**，这是最危险的一类缺陷：
        日志里有拦截记录，下游却按"通过"处理。

        拦截必须与登记同时发生，所以收敛成一个入口，不再直接 append。
        """
        result.rejected.append(message)
        result.blocking += 1

    def _pool_statutes(self) -> list[dict[str, Any]]:
        return list(self.state.get("statutes") or [])

    def _pool_cases(self) -> list[dict[str, Any]]:
        return list(self.state.get("cases") or [])

    # ------------------------------------------------------------------ 引用扫描
    def _collect_mentions(self) -> list[dict[str, Any]]:
        """抽取所有"对律师输出了法律依据"的引用表述。

        覆盖范围必须全，否则反幻觉红线一就只是"文书类任务的红线"：
        草稿 / 审查意见有 draft 与 risk_points 兜着，而**咨询与案件分析的交付物在
        ``strategy``（攻防要点）与 ``issues[].basis``（争议焦点依据）里**。
        原先这两处都不扫，于是咨询类任务的引用一条都没被核验过，
        报告里还写着"本次未执行引用终审" —— 律师拿到的是一份
        引用了《劳动法》第21条、却没人核过这条能不能用的意见。
        """
        # ---- 1. 结构化依据字段：一条字符串就是一个引用，解析精度最高
        mentions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for s in self._structured_basis():
            self._add_mention(mentions, seen, self._parse_basis_string(s))

        # ---- 2. 自由文本：草稿、报告、策略、风险点描述
        blob = "\n".join(self._free_texts())
        if blob.strip():
            for m in STATUTE_CITE_RE.finditer(blob):
                # 与证据池用同一套规范化：否则全称/简称写法差异会让正确引用被拦
                name = _plain(m.group(1))
                article = _norm_article(m.group(2) or "")
                # 中文书名号在正文里也用于非法规文档（如《XX协议》），过滤掉明显不是法规的
                if not _looks_like_statute(name):
                    continue
                self._add_mention(mentions, seen,
                                  {"kind": "statute", "name": name, "article": article,
                                   "raw": m.group(0)})

            for m in BARE_STATUTE_RE.finditer(blob):
                name = _plain(_strip_lead_words(m.group(1)))
                if not self._looks_like_bare_statute(name):
                    continue
                self._add_mention(mentions, seen,
                                  {"kind": "statute", "name": name,
                                   "article": _norm_article(m.group(2) or ""),
                                   "raw": m.group(0)})

            for m in CASE_NO_RE.finditer(blob):
                no = _norm_case_no(m.group(0))
                self._add_mention(mentions, seen,
                                  {"kind": "case", "name": "", "case_no": no,
                                   "raw": m.group(0)})

            # 只说案例名不带案号的情况：用证据池里的案例名做包含匹配
            pool_case_names = {_plain(c.get("case_name") or "") for c in self._pool_cases()}
            for name in [n for n in pool_case_names if n and len(n) >= 6]:
                if name in blob:
                    self._add_mention(mentions, seen,
                                      {"kind": "case", "name": name,
                                       "case_no": "", "raw": name})

        return mentions

    def _free_texts(self) -> list[str]:
        """所有会输出给律师的自由文本字段。"""
        texts: list[str] = []
        for field in ("draft", "revision_draft", "report"):
            if self.state.get(field):
                texts.append(str(self.state[field]))
        for rp in self.state.get("risk_points") or []:
            texts.append(str(rp.get("risk") or ""))
            texts.append(str(rp.get("suggestion") or ""))
        # 咨询/案件分析的结论就在这里
        texts.extend(str(s) for s in (self.state.get("strategy") or []))
        for it in self.state.get("issues") or []:
            texts.append(str(it.get("title") or ""))
            texts.append(str(it.get("description") or ""))
        lc = self.state.get("limitation_check") or {}
        texts.append(str(lc.get("conclusion") or ""))
        texts.append(str(lc.get("period") or ""))
        return texts

    def _structured_basis(self) -> list[str]:
        """结构化依据字段：里面的每一项本身就是一条引用，直接解析即可。

        比在自由文本上跑正则可靠得多 —— 这里不会有"上述规定"这类回指，
        也不会把句子里的普通名词误当成法规名。
        """
        out: list[str] = []
        for rp in self.state.get("risk_points") or []:
            out.extend(_as_text_list(rp.get("basis")))
            out.extend(_as_text_list(rp.get("citations")))
        for it in self.state.get("issues") or []:
            out.extend(_as_text_list(it.get("basis")))
        lc = self.state.get("limitation_check") or {}
        out.extend(_as_text_list(lc.get("basis")))
        return [s for s in out if s.strip()]

    def _parse_basis_string(self, s: str) -> dict[str, Any] | None:
        """把「中华人民共和国劳动法第21条」这类依据字符串解析成引用表述。"""
        text = (s or "").strip()
        if not text:
            return None
        m = STATUTE_CITE_RE.search(text)
        if m:
            name = _plain(m.group(1))
            if _looks_like_statute(name):
                return {"kind": "statute", "name": name,
                        "article": _norm_article(m.group(2) or ""), "raw": text}
        m = BARE_STATUTE_RE.search(text)
        if m:
            name = _plain(_strip_lead_words(m.group(1)))
            if self._looks_like_bare_statute(name):
                return {"kind": "statute", "name": name,
                        "article": _norm_article(m.group(2) or ""), "raw": text}
        m = CASE_NO_RE.search(text)
        if m:
            return {"kind": "case", "name": "", "case_no": _norm_case_no(m.group(0)),
                    "raw": text}
        return None

    def _looks_like_bare_statute(self, name: str) -> bool:
        """不带书名号的名字要更严：必须像真法规名，且不是行文回指。

        比 ``_looks_like_statute`` 多两道闸，因为这里没有书名号做信号：

        1. 名字里不能含「第/条/与/和/的」这类字 —— 含了就说明它是句子片段，
           不是法规名（正则从句子中间起头时最容易出现这种情况）；
        2. 法规体裁后缀必须出现在名字**末尾**（「劳动合同法」可以，
           「劳动合同法律顾问意见」不行）。
        """
        if not name or len(name) < 2:
            return False
        if any(c in name for c in _NAME_FORBIDDEN):
            return False
        if not name.endswith(_STATUTE_SUFFIX):
            return False
        return _looks_like_statute(name)

    @staticmethod
    def _add_mention(mentions: list[dict[str, Any]], seen: set[str],
                     m: dict[str, Any] | None) -> None:
        if not m:
            return
        key = (f"st|{m['name']}|{m.get('article', '')}" if m["kind"] == "statute"
               else f"cs|{m.get('case_no') or m.get('name') or ''}")
        if key in seen:
            return
        seen.add(key)
        mentions.append(m)

    # ------------------------------------------------------------------ 构建引用
    def _build_statute_citation(self, m: dict[str, Any],
                                pool: dict[str, set[str]]) -> dict[str, Any]:
        hit = next((s for s in self._pool_statutes()
                    if _plain(s.get("doc_name") or "") == m["name"]
                    and (not m["article"] or _norm_article(s.get("article_no") or "") == m["article"])),
                   None)
        return {
            "type": "statute",
            "name": f"《{m['name']}》",
            "article": m["article"],
            "status": (hit or {}).get("status") or "未知",
            "version": (hit or {}).get("version") or "",
            "source_path": (hit or {}).get("source_path") or "",
            "verified": False,
            "reason": "已在检索证据中找到",
            "raw": m["raw"],
        }

    def _build_case_citation(self, m: dict[str, Any],
                             pool: dict[str, set[str]]) -> dict[str, Any]:
        hit = None
        if m.get("case_no"):
            hit = next((c for c in self._pool_cases()
                        if _norm_case_no(c.get("case_no") or "") == m["case_no"]), None)
        if hit is None and m.get("name"):
            hit = next((c for c in self._pool_cases()
                        if _plain(c.get("case_name") or "") == _plain(m["name"])), None)
        hit = hit or {}
        return {
            "type": "case",
            "name": hit.get("case_name") or m.get("name") or "",
            "case_no": hit.get("case_no") or m.get("case_no") or "",
            "court": hit.get("court") or "",
            "verified": False,
            "reason": "已在检索证据中找到",
            "raw": m["raw"],
        }

    # ------------------------------------------------------------------ 校验
    async def _verify_statutes(self, pool_statutes: list[dict[str, Any]],
                               result: ValidationResult) -> None:
        """效力状态校验：已废止拦截；其余按级别提示。

        **核验范围是整个证据池，不只是正文引用到的那些。**

        工具调用本来就是拿整池去查的（下面 ``items``），但结果原先只套在
        ``result.citations`` 上 —— 没有正文表述时那是空列表，于是查询结果
        被整批丢弃、``checked`` 恒为 0，终审在 log 里"跑过了"、实际一条没核。
        咨询与快速问答这类路的结论正文在终审那一刻还没有，正好全部落在这一档。

        现在池内每一条的效力状态都被记进 ``result.pool_statutes``，
        池里出现**已废止**也照样拦截（哪怕正文没引它）——
        报告正文正是从这些检索结果拼出来的，放一条废法进去就是
        "过时的法律依据比没有依据更危险"（state.py 的原话）。
        """
        if not pool_statutes:
            return
        items = [
            {"doc_name": s.get("doc_name") or "", "article_no": s.get("article_no") or "",
             "status": s.get("status") or ""}
            for s in pool_statutes
        ]
        res = await self.invoker.call("statute_validity_check", "ValidatorNode",
                                      items=items)
        status_by: dict[str, str] = {}
        # 工具按入参顺序逐条返回（statute_tools.statute_validity_check 里是
        # ``for it in items: results.append(...)``），所以下标是可靠对应；
        # 名称键只作兜底 —— 库里存的法规名可能带《》，与检索结果对不上。
        status_by_index: list[str] = []
        if res.ok and isinstance(res.data, dict):
            for r in res.data.get("results") or []:
                key = f"{_plain(r.get('doc_name') or '')}|{_norm_article(r.get('article_no') or '')}"
                status_by[key] = r.get("status") or "未知"
                status_by_index.append(r.get("status") or "未知")

        def _status_of(item: dict[str, Any], idx: int) -> str:
            if idx < len(status_by_index):
                return status_by_index[idx]
            key = f"{_plain(item.get('doc_name') or '')}|{_norm_article(item.get('article_no') or '')}"
            return status_by.get(key) or item.get("status") or "未知"

        result.pool_statutes += len(pool_statutes)

        # ---- 池内每一条：已废止一律拦截（这是本节点对外的核心承诺）
        # 正文引用到的那些留给下面那一轮报（措辞更准，且不会重复计数）：
        # 第一轮已经保证"能活到这里的引用必定在池内"，所以两边必然重叠。
        cited_keys = {
            f"{_plain(c.get('name') or '')}|{_norm_article(c.get('article') or '')}"
            for c in result.citations if c.get("type") == "statute"
        }
        for idx, s in enumerate(pool_statutes):
            status = _status_of(s, idx)
            if status not in BLOCKING_STATUS:
                continue
            key = f"{_plain(s.get('doc_name') or '')}|{_norm_article(s.get('article_no') or '')}"
            if key in cited_keys:
                continue
            label = f"《{_plain(s.get('doc_name') or '')}》{s.get('article_no') or ''}"
            self._reject(result, f"{label}：已废止，本次检索证据中含失效依据，禁止引用")

        # ---- 正文引用到的：逐条写回状态与可引用标记（口径不变）
        for c in result.citations:
            if c.get("type") != "statute":
                continue
            result.checked += 1
            key = f"{_plain(c.get('name') or '')}|{_norm_article(c.get('article') or '')}"
            status = status_by.get(key) or c.get("status") or "未知"
            c["status"] = status

            if status in BLOCKING_STATUS:
                c["verified"] = False
                c["reason"] = "该法规已废止，不得作为裁判依据"
                self._reject(result, f"{c['name']}{c['article']}：已废止，禁止引用")
            elif status in WARN_STATUS:
                c["verified"] = True
                c["reason"] = {
                    "已修订": "已修订，引用前须核对最新文本",
                    "尚未生效": "尚未生效，注意适用时间范围",
                    "未知": "效力状态未标注，需人工核实",
                }.get(status, "")
                result.warnings.append(f"{c['name']}{c['article']}：{c['reason']}")
                result.verified += 1
            else:
                c["verified"] = True
                c["reason"] = "现行有效"
                result.verified += 1

    async def _verify_cases(self, pool_cases: list[dict[str, Any]],
                            result: ValidationResult) -> None:
        """案号 / 案例名真实性校验。

        拦截策略说明（重要，别改成"一律拦截"）：
        走到这里的案号**已经通过第一轮证据回溯**（必须在本次检索到的类案证据里），
        所以它们必然存在于类案库中。第二轮的作用是**独立复核证据本身可信**，
        而不是判定引用真假。

        因此 ``exists=False`` 只作为**告警 + 证据缺口**处理，不拦截 ——
        BM25 精确 token 命中存在漏召回可能，若在此拦截，会出现
        「检索到了这个判例，又因为这个判例拦截了自己」的假阳性，
        把真问题（编造案号）淹没在噪声里。真正编造的案号在第一轮就已经被拦掉了。
        """
        for c in result.citations:
            if c.get("type") != "case":
                continue
            result.checked += 1
            label = c.get("case_no") or c.get("name") or "（未命名判例）"
            res = await self.invoker.call("case_no_verify", "ValidatorNode",
                                          case_no=c.get("case_no") or "",
                                          case_name=c.get("name") or "")
            if not res.ok:
                # 校验工具不可用：不拦截，标记未核验并登记缺口
                c["verified"] = False
                c["reason"] = f"案号未能核验（{res.error or '校验工具不可用'}）"
                result.warnings.append(f"{label}：未能核验，不得作为正式引用")
                add_evidence_gap(self.state, "case", c["reason"],
                                 "相关判例仅作参考，不得在文书中作为正式引用")
                continue

            data = res.data if isinstance(res.data, dict) else {}
            if data.get("exists"):
                c["verified"] = True
                c["reason"] = data.get("how") or "已核验存在"
                result.verified += 1
            else:
                c["verified"] = False
                reason = data.get("reason") or "案号在类案库中不存在"
                c["reason"] = reason
                result.warnings.append(f"{label}：{reason}")
                add_evidence_gap(self.state, "case",
                                 f"{label} 未通过存在性复核",
                                 "该判例引用须经律师人工核验后方可写入文书")

    # ------------------------------------------------------------------ 收尾
    def _finalize(self, result: ValidationResult) -> None:
        self.state["citations"] = result.citations
        self.state["citation_check"] = "passed" if result.passed else "failed"
        summary = result.to_summary()
        self.state["citation_summary"] = summary

        if not result.passed:
            self.ctx.tracer.error(
                "ValidatorNode", "citation_failed",
                f"拦截 {result.blocking} 条不可信引用：{'；'.join(result.rejected[:5])}",
            )
            add_evidence_gap(
                self.state, "citation",
                f"引用终审拦截 {result.blocking} 条不可信引用",
                "被拦截的引用已从输出中剔除，相关结论需以可核验依据重述",
            )
            self.ctx.note(
                f"引用终审拦截 {result.blocking} 条不可信引用，"
                f"相关表述需以可核验的法条/案号重述"
            )
        else:
            self.ctx.tracer.info(
                "ValidatorNode", "citation_passed",
                f"核对正文引用 {result.checked} 条（通过 {result.verified} 条）"
                f"，证据池法条 {result.pool_statutes} 条"
                + (f"，提示 {len(result.warnings)} 条" if result.warnings else ""),
            )

        if result.warnings:
            for w in result.warnings[:10]:
                self.ctx.note(f"引用提示：{w}")

        self.ctx.audit("citation.verify", resource="citation",
                       result="ok" if result.passed else "denied",
                       detail=f"checked={result.checked} blocking={result.blocking} "
                              f"rejected={result.rejected[:5]}")

        logger.info(
            "引用终审：checked=%d verified=%d pool_statutes=%d blocking=%d passed=%s",
            result.checked, result.verified, result.pool_statutes,
            result.blocking, result.passed,
        )


# --------------------------------------------------------------------------- 工具
def _plain(name: str) -> str:
    """法规名规范化：剥书名号与空白，并去掉前缀「中华人民共和国」。

    必须与 ``nodes.review_node._plain_law`` 用**同一套规则**，否则会出现
    "ReviewNode 判定可溯源、Validator 判定不可溯源"的分裂结论 ——
    而 Validator 才是真正拦截的那一环，结果就是正确引用被拦。

    去前缀是无损的（不携带区分信息），所以合并安全。**不做子串模糊匹配**：
    「合同法」是「劳动合同法」的子串，而《合同法》已于 2021 年废止并入《民法典》，
    放行它等于让失效法律冒充有效法律通过终审。
    """
    cleaned = (name or "").strip().strip("《》").strip()
    for prefix in ("中华人民共和国", "最高人民法院"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _norm_article(value: str) -> str:
    """条文号规范化：统一为阿拉伯数字的「第X条」。

    委托给 ``parser.structure.normalize_article_no`` —— 这里曾有一版只去空格、
    不转数字的实现，导致「第八十七条」（库内写法）与「第87条」（模型常见写法）
    被判定为不同引用，正确的引用会被终审拦下。比较键必须只有一处实现。
    """
    from backend.parser.structure import normalize_article_no

    return normalize_article_no(value) or (value or "").strip()


def _norm_case_no(value: str) -> str:
    return re.sub(r"[\s（）()]", "", value or "")


# 正文里借书名号出现但显然不是法规的写法（文档体裁）
_NON_STATUTE_HINT = ("协议", "合同", "章程", "报告", "意见书", "起诉状", "答辩状",
                     "方案", "计划", "纪要", "备忘录", "承诺函", "通知书", "清单")

# 法规体裁后缀：以这些字结尾的名字，即使含文档化词汇也**必须**按法规校验
_STATUTE_SUFFIX = ("法", "条例", "规定", "办法", "细则", "解释", "规则",
                   "通知", "批复", "决定", "命令", "标准", "通则", "纲要")


def _as_text_list(value: Any) -> list[str]:
    """把 basis / citations 这类字段统一成字符串列表。

    它们有三种历史写法：``["《民法典》第585条"]``、``[{"name": "...", "article": "..."}]``、
    以及夹着 None 的混合列表。校验器要是只认第一种，
    结构化依据里的引用就会被静默跳过 —— 而"静默跳过"在引用核验里等于没核。
    """
    out: list[str] = []
    for v in value or []:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            name = str(v.get("name") or v.get("doc_name") or "")
            article = str(v.get("article") or v.get("article_no") or "")
            no = str(v.get("case_no") or "")
            out.append(" ".join(x for x in (no, name, article) if x))
        elif v is not None:
            out.append(str(v))
    return [x for x in out if x.strip()]


def _strip_lead_words(name: str) -> str:
    """剥掉法规名前面粘上的介词/连词（「根据劳动合同法」→「劳动合同法」）。

    裸写法正则是从文字里"扫"出引用的，起点由"哪个位置开始能凑出 `XX法第N条`"决定，
    于是句首的介词会被一起卷进名字里。不剥掉的话，键就变成
    「根据劳动合同法|第87条」，而证据池里的键是「劳动合同法|第87条」——
    一条完全正确的引用会被终审拦下。这正是"误拦比漏拦更伤"的典型。
    """
    out = (name or "").strip()
    for _ in range(4):   # 最多剥 4 层（「根据中华人民共和国…」这类叠加）
        hit = next((w for w in sorted(_CITE_LEAD_WORDS, key=len, reverse=True)
                    if out.startswith(w) and len(out) > len(w)), "")
        if not hit:
            break
        out = out[len(hit):]
    return out


def _looks_like_statute(name: str) -> bool:
    """判断书名号里的名字像不像法规。

    报告里会出现《房屋租赁合同》这种书名号，如果不加区分就会被当成法规引用去校验，
    结果是"引用不通过"刷屏，把真正的问题埋掉。

    **但"宁可漏检也不误报"这个取向在这里是错的** —— 它会把《合同法》也一起漏掉：
    「合同法」含「合同」这个文档提示词，于是被过滤；而《合同法》已于 2021-01-01
    废止并入《民法典》，正是一条**最该被拦下来**的引用。漏检失效法律比刷屏严重得多。

    正确判据是看**结尾的体裁词**：以「法/条例/规定/办法/解释…」结尾的一律按法规处理
    （《合同法》《劳动合同法》都在内），只有以「合同/协议/清单/报告…」结尾的才排除
    （《房屋租赁合同》《买卖合同（模板）》）。

    另外两道闸是补上去的，都有具体起因：

    - **回指词**：《该法》《本法》《上述规定》都以体裁词结尾，光看结尾一律放行，
      于是正常的行文回指会被当成"无来源引用"拦下；
    - **最低长度放到 2**：原先卡在 3，把《刑法》《宪法》这两部引用频率极高的法律
      整个漏掉了（规范化会剥掉「中华人民共和国」前缀，「中华人民共和国刑法」→「刑法」）。
      而「该法」这类 2 字回指已被上一道闸挡住，所以放宽长度是安全的。
    """
    if not name or len(name) < 2:
        return False
    if name.startswith(_VAGUE_PREFIX):
        return False
    if name.endswith(_STATUTE_SUFFIX):
        return True
    if any(h in name for h in _NON_STATUTE_HINT):
        return False
    return any(k in name for k in _STATUTE_SUFFIX)

