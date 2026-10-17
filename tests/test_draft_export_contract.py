"""交付合同（修订稿草稿）的导出**内容**。

为什么要有这个文件：`_write_docx` / `_write_pdf` **至今没有任何测试碰过内容** ——
唯一相关的 `test_draft_docx_export_never_silently_falls_back` 只断言了状态码和扩展名。
于是"落款签名区被整块吃掉""页码印在正文里""标题不居中"这种**废纸级**缺陷，
一路跑到了用户手里（2026-09-29）。

所以这里不写"看起来对不对"的软断言：**把 docx 解包读 XML、把 PDF 用 pymupdf 抽文字**，
断言的就是律师打开文件会看到的东西。

对照的原件：`scripts/.cdp_out/_pdfgeom.txt`（A4、标题居中、页脚页码 y≈794）。
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import draft_service, user_service

API = "/api/v1"
PASSWORD = "Test-Pa55word!"

# 一份**最小但形状正确**的交付合同正文：后端 `_contract_markdown` 的产出长这样。
CONTRACT = "\n".join([
    "# 劳 动 合 同 书",
    "根据《中华人民共和国劳动法》及有关法律法规，双方协商一致，同意签订本合同。",
    "### 一、劳动合同期限",
    "第一条 本合同为固定期限劳动合同，合同期自 2024年3月1日 起至 2027年2月28日 止。",
    "### 九、其他规定",
    '第二十五条 本合同盖章签字生效。',
    '<p align="left">甲方：上海具识智能科技有限公司</p>',
    '<p align="right">2024 年 2 月 28 日</p>',
])

# 同样的正文但**不带版式标记**：用来钉住"非合同文书一个字不变"。
PLAIN = "\n".join([
    "这是一份普通文书。",
    "第二段照旧首行缩进。",
])


def _uniq(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def lawyer(client: TestClient) -> dict[str, Any]:
    """建一个律师并登录取 token。

    ⚠️ **草稿必须挂在这个用户名下**：`user_id` 为空的草稿被当成"服务端生成的"，
    只有管理员能导出（`docs._assert_owner`），拿 None 建就是 403。
    """
    team = user_service.create_team(_uniq("合同导出组"))
    u = user_service.create_user(_uniq("ctexporter"), PASSWORD, role="lawyer",
                                 full_name="合同导出测试", team_id=team["id"])
    r = client.post(f"{API}/auth/login",
                    json={"username": u["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"token": r.json()["access_token"], "id": u["id"]}


@pytest.fixture
def made():
    ids: list[str] = []
    yield ids
    for did in ids:
        draft_service.delete_draft(did)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _export(client: TestClient, lawyer: dict[str, Any], made: list[str],
            content: str, fmt: str, doc_type: str) -> bytes:
    """建草稿 → 导出 → 下载，返回文件字节。"""
    d = draft_service.create_draft(title="劳动合同书（改后版）", doc_type=doc_type,
                                   content=content, user_id=lawyer["id"])
    did = d["draft_id"]
    made.append(did)
    r = client.post(f"{API}/drafts/{did}/export?fmt={fmt}", json={},
                    headers=_auth(lawyer["token"]))
    assert r.status_code == 200, r.text
    r = client.get(f"{API}/drafts/{did}/download", headers=_auth(lawyer["token"]))
    assert r.status_code == 200, r.text
    assert len(r.content) > 0, "下载到 0 字节文件"
    return r.content


def _docx_xml(blob: bytes, name: str = "word/document.xml") -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(name).decode("utf-8")


def _docx_text(blob: bytes) -> str:
    doc = Document(io.BytesIO(blob))
    return "\n".join(p.text for p in doc.paragraphs)


# ---------------------------------------------------------------- docx

def test_docx_contract_is_a4_and_has_a_real_page_footer(client, lawyer, made):
    """A4 + **真页脚**的 PAGE 域。

    页码必须在页脚里自动编（用户 2026-09-29「页数是页脚，不是里面的文字」）。
    查 `word/footer*.xml` 里的 `w:fldSimple` 与 `w:instr="PAGE"` ——
    只断言"文档里有数字"是查不出这个缺陷的：正文里本来就有「第二十五条」这种数字。
    """
    blob = _export(client, lawyer, made, CONTRACT, "docx", "revision")

    xml = _docx_xml(blob)

    # A4（210×297mm）在 OOXML 里的单位是 twips：210mm = 11906，297mm = 16838。
    # 走默认（Letter，12240×15840）就是错的 —— 同一份草稿导出 PDF 是 A4，
    # 用户拿到的两份尺寸不一样。
    m = re.search(r'<w:pgSz[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"', xml)
    assert m, "document.xml 里没有 <w:pgSz>，页面尺寸没设上"
    w, h = int(m.group(1)), int(m.group(2))
    assert abs(w - 11906) <= 20 and abs(h - 16838) <= 20, f"不是 A4：{w}×{h} twips"

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        footers = [n for n in z.namelist() if re.match(r"word/footer\d*\.xml$", n)]
        assert footers, "文档里没有页脚部件 —— 页码没地方放"
        fx = "".join(z.read(n).decode("utf-8") for n in footers)
    assert 'w:instr="PAGE"' in fx, "页脚里没有 PAGE 域，页码不会自动编"
    assert "fldSimple" in fx, "页脚里的页码不是域，是写死的文字"


def test_docx_contract_keeps_the_signature_block_and_aligns_it(client, lawyer, made):
    """落款区一字不少，且**靠左那几行没有首行缩进**。

    缩进是这里最容易错的地方：`<p align="left">` 从前匹配不上导出层的正则，
    会**原样印出 `<p align="left">甲方：…</p>` 这串标记**；
    而如果让它落进"普通段落"分支，又会多出两字首行缩进 ——
    预览里、和原件里，落款都是顶格的。
    """
    blob = _export(client, lawyer, made, CONTRACT, "docx", "revision")

    text = _docx_text(blob)
    for must in ("甲方：上海具识智能科技有限公司", "2024 年 2 月 28 日"):
        assert must in text, f"落款丢了：{must!r} 不在导出正文里"
    assert '<p align="left">' not in text, "版式标记被当成正文印出来了"
    assert '<p align="right">' not in text, "版式标记被当成正文印出来了"

    doc = Document(io.BytesIO(blob))
    left = [p for p in doc.paragraphs if p.text.startswith("甲方：")]
    assert left, "找不到落款的甲方行"
    for p in left:
        assert p.paragraph_format.first_line_indent in (None, 0), (
            f"落款被加了首行缩进 {p.paragraph_format.first_line_indent} —— 应当顶格")

    # 正文条款**要**缩进（这是"结构版式"的口径），别把两件事一起改没了
    body = [p for p in doc.paragraphs if p.text.startswith("第一条")]
    assert body, "找不到正文第一条款"
    assert body[0].paragraph_format.first_line_indent is not None
    assert body[0].paragraph_format.first_line_indent.pt == pytest.approx(24, abs=0.5)


def test_docx_contract_body_is_justified(client, lawyer, made):
    """正文两端对齐（原件就是齐的）。居中标题和靠右落款不能被顺手改成两端对齐。"""
    blob = _export(client, lawyer, made, CONTRACT, "docx", "revision")
    xml = _docx_xml(blob)
    assert 'w:jc w:val="both"' in xml, "正文没有两端对齐（w:jc=both）"
    assert 'w:jc w:val="center"' in xml, "大标题没有居中"
    assert 'w:jc w:val="right"' in xml, "落款日期没有靠右"


def test_docx_non_contract_draft_is_unchanged(client, lawyer, made):
    """**反面**：非修订稿文书不许被套上合同的版式。

    这条是防"顺手改坏所有文书的导出"：`_write_docx` 是全站共用的。
    """
    blob = _export(client, lawyer, made, PLAIN, "docx", "contract")
    xml = _docx_xml(blob)
    assert 'w:instr="PAGE"' not in xml, "非合同文书也被加了页脚页码"
    m = re.search(r'<w:pgSz[^>]*w:w="(\d+)"', xml)
    assert m and abs(int(m.group(1)) - 11906) > 20, "非合同文书被改成了 A4"


# ---------------------------------------------------------------- pdf

def test_pdf_contract_has_page_number_in_the_footer_only(client, lawyer, made):
    """PDF：页码在**页脚**，正文里不许再出现页标或孤立页码。

    ⚠️ 用坐标判"是不是页脚"：页码必须在页面底部（y > 页高的 90%），
    而不是随便在正文里找到个 "1" 就算过。
    """
    pymupdf = pytest.importorskip("fitz")
    blob = _export(client, lawyer, made, CONTRACT, "pdf", "revision")

    doc = pymupdf.open(stream=blob, filetype="pdf")
    assert doc.page_count >= 1
    page = doc[0]
    height = page.rect.height
    assert abs(height - 842) < 3, f"不是 A4：页高 {height}"

    # 正文里不许有页标（后端 `_contract_markdown` 就该清掉）
    text = "\n".join(p.get_text() for p in doc)
    assert "<!--" not in text, "正文里还有页标 `<!-- 第 N 页 -->`"
    assert "第 1 页" not in text, "正文里还有『第 N 页』"

    # 页码：底部区域、居中的那一行
    bottom = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                if sp["bbox"][1] > height * 0.9:
                    bottom.append(sp)
    assert bottom, "页面底部什么都没有 —— 页脚没有页码"
    nums = "".join(sp["text"] for sp in bottom).strip()
    assert nums.isdigit(), f"页脚那行不是页码：{nums!r}"
    # 居中：文字块中心与页宽中心相差不超过 5pt
    cx = sum((sp["bbox"][0] + sp["bbox"][2]) / 2 for sp in bottom) / len(bottom)
    assert abs(cx - page.rect.width / 2) < 5, f"页脚页码没居中（中心 x={cx:.1f}）"


def test_pdf_contract_keeps_the_signature_block(client, lawyer, made):
    """PDF 侧同样要保住落款 —— 少一个签署页的合同是废纸。"""
    pymupdf = pytest.importorskip("fitz")
    blob = _export(client, lawyer, made, CONTRACT, "pdf", "revision")
    doc = pymupdf.open(stream=blob, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    assert "甲方：上海具识智能科技有限公司" in text.replace(" ", "").replace("\n", "") \
        or "甲方：上海具识智能科技有限公司" in text
    assert "2024" in text and "2" in text and "28" in text, "落款日期丢了"
    assert '<p align="left">' not in text, "版式标记被印进 PDF 正文了"


def test_pdf_non_contract_draft_has_no_footer_number(client, lawyer, made):
    """反面：非合同 PDF 不加页脚页码（其余文书版式一个字不变）。"""
    pymupdf = pytest.importorskip("fitz")
    blob = _export(client, lawyer, made, PLAIN, "pdf", "contract")
    doc = pymupdf.open(stream=blob, filetype="pdf")
    page = doc[0]
    height = page.rect.height
    bottom = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                if sp["bbox"][1] > height * 0.9:
                    bottom.append(sp["text"])
    assert not "".join(bottom).strip().isdigit(), "非合同 PDF 被加了页脚页码"
