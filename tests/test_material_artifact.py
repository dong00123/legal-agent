"""解析产物必须落盘，并把路径记进 ``case_material.parsed_path``（2026-10-12）。

这条链断过一次，而且断得**完全静默** —— 所有接口都返回 200，功能看起来一切正常，
只有一个后果：凡是非纯文本的材料（PDF / Word / 扫描件 / 图片）在「查看正文」里
永远显示"这份材料还没有可读的正文…请先点「解析」"，而点多少遍都一样。

根因：``parsed_path`` 是「查看材料正文」与证据链**唯一**的正文来源
（``case_service._material_text`` 只认它，读不到才退回原文，而原文只有
``.txt/.md`` 读得出文字）。但写入它的两处调用
（``POST /materials/{id}/parse`` 与微信上传）取的都是
``getattr(doc, "parsed_path", "")`` —— 而 ``ParsedDocument`` 当时**没有这个字段**，
``getattr`` 的默认值把这件事整个吞掉了，永远写空串。
全项目唯一会落盘的是 Agent 的 ``material_parse`` tool，而它只把路径写进自己的
返回 payload，没回填到 doc 上。

所以三个面各钉一条（少一个，换个入口就又断了）：
1. **管线**：``parse_document`` 返回时 ``doc.parsed_path`` 已有值、产物文件真在；
2. **服务**：``_material_text`` 从产物里读出正文，且 ``source == "parsed"``；
3. **接口**：走一遍 ``POST /materials/{id}/parse`` 之后，
   ``GET /materials/{id}/content`` 必须报 ``source == "parsed"`` ——
   这才是"字段真的写进库了"的证明，也是律师看到的那一层。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.services import case_service, user_service
from backend.parser import text_extractor as tx
from backend.parser.pipeline import parse_document

API = "/api/v1"
PASSWORD = "Test-Pa55word!"
ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web" / "app.js"

# 行注释：只剥「行首」或「前面是空白/;/{/}」的 ``//``，这样 URL 里的双斜杠不被误伤
# （与 ``tests/test_ui_static.py`` 同一套写法；这边只用到这一件事，没去 import 它 ——
# 测试模块之间互相 import 会跟着 pytest 的 rootdir 设置变，脆）。
_JS_LINE_COMMENT = re.compile(r"(^|[\s;{}])//[^\n]*", re.M)

# 一条真实的证据正文。用 .txt 而不是图片：解析器对纯文本必然出正文，
# 于是"从产物里读出来"这件事真的被走通，而不是靠"读不到也容错"蒙过去。
EVIDENCE_TEXT = "借条：今借到人民币壹拾万元整（¥100000），约定 2026 年 12 月 31 日前归还。"


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """把产物目录指到临时目录，不去污染项目真实的 ``data/processed/``。"""
    monkeypatch.setattr(settings, "PROCESSED_DIR", str(tmp_path / "processed"))
    return Path(settings.PROCESSED_DIR)


class _Mat:
    """只提供 ``_material_text`` 实际读取的那两个字段。"""

    def __init__(self, stored_path: str = "", parsed_path: str = "") -> None:
        self.stored_path = stored_path
        self.parsed_path = parsed_path


# =========================================================================== 管线
def test_parse_document_records_artifact_path(tmp_path, out_dir):
    """管线出口：返回时 ``parsed_path`` 必须已有值，且产物文件真的可读。

    这一条是整条链的地基。断言不能停在"函数返回了一个非空串" ——
    路径指向的文件必须存在：写入失败时函数也可能回一个路径（或干脆回空串），
    而两种情况下界面都是空的，只有文件在才说明产物真的可用。
    """
    src = tmp_path / "借条.txt"
    src.write_text(EVIDENCE_TEXT, encoding="utf-8")

    doc = asyncio.run(parse_document(src))

    assert doc.parsed_path, "解析完没有回填 parsed_path —— 界面与证据链都会读不到正文"
    product = Path(doc.parsed_path)
    assert product.is_file(), f"parsed_path 指向的文件不存在：{product}"
    blob = json.loads(product.read_text(encoding="utf-8"))
    assert EVIDENCE_TEXT[:12] in blob["text"], "产物里没有正文"
    # 产物要能自证"这段正文是从哪个文件来的"：排查"读错材料"时全靠它。
    assert blob["source_path"] == str(src)


def test_artifact_lands_under_processed_dir(tmp_path, out_dir):
    """产物落在 ``PROCESSED_DIR`` 下 —— 与 Agent 的 ``material_parse`` tool 同一处。

    两个写入点落到两个目录，就会出现"同一个材料两份产物"，而它们迟早分叉。
    """
    src = tmp_path / "证据.md"
    src.write_text(EVIDENCE_TEXT, encoding="utf-8")

    doc = asyncio.run(parse_document(src))

    assert Path(doc.parsed_path).parent == out_dir


# =========================================================================== 服务层
def test_material_text_prefers_parsed_product(tmp_path, out_dir):
    """有产物就读产物，``source`` 必须是 ``"parsed"``。

    退回原文在纯文本材料上看不出差别（两条路都读得出字），所以这里必须断言
    ``source`` —— 它是"到底取到没取到产物"的唯一区分信号。而 PDF / 图片的原文
    读不出文字，"取不到产物"就等于**一个字都看不到**。
    """
    src = tmp_path / "借条.txt"
    src.write_text(EVIDENCE_TEXT, encoding="utf-8")
    doc = asyncio.run(parse_document(src))

    text, source = case_service._material_text(
        _Mat(stored_path=str(src), parsed_path=doc.parsed_path), max_chars=1000,
    )

    assert source == "parsed", f"没有优先读解析产物，source={source!r}"
    assert EVIDENCE_TEXT[:12] in text


def test_material_text_falls_back_to_raw_text_file(tmp_path, out_dir):
    """没有产物时，纯文本原文仍要读得出来（旧材料的兜底路径，不能顺手砸掉）。"""
    src = tmp_path / "旧材料.txt"
    src.write_text(EVIDENCE_TEXT, encoding="utf-8")

    text, source = case_service._material_text(
        _Mat(stored_path=str(src), parsed_path=""), max_chars=1000,
    )

    assert source == "raw"
    assert EVIDENCE_TEXT[:12] in text


def test_usable_text_rejects_placeholder_and_failure_markers() -> None:
    """只有占位符、或只有识别失败标记的"正文"，一律当没有正文。

    这两种形态都是"看着有内容、其实什么都没有"：显示给律师是一行系统标记，
    而 ``has_text`` 会被判成真 —— 那颗「解析」按钮随之被藏起来，材料再也没救。
    混合正文（有真识别结果、也有一张没识别出来）**必须原样保留**：
    把它一起丢掉，等于把已经识别出来的内容扔了。
    """
    # 没有内容：未回插的占位符
    assert case_service._usable_text("[IMAGE_001]") == ""
    assert case_service._usable_text("[IMAGE_001]\n[TABLE_001]") == ""
    # 没有内容：全是识别失败的标记（多模态不可用时，图片材料解析完就长这样）
    assert case_service._usable_text(
        "[IMAGE_001: 图像内容未识别（多模态服务不可用，仅保留占位）]") == ""
    # **有**内容：回插成功的识别结果
    assert "今借到" in case_service._usable_text("[IMAGE_001: 借条：今借到人民币壹拾万元整]")
    # **有**内容：真识别结果旁边挂着一张没识别出来的，不能连坐
    assert "今借到" in case_service._usable_text(
        "[IMAGE_001: 借条：今借到人民币壹拾万元整]\n[IMAGE_002: 图像内容未识别（识别失败）]")
    # 普通正文不受影响
    assert case_service._usable_text(EVIDENCE_TEXT) == EVIDENCE_TEXT
    assert case_service._usable_text("   ") == ""


def test_standalone_image_is_extracted_not_rejected(tmp_path):
    """单张图片（律师从微信发来的照片）必须能进抽取层，不再被整体拒绝。

    修之前：``不支持的文件类型：.jpeg`` —— 照片永远进不了证据链，
    而界面上只显示"没有可读的正文"，看不出是格式不支持。
    """
    src = tmp_path / "借条.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)   # 不解码，只验证分派与形状

    out = tx.extract(src)

    assert len(out.blocks) == 1, f"图片没被当成一个块：{out.blocks}"
    block = out.blocks[0]
    assert block.kind == "image"
    # 必须指向**原图**：视觉客户端按后缀判 mime，原图正是它要的那份字节。
    assert block.image_path == str(src)
    # 正文里必须留占位符 —— ``splice_placeholders`` 是按占位符替换来回插的，
    # 没有它，识别结果就无处可放（"识别成功却什么都看不到"）。
    assert "[IMAGE_001]" in out.text


def test_standalone_image_without_vision_reports_the_reason(tmp_path, out_dir):
    """图片 + 视觉不可用：产物仍要落盘，并**把原因写进去**。

    这条是用户实际遇到的那一类材料（微信发来的照片）。两个后果都要成立：
      · 正文判为"没有正文"（不许拿系统标记冒充材料内容）；
      · 但原因必须能读出来 —— 否则界面只能说"没有抽出文字"，律师只能反复点解析。
    """
    src = tmp_path / "借条.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)

    # 传 do_vision=False 是为了**不联网**：这里要验的是"视觉用不了时怎么交代"，
    # 而不是去探一个本机可能不存在的 VLM 服务（那会让用例挂上超时）。
    doc = asyncio.run(parse_document(src, do_vision=False))

    assert doc.parsed_path, "图片解析没落盘 —— 界面连原因都读不到"
    assert Path(doc.parsed_path).is_file()

    text, source = case_service._material_text(
        _Mat(stored_path=str(src), parsed_path=doc.parsed_path), max_chars=1000,
    )
    assert (text, source) == ("", ""), (
        f"把系统标记当成正文给出来了（{text!r}）—— 律师会以为这是材料内容"
    )

    reason = case_service._parsed_gap_reason(doc.parsed_path)
    assert reason, "产物里没有记下原因，界面只能含糊地说'没有抽出文字'"
    assert "多模态" in reason, f"原因没说清是识别服务的事：{reason!r}"


def test_gap_reason_explains_why_there_is_no_text(tmp_path, out_dir):
    """抽不到文字时，界面要能说出**为什么** —— 原因来自产物里的 ``evidence_gaps``。

    图片/扫描件抽不到字几乎总是"多模态服务不可用"，而那不是材料的问题：
    不说清原因，律师唯一能做的就是反复点「解析」，点一百次也一样。
    """
    product = tmp_path / "扫描件.parsed.json"
    product.write_text(json.dumps({
        "text": "",
        "evidence_gaps": [{"kind": "vlm", "reason": "多模态识别服务不可用",
                           "impact": "扫描件语义信息未纳入分析"}],
    }, ensure_ascii=False), encoding="utf-8")

    assert case_service._parsed_gap_reason(str(product)) == "多模态识别服务不可用"
    # 取不到产物时不许抛：这条路径在"文件被清理了"的材料上天天走。
    assert case_service._parsed_gap_reason("") == ""
    assert case_service._parsed_gap_reason(str(tmp_path / "没有这个文件.json")) == ""


# =========================================================================== 接口层
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def lawyer(client):
    """一名测试律师 + 一件诉讼案件（材料只对诉讼案件开放）。"""
    user = user_service.create_user(_uniq("matlawyer"), PASSWORD, role="lawyer",
                                    full_name="材料查看测试律师", team_id=None)
    r = client.post(f"{API}/auth/login",
                    json={"username": user["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    case_id = _uniq("MAT-LIT")
    r = client.post(f"{API}/cases", headers={"Authorization": f"Bearer {token}"},
                    json={"case_id": case_id, "title": "材料产物回归案",
                          "case_type": "litigation"})
    assert r.status_code in (200, 201), r.text

    yield {"token": token, "case": case_id}

    try:
        case_service.delete_case(case_id, case_service.INTERNAL)
    except Exception:  # noqa: BLE001
        pass


def test_parse_then_content_serves_the_artifact(client, lawyer, out_dir):
    """上传 → 解析 → 查看正文：``source`` 必须是 ``parsed``，正文非空。

    这是律师真正走的那条路。修好之前它返回的是 ``source="raw"``（纯文本材料）
    或空正文（PDF / 图片），而界面对后者只会说"请先点解析"。

    依赖 ``out_dir``（把产物目录指到临时目录）是**必须**的：否则每跑一次测试，
    项目真实的 ``data/processed/`` 里就会多一个没人认领的 ``*.parsed.json``。
    """
    auth = {"Authorization": f"Bearer {lawyer['token']}"}
    r = client.post(
        f"{API}/cases/{lawyer['case']}/materials", headers=auth,
        files={"file": ("借条.txt", EVIDENCE_TEXT.encode("utf-8"), "text/plain")},
        data={"doc_type": "evidence"},
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]

    r = client.post(f"{API}/materials/{mid}/parse", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["chars"] > 0, "解析没抽出正文"

    r = client.get(f"{API}/materials/{mid}/content", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["source"] == "parsed", (
        f"查看正文没走解析产物（source={body['source']!r}）—— "
        "parsed_path 大概率又没写进库（见本文件开头的说明）"
    )
    assert body["has_text"] is True, "明明读到了正文，却告诉界面没有"
    assert EVIDENCE_TEXT[:12] in body["text"], "正文内容对不上"
    # 路径泄漏那条边界同时守一下：产物路径是服务端的，不进响应体
    # （另有 tests/test_no_path_leak.py 专门守它，这里顺带保证这条链路也干净）。
    assert "parsed_path" not in body, "接口把服务端产物路径带出去了"


# =========================================================================== 界面
def test_viewer_offers_the_parse_button_it_tells_you_to_click() -> None:
    """「查看材料」里必须有「解析」按钮，且解析完要重读正文。

    后端那句提示一直在说"点「解析」"，而查看器里原先没有这颗按钮 ——
    律师只能关掉弹层、回到列表里去找，而列表那一行看着就是"没有正文"，
    整条链路的观感是"坏了"。**跨层契约**：改提示语时别忘了这边那颗按钮。

    同时也守"点了像没反应"：解析接口不回传全文（见 ``api/v1/cases.py``），
    不重读一次正文，律师点完屏幕上什么都不会变。
    """
    js = _JS_LINE_COMMENT.sub(r"\1", APP_JS.read_text(encoding="utf-8"))
    assert "data-mv-parse" in js, "查看器里没有绑定「解析」按钮的抓手指令"

    i = js.find("function parseFromViewer(")
    assert i >= 0, "app.js 里找不到 parseFromViewer()"
    depth = 0
    for k in range(js.index("{", i), len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                body = js[i:k + 1]
                break
    else:  # pragma: no cover - 代码坏了才会走到
        raise AssertionError("parseFromViewer() 的大括号不配对")

    assert "'/parse'" in body, "那颗按钮没打解析接口"
    assert "loadMaterialView(" in body, (
        "解析完没有重读正文（解析接口不回传全文）—— 点了屏幕上什么都不会变"
    )
