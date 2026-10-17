"""业务领域自动识别：让模型判断法规属于哪个领域，并据此把文件归进同名目录。

**分类结果不用 json 记录 —— 记录就是"文件在哪个目录"**：

- 法规一旦落进 ``data/raw/<领域>/``，它就算"已分类"，之后**永不重新判定**。
  稳定性由目录本身保证：同一部法规不会这次进"劳动法"、下次进"劳动关系"。
- 特意**不做**"标题 → 领域"的 json 映射表：那是一份**和实际文件可能不一致的平行记录**。
  文件被手工移走 / 删掉之后，缓存还留着旧答案；模型换了、领域名改了，它也一样不变。
  最后的症状是"缓存说劳动法、文件在劳动关系"，而两边各自看都"没错"，极难排查。
  目录是唯一的事实来源 —— 看目录就知道分类结果。

三条刻意的设计：

1. **已归位的文件不再问模型**（:meth:`TopicClassifier.lookup` 先认目录记录）。既省调用，
   又天然幂等：重跑不会把文件搬到别的目录去。
2. **模型不可用时一个字都不写**。LLM 客户端在探测不到模型时会**静默降级**到规则引擎
   （见 ``backend/llm/client.py``），那种输出不是领域名。把它当目录名建出来，
   比不分类更糟 —— 所以检查 ``degraded``，降级就整批放弃、保持原目录。
3. **领域名要净化后才能当目录名**。模型输出会带书名号、斜杠、超长描述，
   直接拿来做目录名会造出非法路径或一层层怪目录。

领域判定优先级（与 flk 落盘一致）：显式/按检索词选定的目录 → **目录记录 / 本次模型判定**
→ 关键词规则表（兜底）→ ``CRAWL_FLK_DIR``。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.app.config import PROJECT_ROOT, settings

logger = logging.getLogger("legal_agent.crawler.topics")

# 参与归档的文档类型。与 flk 落盘的三种格式一致。
DOC_SUFFIXES = (".pdf", ".docx", ".doc")

# 文件名非法字符 + 路径分隔符：一律从领域名里剔除。
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
# 模型可能用来表示"我判断不了"的词，这些不能当目录名。
_REJECT = {"", "无", "未知", "未分类", "其他", "其它", "无法判断", "不确定", "综合"}
_MAX_TOPIC_LEN = 20        # 领域名上限，超过说明它在写描述而不是名字

# 文件名归一：**全项目只此一份实现**。flk 的落盘命名、采集检索词、这里的目录记录
# 必须用同一套规则，否则"同一部法规"会被当成两个不同的词，表现为"明明采过却又采一遍"
# 或"目录里明明有却说没分类"。
_EXT_RE = re.compile(r"\.(pdf|docx|doc)$", re.I)
# 尾部日期：`_20220331` / `-2022-03-31` / `_202203`。文件名常带它，比对时必须去掉。
_DATE_RE = re.compile(r"[_\-]?\s*\d{4}[-_]?\d{0,2}[-_]?\d{0,2}$")


def clean_doc_name(name: str) -> str:
    """把 raw/ 下的目录名或文件名归一成一个检索词 / 领域键。

    去扩展名 → 去尾部日期 → 去书名号与首尾空白、下划线。例：
    ``中华人民共和国知识产权海关保护条例_20180319.docx`` → ``中华人民共和国知识产权海关保护条例``。
    """
    n = _EXT_RE.sub("", (name or "").strip())
    n = _DATE_RE.sub("", n)
    n = n.strip(" _-")
    n = re.sub(r"^[《〈]|[》〉]$", "", n)
    return n.strip()


def raw_root() -> Path:
    """``data/raw/``（相对路径按项目根锚定）。"""
    root = Path(settings.RAW_DIR).expanduser()

    return root if root.is_absolute() else PROJECT_ROOT / root




def _abs(p: str | Path) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() else PROJECT_ROOT / q


def sanitize_topic(name: str) -> str:
    """把模型给的领域名净化成安全的目录名；不可用则返回 ``""``。

    去掉书名号/引号、剔除非法字符、限长、拒绝"未知/其他"这类占位词 ——
    它要直接当目录名用，任何漏网字符都会造出非法路径。
    """
    t = (name or "").strip()
    t = t.strip("《》〈〉【】[]\"'“”‘’")
    t = _BAD_CHARS.sub("", t)
    t = t.strip(" ._-、，,")
    if len(t) > _MAX_TOPIC_LEN:
        return ""
    if t in _REJECT or t.isdigit():
        return ""
    return t


def _reserved_dir_names() -> set[str]:
    """``data/raw/`` 下**不是领域目录**的顶层名字。

    采集输出目录（``CRAWL_FLK_DIR``，法规先落这里再归位）与文书模板目录要被排除：
    它们是"原料"和"模板"，不是"分类结果"，当成领域会让采集自我循环。
    """
    reserved = {Path(settings.crawl_flk_dir).name, "文书模板"}
    tmpl = (settings.DOC_TEMPLATE_DIR or "").strip()
    if tmpl:
        reserved.add(Path(tmpl.replace("\\", "/")).name)
    return reserved


def topic_dirs() -> list[Path]:
    """``data/raw/`` 下的领域目录 —— 也就是"已分类"的那些目录。"""
    root = raw_root()
    if not root.is_dir():
        return []
    reserved = _reserved_dir_names()
    return [d for d in sorted(p for p in root.iterdir() if p.is_dir())
            if not d.name.startswith(".") and d.name not in reserved]


def known_topics(session_values: list[str] | None = None) -> list[str]:
    """已有领域名 = ``data/raw/`` 下的目录名 + 本次已判定出的领域。

    把已有领域喂给模型当**候选**，它才会复用而不是每部法都造一个近义新名字
    （"劳动法" / "劳动用工" / "劳动关系" 各建一个目录就彻底乱了）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        t = sanitize_topic(name)
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    for d in topic_dirs():
        add(d.name)
    for v in session_values or []:
        add(v)
    return out


