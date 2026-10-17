"""解析层：DOM 提取 + 正文正则兜底。

**两段式**，而不是二选一：

1. DOM 提取（``dom_selectors`` 命中）—— 结构化、准；但站点改版就会静默取空；
2. 正文正则兜底 —— 只要正文拿到手，案号/法院/日期总能捞出来；不依赖任何选择器。

如果只做 DOM：改版当天所有字段一起变空，还看不出来（页面照样 200）。
如果只做正则：正文里出现第二个案号（引用他案）时会取错。
所以 DOM 优先、正则补位，并在 ``fields`` 里**逐字段记录来源**，
一条数据的某个值到底是"抓到的"还是"猜的"可回溯 —— 这是法律场景的底线。
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

from backend.crawler.models import CrawlTarget, ParsedDoc
from backend.crawler.storage import build_doc_id, content_hash, normalize_body
# 复用解析层既有的正则，避免同一件事写两套、两套慢慢分叉
from backend.parser.structure import DATE_RE as _DATE_RE

# --------------------------------------------------------------------------- 正则
# 完整案号：（2023）京0105民初123号 / （2023）最高法民终123号。
# 比 structure.DOC_NO_RE 更严：要求带年份括号，避免把正文里"第123号"这类
# 泛指（如证据编号）误当案号。
CASE_NO_STRICT_RE = re.compile(
    r"[（(]\s*(?P<y>(?:19|20)\d{2})\s*[）)]\s*"
    r"(?P<body>[\u4e00-\u9fa5]{0,10}\d{1,6}[\u4e00-\u9fa5]{0,8}\d{0,10})\s*号"
)
# 法院 / 仲裁机构。取最常见的后缀，宁可窄一点也不要匹配到句子。
COURT_RE = re.compile(
    r"(?P<court>[\u4e00-\u9fa5]{2,20}(?:人民法院|人民检察院|仲裁委员会))"
)
# 中文数字日期：二〇二三年五月一日 / 二零二三年五月一日。
# structure.DATE_RE 只认阿拉伯数字，而裁判文书正文里中文纪年极常见 ——
# 缺了它，日期兜底在最该生效的地方恰好失效。
_CN_NUM = {"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
           "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
CN_DATE_RE = re.compile(
    r"(?P<y>[〇零一二三四五六七八九]{4})\s*年\s*"
    r"(?P<m>[〇零一二三四五六七八九十]{1,3})\s*月\s*"
    r"(?P<d>[〇零一二三四五六七八九十]{1,3})\s*日"
)
# 中文数字（十/二十/三十一 等）
_CN_UNITS = {"十": 10}


def _cn_to_int(s: str) -> int:
    """把中文数字串转成 int。支持 一/十/十一/二十/三十一 与纯数字串。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if "十" not in s:
        digits = "".join(_CN_NUM.get(c, "") for c in s)
        return int(digits) if digits else 0
    head, _, tail = s.partition("十")
    tens = _cn_to_int(head) if head else 1
    ones = _cn_to_int(tail) if tail else 0
    return tens * 10 + ones


def _cn_year(s: str) -> int:
    return int("".join(_CN_NUM.get(c, "0") for c in s) or "0")


# --------------------------------------------------------------------------- HTML → 文本
class _TextExtractor(HTMLParser):
    """极简正文抽取：丢掉 script/style，块级标签换算行。

    用标准库而不是引入 lxml/bs4：这里只需要"把标签剥掉"，多一个解析依赖不划算。
    真正的结构化抽取交给访问层的 DOM 选择器，本类只服务于**正则兜底**这条路径。
    """

    _SKIP = {"script", "style", "noscript", "template"}
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5",
              "section", "article", "table", "td", "th", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = _html.unescape(raw)
        return normalize_body(re.sub(r"[ \t\u3000]*\n[ \t\u3000]*", "\n", raw))


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不该让整条链路失败
        pass
    return p.text()


def html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(?P<t>.*?)</title>", html or "", re.I | re.S)
    if not m:
        return ""
    return _html.unescape(re.sub(r"\s+", " ", m.group("t"))).strip()


# --------------------------------------------------------------------------- 字段抽取
def _normalize_date(y: str, m: str, d: str) -> str:
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (TypeError, ValueError):
        return ""


def extract_case_no(text: str) -> str:
    m = CASE_NO_STRICT_RE.search(text or "")
    if m:
        head = f"（{m.group('y')}）"
        return f"{head}{m.group('body')}号"
    return ""


