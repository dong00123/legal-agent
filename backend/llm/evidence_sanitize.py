"""对外证据文本清洗：检索命中里携带的本地路径与解析器内部标识，不得进入任何对外文本。

背景（2026-10 真机 bug）：回答正文出现
    ``file:///C:/Users/毛义森/Desktop/地域/wage-tables.html [TABLE_002: 工资明细]``
前端把 ``file://`` 与 ``[TABLE_002: …]`` 当成附图源，渲染成"第二张照片"。
根因有两段：
1. 知识库索引了本地生成的 html，``title`` / 片段里带 ``file://`` 绝对路径；
2. ``parser.text_extractor.splice_placeholders`` 把表格/图片占位符回插成
   ``[TABLE_002: 工资明细]`` 形式，随正文进入 chunk，检索命中后被模型照抄进回答。

本模块在**证据进入模型视野之前**（prompts 的证据块）与**检索命中构造输出时**
（rag.pipeline 的 _hit_to_*）统一剥离这些技术元素：

- ``file://`` URL 整段删除（本地路径不属于法律事实）；
- Windows 绝对路径（``C:\\...`` / ``C:/...`` / 网络路径）压缩为**文件名**，
  保留可读性、去掉泄漏路径；
- ``[TABLE_xxx: 语义]`` / ``[IMAGE_xxx: 语义]`` 占位符：去掉编号与方括号，
  **保留语义描述**（"工资明细"这类语义对律师有用）；无语义的占位符整体删除。

原则：只删/压缩技术元素，不改动法律语义；幂等（重复清洗结果不变）。
"""

from __future__ import annotations

import re

# ``file:///C:/Users/...`` 整段删除（含 file:// 与 file:/// 两种前缀）。
_FILE_URL_RE = re.compile(r"file://[^\s)\]」』】》]*", re.I)

# Windows 绝对路径：盘符路径（C:\... 或 C:/...）与 UNC 网络路径（\\host\share\...）。
# 末尾不含空白与常见闭合标点；负向后顾排除字母数字粘连的误伤。
_WIN_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Za-z]:[\\/][^\s)\]」』】》]*|\\\\[^\\\s]+\\[^\s)\]」』】》]*)"
)

# 解析器占位符：``[TABLE_002]`` / ``[TABLE_002: 工资明细]`` / ``[IMAGE_001: 语义]``。
# 编号至少 3 位数字（parser 用 ``{:03d}`` 生成），组 1 为可选语义描述。
_PLACEHOLDER_RE = re.compile(
    r"\[(?:TABLE|IMAGE)_\d{3,}(?:\s*[:：]\s*([^\]\n]*))?\]", re.I
)


def _file_name_of(path: str) -> str:
    """把任意路径压缩成最后一段文件名；空则返回空串。"""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.strip()


def sanitize_evidence_text(text: str) -> str:
    """清洗证据正文/片段：删 file://、压缩路径、去占位符编号（保留语义）。"""
    if not text:
        return text
    t = _FILE_URL_RE.sub("", text)
    t = _PLACEHOLDER_RE.sub(
        lambda m: (m.group(1) or "").strip() if m.group(1) and m.group(1).strip() else "",
        t,
    )
    t = _WIN_PATH_RE.sub(lambda m: _file_name_of(m.group(0)), t)
    # 压缩连续空白（删除/替换后可能残留），但保留换行结构
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def sanitize_display_title(title: str) -> str:
    """清洗展示标题：只处理 file:// 与路径压缩；空结果时回退原标题。

    占位符不会出现在标题里，但 `file://` 路径可能被直接当成 title 入库，
    此处先剥 ``file://`` 前缀、再把剩余路径压成文件名（保留可读性），
    避免引用清单 / 前端卡片把整条路径亮给律师。
    """
    if not title:
        return title
    t = title.replace("file:///", "").replace("file://", "")
    t = _WIN_PATH_RE.sub(lambda m: _file_name_of(m.group(0)), t)
    t = _FILE_URL_RE.sub("", t)  # 兜底：剥残留的 file:// 前缀（无盘符情形）
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t or title