_SYSTEM = (
    "你是中国法律法规归档助手。任务：为每个法规标题指定一个**业务领域**，"
    "用于归档到同名文件夹。只输出 JSON，不要任何解释。"
)

# --------------------------------------------------------------------------- 兜底规则表
def parse_topic_rules(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """解析规则表文本：``领域=关键词1,关键词2``，多条用换行或 ``;`` 分隔。

    解析失败的片段**跳过而不是报错**：一条规则写错不该让整次采集失败 ——
    最坏结果只是这个领域不被分类，法规仍会落到默认目录、照样入库。
    顺序即优先级（先匹配先赢），所以靠前的规则应当更具体。
    """
    out: list[tuple[str, tuple[str, ...]]] = []
    for raw in re.split(r"[;\n]", text or ""):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        topic, _, words = line.partition("=")
        topic = sanitize_topic(topic)
        kws = tuple(w.strip() for w in re.split(r"[,，]", words) if w.strip())
        if topic and kws:
            out.append((topic, kws))
    return out


def load_topic_rules() -> list[tuple[str, tuple[str, ...]]]:
    """规则表：**文件在前、内联在后**（先匹配先赢）。

    文件读不到只记一条 warning：它只是模型不可用时的兜底，不该让采集整体失败。
    """
    rules: list[tuple[str, tuple[str, ...]]] = []
    path = (settings.CRAWL_FLK_TOPIC_RULES_FILE or "").strip()
    if path:
        try:
            rules.extend(parse_topic_rules(_abs(path).read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("业务领域规则文件读取失败（已忽略）：%s —— %s", path, exc)
    rules.extend(parse_topic_rules(settings.CRAWL_FLK_TOPIC_RULES))
    return rules


def classify_title(title: str, rules: list[tuple[str, tuple[str, ...]]] | None = None) -> str:
    """按关键词规则表把标题归入领域；没命中返回 ``""``。"""
    t = (title or "").strip()
    if not t:
        return ""
    for topic, words in (rules if rules is not None else load_topic_rules()):
        if any(w in t for w in words):
            return topic
    return ""


def topic_for_title(
    title: str, rules: list[tuple[str, tuple[str, ...]]] | None = None,
) -> str:
    """标题 → 业务领域目录名（未识别返回 ``""``）。

    优先级：**目录记录 / 本次模型判定**（都走 ``classifier.lookup``）→ 关键词规则表（兜底）。

    "已归位"的法规不需要预热就能查到（它就在某个领域目录里）；而**新**法规的模型判定
    必须先由 :meth:`TopicClassifier.classify_batch` 预热 —— 落盘路径是同步的，
    不能在这里现场等一次网络调用。
    """
    t = (title or "").strip()
    if not t:
        return ""
    if settings.CRAWL_FLK_AUTO_TOPIC:
        topic = classifier.lookup(t)
        if topic:
            return topic
    return classify_title(t, rules)

_PROMPT = """请为下列中国法律法规标题指定业务领域。

规则：
1. 领域名用**简短中文名词**（2~8 字），例如：劳动法、知识产权法、刑事、财税、行政法、环境资源。
   不要带"领域""类""相关"等后缀，不要写整句描述。
2. **优先复用**下面「已有领域」里的名字；都不合适时**自行新起**一个简短领域名。
   **宁可新起名字，也不要留空** —— 每一条都必须能归档到某个文件夹。
3. **只有**《中华人民共和国民法典》这类横跨多个领域的综合性法典，值才给空字符串 ""。
   除此以外一律给领域名。
4. 输出 JSON 对象：{{"法规标题": "领域", ...}}。键必须是给定的标题原文，
   **必须包含全部标题，一个都不能少**。

已有领域：{known}

法规标题：
{titles}
"""


class TopicClassifier:
    """标题 → 领域的判定器。**记录在目录里，不在 json 里**。

    两类知识：

    - ``_placed``：**目录记录** —— 懒扫描 ``data/raw/<领域>/`` 下的文件名。
      这是唯一的持久事实来源，"已归位的法规永不重新判定"就靠它。
    - ``_session``：本次进程内模型刚判出来的结果。它只在内存里；真正生效是文件落进
      领域目录之后（那一刻它已经变成目录记录的一部分）。
    """

    def __init__(self) -> None:
        self._session: dict[str, str] = {}
        self._placed: dict[str, str] | None = None

    # ------------------------------------------------------------- 目录记录
    def _scan_placed(self) -> dict[str, str]:
        """扫 ``data/raw/<领域>/`` 下的文档 → ``{归一文件名: 领域目录名}``。

        同名文件在多个领域目录里都出现时**先见先赢**（不覆盖）：这说明"它算哪个领域"
        本身有歧义，此时沿用先出现的那个，不去制造新的搬动。
        """
        out: dict[str, str] = {}
        tmpl = (_abs(settings.DOC_TEMPLATE_DIR)
                if (settings.DOC_TEMPLATE_DIR or "").strip() else None)
        for d in topic_dirs():
            for f in sorted(d.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in DOC_SUFFIXES:
                    continue
                if tmpl is not None and tmpl in f.parents:
                    continue                       # 文书模板不是法规，不算领域记录
                key = clean_doc_name(f.name)
                if key:
                    out.setdefault(key, d.name)
        return out

    def placed(self) -> dict[str, str]:
        """目录记录（首次访问时扫描一次，之后复用）。"""
        if self._placed is None:
            self._placed = self._scan_placed()
        return self._placed

    def refresh(self) -> None:
        """丢弃并重建目录记录。

        文件被**外部**改动（手工移动 / 删除 / 另一个进程写的）后调用。进程内自己的
        落盘不需要它 —— :meth:`remember` 会同步更新记录。
        """
        self._placed = None

    def reset(self) -> None:
        """清空全部状态（单测用；正常流程不需要）。"""
        self._session.clear()
        self._placed = None

    # ------------------------------------------------------------- 查询 / 记录
    def lookup(self, title: str) -> str:
        """这部法规该归哪个领域；没归位过、本次也没判定过则返回 ``""``。

        **目录记录先于本次判定**：文件已经在 ``data/raw/劳动法/`` 里，答案就是"劳动法"，
        模型说过什么都不重要 —— 否则模型换个说法就会把文件搬到另一个目录去。
        """
        key = (title or "").strip()
        if not key:
            return ""
        t = sanitize_topic(self.placed().get(key, ""))
        if t:
            return t
        return sanitize_topic(self._session.get(key, ""))

    def remember(self, title: str, topic: str) -> None:
        """记下"标题 → 领域"（本次进程内）。

        不落盘：真正的持久记录是**文件落进那个目录**这件事本身。
        """
        t = sanitize_topic(topic)
        key = (title or "").strip()
        if not key or not t:
            return
        self._session[key] = t
        if self._placed is not None:
            # 刚判定的结果**不覆盖**已有目录记录：文件已经在别的领域目录里了，
            # 就以目录为准，免得同一部法被两个答案来回搬。
            self._placed.setdefault(key, t)

    def _known(self, titles: list[str]) -> dict[str, str]:
        """挑出其中**已经有答案**的标题，组成 ``{标题: 领域}``。"""
        out: dict[str, str] = {}
        for t in titles:
            key = (t or "").strip()
            if not key:
                continue
            topic = self.lookup(key)
            if topic:
                out[key] = topic
        return out

    # ------------------------------------------------------------- 判定
    async def classify_batch(self, titles: list[str]) -> dict[str, str]:
        """为一批标题判定领域，返回 ``{标题: 领域}``（未判定的不出现）。

        只对**既没归位、本次也还没判定过**的标题调用模型：已归位的文件永远不会再问
        模型，所以重跑这一步是幂等且免费的。模型降级或异常时整批放弃，由调用方回落
        到规则表或默认目录。
        """
        uniq = list(dict.fromkeys((x or "").strip() for x in titles))
        pending = [t for t in uniq if t and not self.lookup(t)]
        if not pending:
            return self._known(uniq)

        batch = max(1, int(settings.CRAWL_FLK_AUTO_TOPIC_BATCH or 20))
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            await self._classify_and_remember(chunk)
            # 模型常会**漏答**其中几个标题（一批给得多时尤其明显）。漏掉的补判一次：
            # 不补它们就会静默落到默认目录 —— 表现为"分类时好时坏"，很难查。
            missing = [t for t in chunk if not self._session.get(t)]
            if 0 < len(missing) < len(chunk):
                logger.info("模型漏答 %d 个标题，补判一次", len(missing))
                await self._classify_and_remember(missing, retry=True)
        return self._known(uniq)

    async def _classify_and_remember(self, chunk: list[str], *, retry: bool = False) -> None:
        try:
            got = await self._classify_chunk(chunk, retry=retry)
        except Exception as exc:  # noqa: BLE001 - 识别失败不该中断采集
            logger.warning("领域识别失败（这批按原目录落盘）：%s", exc)
            return
        for title, topic in got.items():
            self.remember(title, topic)

    async def _classify_chunk(self, chunk: list[str], *, retry: bool = False) -> dict[str, str]:
        from backend.llm.client import llm_client

        titles_block = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
        if retry:
            titles_block += "\n\n（注意：上一次这些标题你没有给出结果，请**每一条都要有值**，无法归类就给空字符串）"
        prompt = _PROMPT.format(
            known="、".join(known_topics(list(self._session.values()))) or "（暂无，可自行命名）",
            titles=titles_block,
        )
        data, result = await llm_client.chat_json(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": prompt}],
            task="topic_classify", default={},
        )
        if getattr(result, "degraded", False):
            # 降级到规则引擎时输出的**不是领域名**。当成目录名会污染整棵 raw/。
            logger.warning("模型不可用（已降级为规则引擎），本次不做领域识别")
            return {}

        out: dict[str, str] = {}
        # 兼容两种常见形态：{标题: 领域} 与 {"items": [{"title":..,"topic":..}]}
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            pairs = [(str((it or {}).get("title") or ""), str((it or {}).get("topic") or ""))
                     for it in items if isinstance(it, dict)]
        elif isinstance(data, dict):
            pairs = [(str(k), str(v)) for k, v in data.items()]
        else:
            pairs = []

        def _norm(s: str) -> str:
            """比对标题时忽略书名号与空白 —— 模型常自行加上《》。"""
            return re.sub(r"[\s《》〈〉]", "", s or "")

        norm_map = {_norm(t): t for t in chunk}
        for key, value in pairs:
            title = key.strip()
            if title not in chunk:
                title = norm_map.get(_norm(key), "")      # 去书名号/空白后再比对
            if not title:
                idx = key.strip().rstrip(".、) ").strip()  # 模型用序号当键时按位取回
                if idx.isdigit() and 1 <= int(idx) <= len(chunk):
                    title = chunk[int(idx) - 1]
            topic = sanitize_topic(value)
            if title and topic:
                out[title] = topic
        return out


classifier = TopicClassifier()