def extract_court(text: str) -> str:
    """取**首个**法院名。裁判文书首页的法院名即作出机关，后面的多是他案引用。"""
    m = COURT_RE.search(text or "")
    return m.group("court") if m else ""


def extract_date(text: str) -> str:
    """取裁判/发布日期：**全文位置最靠后的那个日期**。

    落款日期在文末，故取最后一个；首个往往是当事人出生日期或合同签订日，
    用它当裁判日期是错的，而且错得很隐蔽（格式完全合法）。

    阿拉伯与中文两种纪年必须**合到一起比位置**再取最后：先看阿拉伯、
    有就返回的话，"1985年3月1日出生 …… 二〇二三年五月一日" 这种
    最典型的中文判决书会取到出生日期。
    """
    t = text or ""
    best_pos, best_iso = -1, ""
    for m in _DATE_RE.finditer(t):
        iso = _normalize_date(m.group("y"), m.group("m"), m.group("d"))
        if iso and m.start() >= best_pos:
            best_pos, best_iso = m.start(), iso
    for m in CN_DATE_RE.finditer(t):
        iso = _normalize_date(str(_cn_year(m.group("y"))),
                              str(_cn_to_int(m.group("m"))),
                              str(_cn_to_int(m.group("d"))))
        if iso and m.start() >= best_pos:
            best_pos, best_iso = m.start(), iso
    return best_iso


_DOC_TYPE_HINTS = (
    ("民事判决书", "judgment"), ("行政判决书", "judgment"), ("刑事判决书", "judgment"),
    ("判决书", "judgment"),
    ("民事裁定书", "ruling"), ("行政裁定书", "ruling"), ("裁定书", "ruling"),
    ("调解书", "mediation"), ("决定书", "decision"), ("通知书", "notice"),
)


def guess_doc_type(text: str, default: str = "judgment") -> str:
    for hint, kind in _DOC_TYPE_HINTS:
        if hint in (text or ""):
            return kind
    return default


def _pick(dom: dict[str, str], key: str) -> tuple[str, str]:
    """从 DOM 字段里取一个值，返回 ``(值, 来源)``。"""
    v = (dom or {}).get(key)
    if v and str(v).strip():
        return str(v).strip(), "dom"
    return "", ""


def parse_document(
    *,
    source: str,
    url: str = "",
    html: str = "",
    text: str = "",
    dom: dict[str, str] | None = None,
    title: str = "",
    target: CrawlTarget | None = None,
) -> ParsedDoc:
    """把一次取页结果解析成 ``ParsedDoc``。

    正文来源优先级：DOM 选择器正文 → 已抽好的 ``text`` → 由 ``html`` 剥标签。
    """
    dom = dict(dom or {})
    body_dom, _ = _pick(dom, "body")
    body = normalize_body(body_dom or text or html_to_text(html))
    if not body:
        body = ""

    fields: dict[str, str] = {}

    case_no, src = _pick(dom, "case_no")
    if not case_no:
        case_no = extract_case_no(body)
        src = "regex" if case_no else ""
    fields["case_no"] = src

    court, src = _pick(dom, "court")
    if not court:
        court = extract_court(body)
        src = "regex" if court else ""
    fields["court"] = src

    judge_date, src = _pick(dom, "judge_date")
    if not judge_date:
        judge_date = extract_date(body)
        src = "regex" if judge_date else ""
    fields["judge_date"] = src

    title_final = (title or dom.get("title") or "").strip()
    title_src = "dom" if title_final else ""
    if not title_final:
        title_final = html_title(html)
        title_src = "regex" if title_final else ""
    if not title_final:
        # 退而求其次：正文首行
        first = (body.split("\n", 1)[0] if body else "").strip()
        if 2 <= len(first) <= 120:
            title_final, title_src = first, "body"
    fields["title"] = title_src

    doc_type = guess_doc_type(body, (target.extra.get("doc_type") if target else "") or "judgment")

    doc_id = build_doc_id(source, case_no=case_no, url=url)

    return ParsedDoc(
        doc_id=doc_id,
        source=source,
        url=url,
        title=title_final,
        doc_type=doc_type,
        case_no=case_no,
        court=court,
        judge_date=judge_date,
        body=body,
        content_hash=content_hash(body) if body else "",
        fields=fields,
        extra={
            "doc_id_field": "case_no" if case_no else "url",
            "body_len": len(body),
        },
    )
