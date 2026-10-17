"""文本抽取：PyMuPDF(PDF) / python-docx(Word) / 纯文本。

关键设计：**占位符机制**。
PDF 中的表格、图片、印章、签字页不能被当作普通文本流吞掉，否则会破坏
「原始文本位置与文档结构对齐」，后续条款定位（第X条在第几页）就无从谈起。

因此抽取时在正文对应位置插入 ``[TABLE_001]`` / ``[IMAGE_001]`` 占位符，
由 table_parser / vision 单独处理后回插语义描述。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("legal_agent.parser")

SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".jpg", ".jpeg",
                  ".png", ".bmp", ".webp"}

# 单独成文件的图片（不是文档里嵌的那张图）。**律师从微信发证据最常见的就是这个**
# —— 拍一张照就发过来。此前它们会被 ``extract`` 直接判成"不支持的文件类型"，
# 于是照片永远进不了证据链，而界面上只会说"没有可读的正文"。
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# PDF 页内可能出现的无用页眉页脚噪声
PAGE_NOISE = re.compile(r"^\s*(第\s*\d+\s*页\s*(共\s*\d+\s*页)?|-\s*\d+\s*-|\d+\s*/\s*\d+)\s*$")


@dataclass
class ExtractedBlock:
    """一个占位块（表格 / 图片）。"""

    placeholder: str          # [TABLE_001]
    kind: str                 # table / image
    page: int
    bbox: list[float] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)   # table
    image_path: str = ""                                   # image
    semantic: str = ""                                     # 回插的语义描述
    parsed: dict | None = None


@dataclass
class ExtractionResult:
    text: str
    pages: int
    blocks: list[ExtractedBlock]
    source_path: str
    doc_format: str
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- 工具
def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text(text: str) -> str:
    """清理 PDF 抽取常见的排版噪声，但不动正文用词。"""
    if not text:
        return ""
    lines = []
    for raw in text.splitlines():
        line = raw.replace("\u3000", " ").rstrip()
        if PAGE_NOISE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- PDF
def extract_pdf(path: str | Path, *, max_pages: int | None = None, image_dir: str | None = None) -> ExtractionResult:
    import pymupdf  # PyMuPDF，1.24+ 的模块名

    path = Path(path)
    blocks: list[ExtractionBlock] = []
    parts: list[str] = []
    table_idx = 0
    image_idx = 0
    warnings: list[str] = []

    doc = pymupdf.open(str(path))
    try:
        pages = len(doc)
        limit = min(pages, max_pages) if max_pages else pages
        for pno in range(limit):
            page = doc[pno]
            parts.append(f"\n<!-- 第 {pno + 1} 页 -->\n")

            # ---- 表格：优先用 PyMuPDF 的表格识别，识别到就用占位符替换
            try:
                finder = page.find_tables()
                page_tables = list(finder.tables)
            except Exception as exc:  # noqa: BLE001
                page_tables = []
                warnings.append(f"第{pno + 1}页表格识别失败：{exc}")

            table_rects = []
            for tbl in page_tables:
                table_idx += 1
                ph = f"[TABLE_{table_idx:03d}]"
                data = []
                try:
                    data = [[(c or "").strip() for c in row] for row in tbl.extract()]
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"第{pno + 1}页表格解析失败：{exc}")
                blocks.append(
                    ExtractedBlock(
                        placeholder=ph,
                        kind="table",
                        page=pno + 1,
                        bbox=list(tbl.bbox),
                        rows=data,
                    )
                )
                table_rects.append(tbl.bbox)
                parts.append(f"\n{ph}\n")

            # ---- 图片（含印章/签字/插图）
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    rects = page.get_image_rects(xref)
                except Exception:  # noqa: BLE001
                    rects = []
                if rects and any(_inside(r, table_rects) for r in rects):
                    continue  # 表格内部的图不重复占位
                image_idx += 1
                ph = f"[IMAGE_{image_idx:03d}]"
                saved = ""
                if image_dir:
                    try:
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.n - pix.alpha >= 4:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        out_dir = Path(image_dir)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        saved = str(out_dir / f"p{pno + 1:04d}_{ph.strip('[]')}.png")
                        pix.save(saved)
                        pix = None
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"第{pno + 1}页图片导出失败：{exc}")
                        saved = ""
                blocks.append(
                    ExtractedBlock(
                        placeholder=ph,
                        kind="image",
                        page=pno + 1,
                        bbox=list(rects[0]) if rects else [],
                        image_path=saved,
                    )
                )
                parts.append(f"\n{ph}\n")

            parts.append(page.get_text("text") or "")
    finally:
        doc.close()

    return ExtractionResult(
        text=clean_text("\n".join(parts)),
        pages=pages,
        blocks=blocks,
        source_path=str(path),
        doc_format="pdf",
        sha256=file_sha256(path),
        warnings=warnings,
    )


def _inside(rect, others: list) -> bool:
    try:
        x0, y0, x1, y1 = rect
        for o in others:
            if x0 >= o[0] - 1 and y0 >= o[1] - 1 and x1 <= o[2] + 1 and y1 <= o[3] + 1:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------------- Word
def extract_docx(path: str | Path) -> ExtractionResult:
    """Word 文档抽取：保留段落顺序，正文内嵌图片打占位符，表格单独结构化。"""
    import docx
    from docx.document import Document as _Doc
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    path = Path(path)
    document: _Doc = docx.Document(str(path))
    blocks: list[ExtractedBlock] = []
    parts: list[str] = []
    t_idx = 0
    i_idx = 0

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, document)
            text = (para.text or "").strip()
            has_img = bool(child.findall(".//" + qn("a:blip")))
            if has_img:
                i_idx += 1
                ph = f"[IMAGE_{i_idx:03d}]"
                blocks.append(ExtractedBlock(placeholder=ph, kind="image", page=0))
                parts.append(ph)
            if text:
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            t_idx += 1
            ph = f"[TABLE_{t_idx:03d}]"
            tbl = Table(child, document)
            rows = []
            for row in tbl.rows:
                rows.append([(c.text or "").strip() for c in row.cells])
            blocks.append(ExtractedBlock(placeholder=ph, kind="table", page=0, rows=rows))
            parts.append(ph)

    return ExtractionResult(
        text=clean_text("\n".join(parts)),
        pages=0,
        blocks=blocks,
        source_path=str(path),
        doc_format="docx",
        sha256=file_sha256(path),
    )


def extract_doc(path: str | Path) -> ExtractionResult:
    """旧版 .doc：python-docx 不支持，尝试 OLE 文本抽取；失败则明确报错。"""
    path = Path(path)
    warnings = ["旧版 .doc 格式解析能力有限，建议转换为 .docx 后再导入以保证结构对齐"]
    text = ""
    try:
        data = path.read_bytes()
        # 粗抽：从 WordDocument 流中提取可见字符
        chunks = re.findall(rb"[\x20-\x7e\u4e00-\u9fa5]{4,}", data)
        text = ""
        for c in chunks[:20000]:
            try:
                text += c.decode("utf-8")
            except UnicodeDecodeError:
                continue
    except Exception as exc:  # noqa: BLE001
        warnings.append(f".doc 抽取失败：{exc}")
    return ExtractionResult(
        text=clean_text(text),
        pages=0,
        blocks=[],
        source_path=str(path),
        doc_format="doc",
        sha256=file_sha256(path),
        warnings=warnings,
    )


def extract_text_file(path: str | Path) -> ExtractionResult:
    path = Path(path)
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    return ExtractionResult(
        text=clean_text(text),
        pages=0,
        blocks=[],
        source_path=str(path),
        doc_format=path.suffix.lstrip("."),
        sha256=file_sha256(path),
    )


def extract_image_file(path: str | Path) -> ExtractionResult:
    """把**一张单独的图片**当成"一页扫描件"来抽取。

    它自己不识别内容：产出一个 ``kind="image"`` 的块，正文只放占位符
    ``[IMAGE_001]``，**把识别交给管线的第 4 步**（多模态）—— PDF 里嵌的图走的就是
    那条路，这里不另开一条。三种结果都比"整体拒绝"诚实：

      · 视觉可用 → 识别结果回插进正文，照片里的字就进了证据链；
      · 视觉不可用 → 正文只剩占位符（``case_service._material_text`` 会把它
        当成"没有正文"，不会拿标记冒充内容），并留下 ``vlm`` 缺口说明原因；
      · 识别失败 → 同上，且 ``evidence_gaps`` 里写明"识别异常"。

    为什么不在这里直接调 VLM：这一层是**同步、无模型依赖**的抽取层
    （``asyncio.to_thread`` 里跑的），而多模态调用是异步的、要探活、要缓存。
    把它拽进来会让"抽取"变成"抽取 + 联网"，而那条路已经有人走了。
    """
    path = Path(path)
    block = ExtractedBlock(
        placeholder="[IMAGE_001]",
        kind="image",
        page=1,
        # 指向**原图本身**：视觉客户端按后缀判 mime（.jpg/.jpeg/.png…），
        # 原图正是它要的那份字节，不必像 PDF 那样先落盘成 PNG。
        image_path=str(path),
    )
    return ExtractionResult(
        # 正文里必须留占位符：``splice_placeholders`` 是"按占位符替换"来回插语义的，
        # 正文里没有它，识别结果就**无处可放**（那是"识别成功却什么都看不到"的坑）。
        text="[IMAGE_001]",
        pages=1,
        blocks=[block],
        source_path=str(path),
        doc_format=path.suffix.lstrip("."),
        sha256=file_sha256(path),
    )


def extract(path: str | Path, *, image_dir: str | None = None, max_pages: int | None = None) -> ExtractionResult:
    """按扩展名分派抽取器。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path, max_pages=max_pages, image_dir=image_dir)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".doc":
        return extract_doc(path)
    if ext in (".txt", ".md"):
        return extract_text_file(path)
    if ext in IMAGE_EXTS:
        return extract_image_file(path)
    raise ValueError(f"不支持的文件类型：{ext}（支持 {sorted(SUPPORTED_EXTS)}）")


# --------------------------------------------------------------------------- 回插
def splice_placeholders(text: str, blocks: list[ExtractedBlock]) -> str:
    """把表格/图片的语义描述回插到占位符位置。

    处理后：
        [IMAGE_001: 营业执照扫描件，有限责任公司，法定代表人×××……]
        [TABLE_001: 租赁物、面积、用途等要素]
    保证原始文本位置与文档结构对齐。
    """
    out = text
    for b in blocks:
        if not b.semantic:
            continue
        out = out.replace(b.placeholder, f"[{b.placeholder.strip('[]')}: {b.semantic}]")
    return out
