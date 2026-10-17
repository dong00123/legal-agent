"""前端静态资源与登录页结构的守卫。

这几条都不是"代码风格"问题，而是**一旦破坏、症状会严重误导排查方向**的坑：

1. ``hidden`` 属性必须真的能隐藏元素。浏览器给的
   ``[hidden] { display: none }`` 来自 UA 样式表，优先级极低，任何一条
   显式 ``display`` 都能盖掉它。登录页三个表单（``#fPwd`` / ``#fSms`` /
   ``#fReg``）就是靠 ``$('#fPwd').hidden = m !== 'pwd'`` 这行切换的 ——
   一旦哪个 form 被某条 ``display`` 规则命中，三个表单会**同时显示**。
   旧版（``web/css/app.css``）踩过一模一样的坑：``.login-screen`` 写了
   ``display: grid``，于是它带着 ``hidden`` 照样占位，登录页下方多出
   整整一屏空白可滚动区，而页面上什么都看不见。

2. 静态资源必须发 ``Cache-Control``。Starlette 默认不发，浏览器于是走
   启发式缓存，期间连 F5 都不回源 —— 表现是"改了前端刷新没变化"，
   极易误判成 CSS 写错了。

3. 登录页那几个表单 id 与字段名是 JS 的唯一抓手。重构 HTML 时漏掉一个，
   登录功能会整体失效（拿不到表单/输入框），而不是样式走样那么明显。

4. 容器部署那条路（nginx 用 alias 直接 serve ``/ui``）必须单独守缓存策略。
   它**绕过 FastAPI**，后端那层 ``no-cache`` 在那里根本不生效。

5. **图标 sprite 少一个 symbol 不会报错**。``icon()`` 查不到就直接吐一个空的
   ``<svg>`` —— 按钮在页面上是一片空白，控制台干干净净，只能靠肉眼发现。
   ``tests/test_client_ui.py::test_icon_names_are_defined`` 只扫 ``client/``
   （PySide6 那套），Web 这边此前没有守卫。

6. **文书编辑页（``#pg-docedit``）的每个 id 都是 app.js 的抓手**。少一个不是
   样式走样，而是那一整块功能静默失效；而 ``hidden`` 属性在这个页面上是**反效果**
   （``web/index.html`` 里没有 ``[hidden]`` 规则，见第 1 条）。

⚠️ 2026-09-20 前端整体换版：``web/`` 从"多文件 ESM 版"
（``css/app.css`` + ``js/*.js``）换成了"三件套"
（``index.html`` + ``login.html`` + ``app.js``，样式内联在 HTML 的
``<style>`` 里）。本模块守的**契约一条没变，变的只是抓手** ——
``web/css/app.css`` 与 ``web/js/main.js`` 已随换版移除，
别再把它们当成"文件丢了"而加回来。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INDEX = WEB / "index.html"
LOGIN = WEB / "login.html"
APP_JS = WEB / "app.js"

# 新版前端的样式全部内联在各 HTML 的 <style> 里（旧版是 web/css/app.css）。
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)

# 带 hidden 属性、初始必须隐藏的登录表单。只有 #fPwd 默认显示，
# 另外两个靠 JS 的 `.hidden = m !== 'sms'/'reg'` 切换。
_HIDDEN_ATTR_IDS = ("fSms", "fReg")

# JS 绑定登录逻辑时依赖的表单 id，缺一个对应那条登录路径就整体不可用。
_LOGIN_FORM_IDS = ("fPwd", "fSms", "fReg")

# 三个表单里被 JS 按 name 取值的字段（`$('#fPwd [name=pwd]')`）。
_LOGIN_FIELD_NAMES = ("id", "pwd", "pwd2", "tel", "code")


def _inline_css(path: Path) -> str:
    """取某个 HTML 里所有 ``<style>`` 块的内容。

    抽成函数便于单独测它 —— 扫描器本身写错时（比如正则漏掉了
    ``<style media=...>`` 这种带属性的开标签），"没扫到"会被当成
    "没违规"，测试照样全绿。
    """
    return "\n".join(_STYLE_BLOCK.findall(path.read_text(encoding="utf-8")))


def _all_css() -> str:
    """两页的样式合并 —— 规则写在哪一页都该被看见。"""
    return _inline_css(INDEX) + "\n" + _inline_css(LOGIN)


# 行注释：只剥「行首」或「前面是空白/;/{/}」的 ``//``，
# 这样 ``'https://…'`` 里的双斜杠不会被误当成注释起点。
_JS_LINE_COMMENT = re.compile(r"(^|[\s;{}])//[^\n]*", re.M)
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_js_comments(src: str) -> str:
    """剥掉 JS 注释后再做关键字扫描。

    不剥的后果实测过：本文件那条「不许用 ``f.text.length``」的断言，
    被自己代码旁边那句「**不要退回 f.text.length**」的注释绊红 —— 注释是在解释
    为什么不能用它，反倒被判成违规。**注释里的字样不能当代码看**。
    """
    return _JS_LINE_COMMENT.sub(r"\1", _JS_BLOCK_COMMENT.sub("", src))


@pytest.mark.parametrize("path", [INDEX, LOGIN, APP_JS],
                         ids=["index.html", "login.html", "app.js"])
def test_web_assets_exist(path: Path) -> None:
    """前置条件单列：路径写错时下面几条会以"找不到关键字"的形式失败，
    看着像内容问题，实际是文件不在了。"""
    assert path.exists(), f"前端文件不存在：{path}"


def test_inline_css_extractor_works() -> None:
    """抽取器自检：要真的抽到样式，且不能把 ``<script>`` 一起吞进来。"""
    css = _all_css()
    assert len(css) > 1000, f"两页合起来的内联样式只有 {len(css)} 字符，抽取逻辑可能坏了"

    assert "<script" not in css, "抽取器把 <script> 也吞进来了"
    assert "--tp" in css or "--bg" in css, "没抽到设计变量，说明没取到真正的样式块"


def _hidden_rule(css: str) -> str | None:
    """取出 ``[hidden]`` 规则的声明块；没有则返回 ``None``。

    抽成函数是为了能单独测它 —— 扫描器本身写错时（比如被注释里的
    同名文本骗到），"没扫到"会被当成"没违规"，测试照样全绿。
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    m = re.search(r"\[hidden\]\s*\{([^}]*)\}", stripped)
    return m.group(1) if m else None


def test_hidden_rule_detector_actually_detects() -> None:
    """检波器自检：**它必须能分辨三种输入**，否则上面那条测试毫无意义。"""
    assert _hidden_rule("a{color:red}\n[hidden]{display:none !important;}") is not None
    # 注释里的同名文本不算 —— 第一版就是被这个骗到的
    assert _hidden_rule("/* [hidden] { display: none } */\n.foo{color:red}") is None
    # 规则存在但内容是别的
    assert "display" not in (_hidden_rule("[hidden]{color:red}") or "display")


def test_hidden_attribute_rule_is_present() -> None:
    """``[hidden]`` 必须有强制生效的规则。

    **不要因为"浏览器本来就支持 hidden"而删掉它。** 支持归支持，优先级
    只有 UA 那一档；本项目就踩过 —— 见模块 docstring 第 1 条。
    登录页那三个表单没有 class、现在也没被任何 ``display`` 命中，所以
    这条规则眼下"看起来多余"，但它是唯一挡住"将来给 form 或其祖先加
    display 就静默坏掉"的东西 —— 三个表单一起显示，页面上不会报错。

    匹配前先剥掉注释：本文件的说明文字里也写着 ``[hidden] { display: none }``，
    直接 split 会命中注释里那一次（第一版就是这么写错的）。
    """
    block = _hidden_rule(_all_css())
    assert block is not None, (
        "两页的内联样式里都缺少 [hidden] 规则。删掉它会让 #fSms / #fReg "
        "这类靠 hidden 属性切换的表单在被 display 规则命中时仍然占位 —— "
        "三个登录表单会同时显示。"
    )
    assert "display" in block and "none" in block, f"[hidden] 规则没写 display:none：{block!r}"
    # 必须带 !important：类选择器（如 .form）与属性选择器同属一个优先级档，
    # 不加就变成依赖书写顺序，而这类"看不到的布局参与"极难排查。
    assert "!important" in block, (
        "[hidden] 规则缺少 !important —— 同优先级下靠书写顺序取胜太脆弱。"
    )


def test_login_forms_are_intact() -> None:
    """三个表单的 id 与字段 name 一个都不能少 —— 少了是功能整体失效。"""
    html = LOGIN.read_text(encoding="utf-8")
    missing_forms = [i for i in _LOGIN_FORM_IDS if f'id="{i}"' not in html]
    assert not missing_forms, f"登录页缺少这些表单 id，JS 将绑定失败：{missing_forms}"
    missing_names = [n for n in _LOGIN_FIELD_NAMES if f'name="{n}"' not in html]
    assert not missing_names, f"登录页缺少这些字段 name，表单取不到值：{missing_names}"


def test_hidden_elements_still_exist() -> None:
    """那两个高危表单仍带着 hidden 属性 —— 否则首屏会同时闪出三个表单。"""
    html = LOGIN.read_text(encoding="utf-8")
    for el_id in _HIDDEN_ATTR_IDS:
        marker = f'id="{el_id}"'
        assert marker in html, f"{el_id} 不存在了"
        # 取该元素所在的那一行（本项目的写法是单行开标签）
        line = next(ln for ln in html.splitlines() if marker in ln)
        assert "hidden" in line, f"{el_id} 丢了 hidden 属性，初始状态会闪现"


def test_static_files_send_no_cache() -> None:
    """``/ui`` 下的静态资源要带 ``Cache-Control``。

    没有它 → 浏览器启发式缓存 → "改了前端刷新看不到"（实测踩过）。
    断言的是"有值且不是长缓存"，不锁死具体策略：将来若改成带 hash 的
    长缓存（那才是更优解），这条不该拦着。
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from backend.app.main import app

    with fastapi_testclient.TestClient(app) as client:
        for asset in ("/ui/app.js", "/ui/login.html"):
            resp = client.get(asset)
            assert resp.status_code == 200, f"静态资源路径变了？{asset}"
            cc = resp.headers.get("Cache-Control", "")
            assert cc, (
                f"{asset} 没有 Cache-Control —— 浏览器会启发式缓存，"
                "用户改了前端按 F5 也看不到变化。"
            )
            assert "no-store" not in cc, "no-store 会让每次完整重传，白白牺牲 304"


def test_nginx_does_not_long_cache_ui() -> None:
    """容器部署这条路（nginx 直接 serve ``/usr/share/nginx/html/ui``）不能长缓存。

    后端那层 ``no-cache`` 在这里**根本轮不到**：静态文件是 nginx 用 alias 从
    bind mount 里读的，压根没经过 FastAPI。所以这里必须单独守一条。
    前端文件名不带内容 hash，一旦加了 ``expires`` / ``max-age``，发新版时浏览器
    就是新 HTML 配旧 CSS —— 症状是"改了没生效"或"样式错乱"，从服务端查不出来。
    """
    conf = ROOT / "deploy" / "nginx" / "nginx.conf"
    if not conf.exists():
        pytest.skip("缺 deploy/nginx/nginx.conf，跳过容器侧缓存守卫")

    text = conf.read_text(encoding="utf-8")
    text = re.sub(r"#.*", "", text)  # 剥注释，免得注释里的字样骗过扫描

    # 取 /ui/ 这个 location 块
    m = re.search(r"location\s+/ui/\s*\{(.*?)\}", text, flags=re.S)
    assert m, "nginx.conf 里找不到 location /ui/ 块"
    block = m.group(1)

    assert "expires" not in block, (
        "location /ui/ 里出现了 expires —— 前端文件名不带 hash，"
        "长缓存会让新版 HTML 配上旧 CSS/JS。删掉它，只留 no-cache。"
    )
    assert "max-age" not in block, (
        "location /ui/ 里出现了 max-age，理由同上：无 hash 文件名不能长缓存。"
    )
    assert "no-cache" in block, (
        "location /ui/ 没有声明 no-cache —— 静态资源会被浏览器启发式缓存。"
    )


def test_contract_char_count_uses_backend_field() -> None:
    """合同解析列表里的「（xx 字）」必须用后端的 ``f.chars``，**不许回到 ``f.text.length``**。

    来历：用户报「合同识别每次字数都不一样」。``text.length`` 把翻页空白、字间空格、
    填空线下划线长度全算进去，而这三样每次识别都不同 —— 实测同一张扫描件跑 6 次
    给出 6 种字数（811/807/809/797/810/804）。后端改成"不计空白"后极差落到 1。

    这条防的是"有人觉得 ``chars`` 多余、又改回 ``text.length``"
    （那看起来更"简单"，但会立刻重现用户看到的现象）。
    口径本身的行为契约在 ``tests/test_vision_transcribe_stability.py``。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    assert "f.chars" in js, "前端没有用后端返回的 chars 字段显示字数"
    assert "f.text.length" not in js, (
        "字数又退回 text.length 了 —— 它含空白，同一张扫描件每次识别都会给出不同数字"
    )


def test_js_comment_stripper_works() -> None:
    """剥注释器自检：既然是它决定了上面那条断言看不看得见代码，它自己得被测。

    两种注释都要剥掉；而 ``'https://…'`` 里的双斜杠必须留下 —— 误剥会静默
    截断整行代码，让后面的断言在"缺了一段"的文本上跑。
    """
    src = 'var a = 1; // f.text.length\n/* f.text.length */ var b = "https://x/y";\n'
    out = _strip_js_comments(src)
    assert "f.text.length" not in out
    assert '"https://x/y"' in out, "字符串里的双斜杠被误当注释剥掉了"
    assert "var a = 1;" in out and "var b" in out


# --------------------------------------------------------------- 图标 sprite
# ``icon()`` 对**不存在的 symbol 不报错**，直接渲染成一个空的 <svg> ——
# 按钮在页面上就是一片空白，控制台一条错都没有，只能靠肉眼发现。
# ``tests/test_client_ui.py::test_icon_names_are_defined`` 只扫 ``client/``
# （PySide6 那套），Web 这边一直没有守卫，加这一组。
_SYMBOL_DEF = re.compile(r'<symbol id="i-([\w-]+)"')
_HREF_USE = re.compile(r'<use href="#i-([\w-]+)"')
_ICON_LITERAL = re.compile(r"icon\(\s*'([\w-]+)'")


def _symbol_names() -> set[str]:
    return set(_SYMBOL_DEF.findall(INDEX.read_text(encoding="utf-8")))


def test_icon_detector_actually_detects() -> None:
    """检波器自检：三种来源都必须能被抓到，否则下面那条测试毫无意义。"""
    assert _SYMBOL_DEF.findall('<symbol id="i-back" viewBox="0 0 24 24"/>') == ["back"]
    assert _HREF_USE.findall('<use href="#i-bold"/>') == ["bold"]
    assert _ICON_LITERAL.findall("'<svg>' + icon('cross', 'sm')") == ["cross"]


def test_every_icon_reference_has_a_symbol() -> None:
    """引用了 sprite 里不存在的图标 → 那个按钮是**空白**的，而且不报错。

    ``app.js`` 里出现的是字面量 ``icon('xxx')``；``icon(name)`` 这种参数化调用
    不在扫描范围内（名字来自 DOC_ICON 这类表，另有测试守表里的值）。
    """
    defined = _symbol_names()
    html = INDEX.read_text(encoding="utf-8")
    referenced = set(_HREF_USE.findall(html))
    referenced |= set(_ICON_LITERAL.findall(_strip_js_comments(APP_JS.read_text(encoding="utf-8"))))
    missing = sorted(referenced - defined)
    assert not missing, (
        f"这些图标在 sprite 里没有定义，页面上会渲染成空白：{missing}。"
        "先往 index.html 的 <svg> 里加 <symbol id=\"i-名字\">，再接线。"
    )


# --------------------------------------------------------------- 文书编辑页
# 点「生成」后进入的全屏编辑页。它的每一个 id 都是 app.js 的抓手，
# 少一个不是"样式走样"而是**功能整体不可用**（拿不到元素就静默什么都不做）。
_DOC_EDIT_ID = re.compile(r"\$\('#(de[A-Za-z]*|pg-docedit)'\)")

# 单独钉住这几个：它们是这一页的存在意义（返回键、文档容器、发送），
# 万一有人在 app.js 里把它们改写成了别的取法，上面那条扫描就"扫不到=没问题"了。
_MUST_HAVE = ("deBack", "deDoc", "deChatText", "deChatSend", "deHist")


def test_doc_edit_page_has_all_handles() -> None:
    """app.js 里取过的每个 ``#de…`` 都必须在 HTML 里真的存在。

    反过来写（在测试里写死一份 id 清单）只能守住"我记得的那几个"；
    从 app.js 反推则能守住**每一个**抓手，包括以后新加的。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    wanted = set(_DOC_EDIT_ID.findall(js))
    assert set(_MUST_HAVE) <= wanted, (
        f"app.js 里已经没有在取这些元素了，编辑页等于废了：{sorted(set(_MUST_HAVE) - wanted)}")
    html = INDEX.read_text(encoding="utf-8")
    missing = sorted(i for i in wanted if f'id="{i}"' not in html)
    assert not missing, (
        f"app.js 取这些 id，但 index.html 里没有，运行时会静默取到 null：{missing}")


def test_doc_edit_page_is_not_hidden_by_the_hidden_attribute() -> None:
    """新页面**不能靠 ``hidden`` 藏**，只能靠 ``.pg`` / ``.on`` 这套切换。

    ``web/index.html`` 里没有 ``[hidden]`` 规则（那条只在 ``login.html``，
    而两个页面的 CSS 在这个测试文件里是合起来查的，所以库里看着是绿的）。
    一旦给这一页写 ``display:flex`` 又用 ``hidden`` 收，就会盖掉 UA 样式表里
    那条 ``[hidden]{display:none}`` —— 页面**永远显示**，把整站遮住。
    """
    html = INDEX.read_text(encoding="utf-8")
    line = next(ln for ln in html.splitlines() if 'id="pg-docedit"' in ln)
    assert "hidden" not in line, (
        "pg-docedit 带了 hidden 属性。它靠 .pg{display:none} / .on{display:block} 切换，"
        "而本页 .on 时会写 display:flex —— hidden 会被盖掉，页面永远显示。")
    css = _inline_css(INDEX)
    m = re.search(r"#pg-docedit\.on\s*\{([^}]*)\}", css)
    assert m, "找不到 #pg-docedit.on 的定位规则"
    block = m.group(1)
    assert "fixed" in block and "inset:0" in block, f"这一页要全屏铺满：{block!r}"
    # z-index 梯子：.top 是 50、.lp-modal 是 90、.toast 是 99。
    # 取 60 才盖得住顶栏、又不挡弹层与提示。
    zi = re.search(r"z-index\s*:\s*(\d+)", block)
    assert zi and 50 < int(zi.group(1)) < 90, (
        f"z-index 要在 50（.top）与 90（.lp-modal）之间，实际 {zi and zi.group(1)}")


def test_rich_text_toolbar_commands_are_wired() -> None:
    """工具栏上每个按钮都要有 ``data-cmd``，且 app.js 真的处理了它。

    ``data-cmd`` 拼错一个字母，对应按钮**点了没反应也不报错**。
    """
    html = INDEX.read_text(encoding="utf-8")
    block = re.search(r'id="deTools"(.*?)</div>\s*<div class="de-scroll"', html, re.S)
    assert block, "找不到工具栏容器"
    cmds = set(re.findall(r'data-cmd="([A-Za-z]+)"', block.group(1)))
    for want in ("undo", "redo", "bold", "italic", "underline", "strikeThrough",
                 "indent", "outdent", "justifyLeft", "justifyCenter", "justifyRight",
                 "insertUnorderedList", "insertOrderedList"):
        assert want in cmds, f"工具栏缺少命令按钮：{want}"
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    assert "b.getAttribute('data-cmd')" in js, "app.js 没有读取 data-cmd，按钮全都没接线"


def test_inline_result_area_is_fully_removed() -> None:
    """旧的内联结果区（``#docResult``）连同它的读写点必须一起消失。

    **留在代码里不会报错**：元素没了之后 ``$('#docResult')`` 返回 null，
    ``if (box)`` 一道道判空全部跳过 —— 症状是"删草稿后旧结果区还留着"
    这种说不清哪来的残留。所以宁可用这条测试钉死。
    """
    html = INDEX.read_text(encoding="utf-8")
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    for dead in ("docResult", "docSavedTag", "docEditor", "docPreview"):
        assert dead not in js, f"app.js 里还引用着已删除的 {dead}"
    # ``doc-preview`` 是合同审查页的类名，与上面那个 id 不是一回事，不能一起删
    assert 'id="docResult"' not in html
    assert 'id="contractResult"' in html, "误删了合同审查页的结果区"


# ------------------------------------------------ 合同审查页：左栏正文 / 结果区
# 2026-09-29 改版：左栏从只读 ``<pre>``（带 ``<!-- 第 1 页 -->`` 漏出来的页标、
# 孤立页码行、被 PDF 版面硬切断的半句话）换成排版好的可编辑合同纸；右栏从
# "一大串文本 + 只显示前 8 条的 <ul>" 换成逐条风险卡。
#
# 结果区 2026-09-29 又去掉了「主体审查」页签（用户点名"不管甲方还是乙方、
# 还是中立，都删掉"）—— 没有工商数据源，那一页只能给出一排「未接入」。
# 连类名一起清干净由 ``test_subject_review_pane_is_fully_removed`` 钉着。
#
# 这里守的两件事，共同点是**坏了不报错、只是静默走样**：
#
# 1. ``ctPaintDoc()`` 会重画整块左栏。它**没有幂等守卫**的话，律师改完字，
#    任何一次 ``ctEnter()`` / 重跑都会把编辑悄悄冲回后端原文 —— 屏幕上看起来
#    就是"我改的东西自己没了"。守卫是 ``doc._src === text`` 那一行。
# 2. 分页块的原始标记 ``<!-- 第 N 页 -->`` 存在 ``data-mark`` 属性里（屏上只显示
#    "第 N 页"）。``ctReadDoc()`` 不认 ``data-mark`` 的话，回收出的文本会少掉
#    每个页标约 16 个字，而**字数徽标是按 ``count_chars`` 算的**（后端口径，
#    页标计进字数）—— 症状是"我什么都没改，字数怎么少了 48"。
_CONTRACT_DOC_IDS = ("ctSrcDoc", "ctDocChars", "ctDocDirty",
                     "ctPaneRisk", "contractResult")


def test_contract_page_uneditable_source_is_fully_removed() -> None:
    """旧的只读 ``<pre id="ctSrcText">`` 必须连 CSS 一起消失。

    HTML 与 JS 里各留一半是最常见的半截状态：``$('#ctSrcText')`` 返回 null，
    一句句判空全跳过 —— "没坏，但左栏永远是空的"，能一直躺着没人发现。
    """
    html = INDEX.read_text(encoding="utf-8")
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    css = _inline_css(INDEX)

    assert "ctSrcText" not in html, "index.html 里还留着只读的 ctSrcText"
    assert "ctSrcText" not in js, "app.js 里还引用着已删除的 ctSrcText"
    assert ".ct-doc pre" not in css, (
        "index.html 里还有 .ct-doc pre 规则 —— 新正文是 .ct-paper，"
        "那条规则会把排版好的合同重新按等宽纯文本显示"
    )
    assert ".ct-paper{" in css, "左栏正文的版式样式没了"


def test_contract_page_anchors_are_intact() -> None:
    """改版后的每个抓手都必须是现的 —— 少一个不是走样，是整块静默失效。"""
    html = INDEX.read_text(encoding="utf-8")
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    for i in _CONTRACT_DOC_IDS:
        assert f'id="{i}"' in html, f"index.html 缺少 {i}"

    # ``[hidden]`` 在这里是可靠的（index.html 有那条 !important 规则），
    # 但**可编辑**靠的是 contenteditable，写成别的属性编辑器会变成只读，
    # 而页面上没有任何迹象说明为什么打不了字。
    m = re.search(r'<div[^>]*id="ctSrcDoc"[^>]*>', html)
    assert m, "找不到左栏正文容器 ctSrcDoc"
    assert "contenteditable" in m.group(0), "左栏正文不可编辑了"


def test_subject_review_pane_is_fully_removed() -> None:
    """「主体审查」页签、面板与它的抽字段代码必须**连根拔掉**（2026-09-29 用户点名：
    「这个主体审查删掉，不管甲方还是乙方，还是中立，都删掉」）。

    没有工商数据源，那一页除了合同里本来就写着的那几行，剩下的全是「未接入工商
    数据源」—— 十项灰字对律师是纯噪声，点进去只为确认"确实没有"。

    ⛔ 半截状态不会报错：``$('#ctPaneSubj')`` 返回 null，一句句判空全跳过，
    屏幕上只是"少了一页"，谁都不会发现面板的 HTML 生成器还在发霉。所以 HTML、
    JS、CSS 三处各查一遍（CSS 留着更隐蔽 —— 类名不再出现，谁也搜不到它）。
    """
    html = INDEX.read_text(encoding="utf-8")
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    css = _inline_css(INDEX)
    for dead in ("ctTabs", "ctTabSubj", "ctPaneSubj", "ctSubjSwitch",
                 "ctSubjectPane", "ctSubjectInfo", "CT_SUBJ_ROWS", "CT_SUBJ_RISKS",
                 "ctPaintSubj", "ctSetPane", "变更审查主体"):
        assert dead not in html, f"index.html 里还留着已删除的主体审查：{dead}"
        assert dead not in js, f"app.js 里还引用着已删除的主体审查：{dead}"
    assert ".ct-subj" not in css and ".ct-tab" not in css, (
        "还在给已删除的主体审查/页签留样式 —— 类名都不再出现，这条规则谁也搜不到"
    )

    # 抽字段那两个小工具是**只服务于主体面板**的，跟着一起走；哪天有人再加回来，
    # 说明整个面板也在回来的路上。
    for helper in ("ctPick", "ctPickNth"):
        assert helper not in js, f"{helper}() 是主体面板专用的，没了调用者就该一起删"

    # ⚠️ 但 ``ctGuessParties`` **必须留着** —— 上面「审查立场」那三张卡（甲方/
    # 乙方/中立）用的就是它，删主体审查顺手删掉它，立场面板会静默失灵。
    assert "function ctGuessParties" in js, "误删了立场面板共用的 ctGuessParties()"
    # 结果区仍在（删页签最容易顺手把装着它的容器一起带走）。
    assert 'id="contractResult"' in html, "误删了合同审查页的结果区"


def test_contract_doc_paint_is_idempotent_and_reads_page_marks() -> None:
    """两条**只在律师动过手之后**才发作的静默故障，只能用源码钉。

    都测不到行为（要 DOM + 一次真实编辑），但都是"改一行就复发"的类型：

    - ``ctPaintDoc`` 少一句 ``doc._src === text`` 守卫 ⇒ 重跑审查会把编辑冲掉。
    - ``ctReadDoc`` 少认 ``data-mark`` ⇒ 回收文本丢页标，字数徽标对不上后端。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    paint = _js_function(js, "ctPaintDoc")
    assert "_src" in paint, (
        "ctPaintDoc() 没有幂等守卫 —— 律师改完字，任何一次重跑/切页都会把"
        "编辑悄悄冲回后端原文（屏幕上就是「我改的自己没了」）"
    )
    read = _js_function(js, "ctReadDoc")
    assert "data-mark" in read, (
        "ctReadDoc() 不认 data-mark —— 分页标记回收不回来，"
        "字数徽标会比后端少（页标是计进 count_chars 的）"
    )
    assert "data-foot" in read, (
        "ctReadDoc() 不认 data-foot —— 并进分页线的那行页脚页码（整行只有一个 "
        "「1」）回收不回来，每页少一个字"
    )
    assert "data-foot" in _js_function(js, "ctBlockHTML"), (
        "ctBlockHTML() 不再输出 data-foot：页脚页码就是只显示不回收，"
        "往返必丢字"
    )
    # 字数口径必须与后端 ``count_chars``（chat.py）一致：不计空白与 _＿□。
    body = _js_function(js, "countChars")
    for ch in ("_", "＿", "□"):
        assert ch in body, f"countChars() 的口径与后端 count_chars 不一致：漏了 {ch}"


def test_watermarks_are_dropped_and_the_date_sits_on_the_right() -> None:
    """用户点名的两条观感：**页面水印不许显示，落款日期靠右**。

    夹具是带水印那一版（每页一行「示例文件 · 无法律效力 · 仅用于产品演示 ·
    DEMO SAMPLE」，落款处「样例章 · 无效」）。两处都不是样式问题：

    - 水印行必须**在续行合并之前**滤掉。落款日期不以句号收尾，滤晚了它会
      被接进日期那一段，屏幕上就是「2024 年 2 月 28 日示例文件 · …」。
    - 日期要单独成块才谈得上右对齐；原来它混在 ``para`` 里左缩进两格。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    blocks = _js_function(js, "ctDocBlocks")
    i_noise = blocks.find("CT_RE_NOISE.test(ln)")
    i_merge = blocks.find("prev().text +=")
    assert -1 not in (i_noise, i_merge), "ctDocBlocks() 少了水印过滤或续行合并"
    assert i_noise < i_merge, (
        "水印行在续行合并**之后**才滤 —— 它会跟上一行（落款日期）粘成一段，"
        "屏幕上变成「2024 年 2 月 28 日示例文件 · 无法律效力…」"
    )
    assert "CT_RE_DATE" in blocks and "'date'" in blocks, (
        "落款日期不再单独成块 —— 右对齐无从谈起"
    )
    # 滤水印与认日期各有一个纯函数/常量，抠出来单独看口径。
    strip = _js_function(js, "ctStripNoise")
    assert "CT_RE_NOISE" in strip, "ctStripNoise() 不再认水印标记"
    m = re.search(r"var CT_RE_NOISE = /([^/\n]+)/;", js)
    assert m, "CT_RE_NOISE 这个常量没了"
    noise = m.group(1)
    for kw in ("示例文件", "样例章", "DEMO"):
        assert kw in noise, f"水印规则漏了 {kw}"
    assert "仅供" not in noise and "参考" not in noise, (
        "水印规则被放宽成了泛词 —— 真合同里「本文件仅供…参考」是常见措辞，"
        "误删等于替当事人改了合同，而且改得毫无痕迹"
    )
    # 解析器插进来的图片占位符（[IMAGE_001: 红色圆形公章…]）同样不许显示：
    # 合同里没有这一行，摆在"合同全文"里律师会以为合同真写了这句话。
    ph = re.search(r"var CT_RE_PLACEHOLDER = /([^/\n]+)/;", js)
    assert ph, "CT_RE_PLACEHOLDER 这个常量没了"
    assert "IMAGE" in ph.group(1), "占位符规则不认 [IMAGE_NNN] 了"
    assert "TABLE" not in ph.group(1), (
        "占位符规则把 [TABLE_00N: …] 也一起删了 —— 表格那块回插的是**真数据**"
        "（工资表、清单），删掉等于把当事人的材料吃了"
    )
    assert "CT_RE_PLACEHOLDER" in strip and "CT_RE_PLACEHOLDER" in blocks, (
        "图片占位符没进过滤名单（切块与清洗各要过一道）"
    )
    # 卡片里那段「原文」：后端按位置切出来的 clause_text 跨页时会把页标、页脚
    # 页码、占位符一起带进来，别原样摆给律师看。
    quote = _js_function(js, "ctCleanQuote")
    for kw in ("CT_RE_PAGE", "CT_RE_PAGENUM", "CT_RE_PLACEHOLDER", "CT_RE_NOISE"):
        assert kw in quote, f"ctCleanQuote() 漏了 {kw} —— 原文里会留下解析痕迹"
    assert "ctCleanQuote(" in _js_function(js, "ctRiskCards"), (
        "风险卡不再清洗原文（clause_text 直接进 HTML）—— 跨页条款的原文里会露出 "
        "<!-- 第 3 页 --> 和孤立的「3」"
    )
    # 解析回来就要滤，且**字数要把滤掉的那几个字减掉**：不然徽标把水印算进
    # 合同字数，律师改一个字就发现数字凭空少一截。
    parse = _js_function(js, "ctParse")
    assert "ctStripNoise(raw)" in parse, (
        "解析回来的正文没滤水印 —— 左栏照旧显示，材料也跟着送审"
    )
    assert re.search(r"merged_chars\s*-\s*dropped", parse), (
        "滤掉的水印没从字数里减掉 —— 徽标会与正文对不上"
    )
    css = _inline_css(INDEX)
    assert re.search(r"\.ct-paper \.ct-date\{[^}]*text-align:right", css), (
        "缺少 .ct-date 的右对齐样式（落款日期回到左缩进两格）"
    )


def test_contract_result_pane_drops_the_report_and_gaps_when_cards_exist() -> None:
    """合同审查结果区：**改后合同在最上面**，接着风险卡，**完整报告、引用校验警示
    都不渲染**，证据缺口只在没有卡时出。

    2026-09-29 用户点名「完整报告，证据缺口，不要」，紧接着又点名「注意 不需要」，
    最后点名「改后的合同放在前面，放在后面我要滑动很久，不方便」。
    四条约束各有各的死法：顺序调换不报错（只会让"律师第一眼看什么"悄悄变回去，
    2026-09-29 之前风险点就压在几千字的报告下面，还 ``slice(0, 8)``）；
    报告、缺口、警示**删过头**同样不报错 —— 这个函数被 AI 助手那条链路共用，
    而助手的主产出恰恰是那份报告，助手也没有风险卡、那行警示是它唯一的引用可信度提示；
    缺口则是降到规则引擎兜底那一趟里"为什么一条风险点都没有"的唯一解释。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    body = _js_function(js, "formatStructuredAnswer")
    i_cards = body.find("ctRiskCards(")
    i_gap = body.find("证据缺口：")
    i_rep = body.find("ct-report")
    assert i_cards > -1, "风险卡没了 —— 那是合同审查页的主产出"
    assert i_cards < i_gap, "证据缺口又跑到风险卡前面去了 —— 主产出被顶出首屏"
    assert i_cards < i_rep, "完整报告又跑到风险卡前面去了"
    # 改后合同：合同审查页里排在**风险卡前面**（不是后面）。
    # 2026-09-29 用户：「改后的合同放在前面，放在后面我要滑动很久」——
    # 十几张卡每张都带条款原文，"生成改后合同"原来被压在整份清单后面。
    i_rev_contract = body.find("parts.push(revBlock)")
    assert i_rev_contract > -1 and i_rev_contract < i_cards, \
        "合同审查页的改后合同要提到风险卡前面（助手那条链路的位置是另一句）"
    assert re.search(r"if\s*\(opts\.contract\s*&&\s*revBlock\)", body), \
        "改后合同提前只该发生在合同审查页"
    assert re.search(r"if\s*\(revBlock\s*&&\s*!opts\.contract\)", body), \
        "助手那条链路的改后合同还得留在后面：那里报告是主产出，先看分析再拿交付物"
    assert "risk_points.slice" not in body, \
        "风险卡别再加 slice 截断（第 9 条之后会永远不显示）"
    assert re.search(r"if\s*\(data\.report\s*&&\s*!opts\.contract\)", body), \
        "完整报告要「合同审查页不渲染、AI 助手照旧」—— 别改成无条件删（删掉的会是助手的回答）"
    assert re.search(r"if\s*\(!opts\.contract\s*\|\|\s*!nCards\)", body), \
        "证据缺口的开关丢了：降到规则引擎那一趟没有风险卡，它是唯一的解释"
    assert re.search(r"data\.citation_check\s*!==\s*'passed'\s*&&\s*!opts\.contract", body), \
        "引用校验那行「注意」的开关丢了/写反了：合同页每趟都拦下 2~3 处，出这行等于" \
        "每份结果都贴同一张告示；助手那条链路必须留着（它没有风险卡）"
    # 改后合同的说明文字不能写死方位：同一个区块在合同页在风险卡上面、在助手在下面
    rev = _js_function(js, "revisionBlock")
    assert "按上面的风险" not in rev and "按下面的风险" not in rev, \
        "「按上面的风险」在合同页会指错方向（区块已经挪到卡片前面了）"
    calls = [ln for ln in js.splitlines()
             if "formatStructuredAnswer(" in ln and "function " not in ln]
    assert len(calls) == 2, f"调用点数量变了（{len(calls)} 处）—— 确认助手那条链路没被顺手改掉"
    assert sum("contract: true" in ln for ln in calls) == 1, \
        "contract:true 只能加在合同审查那一处（助手那条链路的交付物就是报告）"


# ------------------------------------------------ 等待期「执行轨迹」面板（已删除）
# 2026-09-29 摘掉：那是开发期调试信息 —— 节点名（``StatuteNode``）、内部判定
# （``意图=contract_review 判定方式=rule 置信度=0.95``）、甚至意图正则片段
# （``命中=contract_review(审查|审阅|过一…``）会原样摊在等待期的面板上，
# 而这些页面是给律师看的。三个入口（``ask`` / ``askAgent`` / ``startReview``）
# 一起摘掉，等待期只留 ``.tp3`` 转圈气泡。
#
# ⚠️ 类名 ``tl`` 还活着：它是「日程任务」每日分组的条数徽章
# （``<span class="tl">N 条</span>``）。删面板时最容易顺手把它一起删掉。
_DEAD_PANEL_SYMBOL = ("waitPanel", "agentRoleOf", "NODE_AGENT_LABEL",
                      "snapList", "VISIBLE_ROWS", "setIntent")


def test_wait_trace_panel_is_fully_removed() -> None:
    """面板必须连根拔掉，且**不能顺手带走同名/同前缀的东西**。

    **半截代码留着不会报错**：元素没了之后 ``$('.tl', wrap)`` 一律返回 null，
    一句句判空全部跳过 —— 症状是"面板没了、也没坏"，能一直躺着没人发现。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    html = INDEX.read_text(encoding="utf-8")
    css = _inline_css(INDEX)

    left = [s for s in _DEAD_PANEL_SYMBOL if s in js]
    assert not left, f"app.js 里还留着执行轨迹面板的代码：{left}"

    # ``.tl`` 是**单类选择器**，面板在时它就在串味命中那个条数徽章 ——
    # 白送它一圈边框和背景、内边距，``margin-top:10px`` 还让它在
    # ``align-items:center`` 的 flex 行里错位。删净后 app.js 里不该再有
    # 任何 ``.tl`` 选择器，徽章只由 ``index.html`` 的 ``.dayh .tl`` 负责。
    assert ".tl" not in js, (
        "app.js 里还有 .tl 选择器 —— 它是单类选择器，会串到「日程任务」的条数徽章上"
    )

    # —— 别删多 ——
    assert 'class="tl"' in js, "误删了「日程任务」每日分组的条数徽章"
    assert ".dayh .tl{" in css, "误删了每日分组徽章的样式（index.html）"
    assert ".sft .bar{" in css, "误删了页脚「案卷空间已加密」的进度条样式"
    assert "@keyframes tb" in css, "误删了转圈动画：等待期会变成静止画面"
    assert ".tp3{" in css and 'class="tp3"' in js, (
        "等待期不能变成静止画面：三个入口都靠 .tp3 表示「还在跑」"
    )
    assert 'id="contractResult"' in html, "误删了合同审查页的结果区"
    assert "INTENT_LABEL" in js, (
        "INTENT_LABEL 另有 5 处在用（待办行 / 会话恢复 / 日程任务 / 任务详情），"
        "别跟着面板一起删"
    )


def _js_function(src: str, name: str) -> str:
    """按大括号配对抠出 ``function name(...) {...}`` 的函数体。

    正则数不出嵌套的大括号，"抠到哪算哪"的截断会让下面的断言在半段代码上跑。
    """
    i = src.find("function %s(" % name)
    assert i >= 0, f"app.js 里找不到函数 {name}()"
    depth = 0
    for k in range(src.index("{", i), len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"大括号不配对：{name}")


# ------------------------------------------------ 类案检索页：落地页 ↔ 结果态
# 2026-09-29 按参考图改版：居中的英雄区 + 一张大圆角输入卡（设定检索范围 / 附件 /
# 检索）+ 「您可以按照以下场景提问」 + 三张场景卡；检索后切成结果态（英雄区与
# 场景卡收起、输入卡变紧凑、下面是命中列表）。
#
# 这里守的几处**坏了都不报错、只是静默失效**：
#
# 1. 抓手在 HTML 与 app.js 两侧必须对得上。少一个不是走样 —— ``$('#sBtn')``
#    返回 null，一句句判空全跳过，页面上只剩一个点不动的空壳。
# 2. 两段式的类名 ``run`` 是**字符串对齐**的：CSS 认 ``#pg-search.run``、
#    JS 认 ``classList.toggle('run')``。改一边，点了不切换，控制台干干净净。
# 3. 场景卡点一下要做**两件事**：填进输入框 + 开跑。只填不跑会被读成"卡是坏的"，
#    只跑不填则让人看不到自己搜的是什么。
# 4. 检索式里必须**带上范围词**（``csScopeWords()``）且条数走 ``csScope.topk`` ——
#    面板选了却发不出去，是这套界面最容易出现的假功能。
_SEARCH_IDS = ("sInput", "sBtn", "sComp", "sScopeBtn", "sPanel", "sCause", "sCourt",
               "sTopk", "sPicked", "sAttach", "sFile", "sScenes", "sBack",
               "searchHits", "searchFoot", "searchSub")


def test_case_search_page_is_wired_end_to_end() -> None:
    """类案检索页的抓手、两段式切换、场景卡与范围词，一处不落地钉住。"""
    html = INDEX.read_text(encoding="utf-8")
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    css = _inline_css(INDEX)

    for i in _SEARCH_IDS:
        assert f'id="{i}"' in html, f"index.html 缺少类案检索页的抓手 {i}"
        assert f"$('#{i}')" in js, f"app.js 里没有任何地方取过 #{i} —— 抓手是死的"

    # 旧版的抓手与样式必须连根清掉（半截状态不报错：老标签的监听器还挂着，
    # 只是页面上已经没有那颗按钮了）。
    for dead in ("searchChips", "sbox"):
        assert dead not in html and dead not in js, f"还留着旧版类案检索页的 {dead}"

    # 两段式：CSS 与 JS 必须认同一个类名。
    assert re.search(r"#pg-search\.run \.cs-only-land\{display:none\}", css), \
        "落地页那几块没有在 .run 下收起 —— 检索完英雄区还压着结果"
    assert re.search(r"#pg-search\.run \.cs-rw\{display:block\}", css), \
        "结果态没有在 .run 下显示出来"
    assert re.search(r"#pg-search \.cs-rw\{display:none\}", css), \
        "结果态默认没有藏起来 —— 一进页面就是一块空的命中列表"
    assert "classList.toggle('run'" in _js_function(js, "csSetRun"), \
        "csSetRun() 不再切 run 类 —— 与 CSS 那边对不上了"

    # 「新检索」必须连结果一起收掉：进页面时是按"结果区有没有东西"恢复形态的
    # （见 renderSearch），只切类名的话走开再回来又会被弹回结果页。
    reset = _js_function(js, "csReset")
    assert "$('#searchHits')" in reset and "csSetRun(false)" in reset, \
        "csReset() 没清结果区 —— 「新检索」按下去像没生效"

    # 场景卡：三张、下标对齐、点击同时做"填充 + 开跑"。
    # 切片用**代码**当锚点，别用注释 —— 上面刚把注释剥掉了（``_strip_js_comments``）。
    scenes = js[js.index("var CS_SCENES"):js.index("var CS_CAUSES")]
    assert scenes.count("brief:") == 3, "场景卡不是三张（参考图是三张）"
    assert "data-i=" in _js_function(js, "csPaintScenes"), \
        "场景卡没带下标 —— 点了不知道要点哪一张"
    click = _js_function(js, "renderSearch")
    assert "CS_SCENES[Number(card.getAttribute('data-i'))]" in click, \
        "场景卡的点击没按下标取内容"
    assert "input.value = s.q" in click and "doSearch(s.q)" in click, \
        "场景卡只填不跑（或只跑不填）—— 前者会被读成卡是坏的"

    # 范围词要真的进检索式，条数要真的进 top_k。选了发不出去 = 假功能。
    assert "csScopeWords()" in _js_function(js, "csQuery"), \
        "csQuery() 没把范围词拼进检索式 —— 面板里选了个寂寞"
    assert "top_k: csScope.topk" in _js_function(js, "doSearch"), \
        "检索条数没走「返回条数」面板的选择"
    for key in ("causes", "courts", "topk"):
        assert key in _js_function(js, "csPaintScope"), f"范围面板漏了 {key}"

    # 附件走的是合同页那条解析接口，而且**只填进输入框、不自动开跑**：
    # 一份合同里真正要检索的往往只有一段，直接拿全文搜召回质量更差。
    attach = _js_function(js, "csAttach")
    assert "'/contract/parse'" in attach, "附件没走 /contract/parse，提取不到正文"
    assert "doSearch(" not in attach, \
        "附件提取完自动开跑了 —— 用户还没来得及删掉无关段落"

    # 输入框只能有一个。哪天冒出第二个，「全局搜索回车填哪个」立刻就说不清。
    assert html.count('id="sInput"') == 1, "页面上出现了不止一个检索输入框"


def test_js_function_extractor_works() -> None:
    """抠函数源码的工具自检：只能抠出那一个，不能多也不能少。

    "抠不到"会被当成"没违规"（与 ``_inline_css`` 那份自检同一个道理）。
    """
    src = ("function ask(a) { if (a) { return 1; } }\n"
           "function askAgent(b) { return 2; }\n")
    got = _js_function(src, "ask")
    assert got.startswith("function ask(")
    assert "askAgent" not in got, "抠多了：把相邻函数也吞进来了"
    assert "return 2" not in got, "抠多了：把下一个函数也算进来了"
    assert "return 2" in _js_function(src, "askAgent"), "抠少了或抠错了函数"


def test_failure_paths_stop_the_spinner() -> None:
    """``error`` / ``onError`` 分支**也要**收掉转圈气泡。

    来历：三个入口原先只在 ``done`` 收转圈，失败时错误文案上方永远挂着一个
    转得正欢的圈。执行轨迹面板删掉之后，它是"界面还在动"的唯一证据 ——
    读起来像"还在生成"，比直接报错更误导。所以每个入口至少要有两处
    收转圈的调用（``error`` + ``onError``；成功路径那处另算）。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    for name in ("ask", "askAgent", "startReview"):
        body = _js_function(js, name)
        n = body.count("dropSpin(") + body.count("dropTyping(")
        assert n >= 2, (
            f"{name}() 里只有 {n} 处收转圈气泡 —— 失败分支漏了的话，"
            "错误文案上方会一直转圈，看起来像还在生成"
        )


# ------------------------------------------------ Agent 关键决策与执行摘要（约束 4）
def test_agent_decision_line_and_exec_summary_are_wired() -> None:
    """约束 4 的两层显示：过程中一句关键决策 + 结束时一行摘要。

    2026-10-07 加。**两层都不是"面板"**：等待期那三个点（``.tp3``）原地换成
    一句话，结束时那行赘在既有的元信息行/Object 上 —— 执行轨迹面板
    2026-09-29 已整块摘掉，多一块就等于把它请回来。

    三处坏法都不报错：
    1. 四个 ``onFrame`` 少接一个 → 那个入口**永远不显示决策**，
       看起来与"这次没裁决"一模一样；
    2. ``dropSpin`` 只收 ``.tp3`` → ``showDecision`` 是给同一个节点**换类名**，
       换过之后收不走，那句决策赖在气泡里、压在正文上方，像还在跑；
    3. 摘要没穿过 ``answerShape`` 适配层 → 助手页（走适配的那条链路）不显示，
       老 RAG 链路照旧显示，只在一条链路上消失，更像偶发渲染问题。
    """
    js = _strip_js_comments(APP_JS.read_text(encoding="utf-8"))
    css = _inline_css(INDEX)

    # ---- ① 四个流式入口都认 decision 帧（AI 助手 / askAgent / 合同审查 / 类案检索）
    n = len(re.findall(r"ev === 'decision'", js))
    assert n == 4, (
        f"接 decision 帧的入口有 {n} 个，应为 4 个 —— 少的那几个入口会静默地"
        "永远不显示关键决策，而界面上看不出任何异常"
    )

    # ---- ② 决策那句话落在等待位上，且 done 一到必须收得走
    show = _js_function(js, "showDecision")
    assert ".agent-note" in show and ".tp3" in show, "showDecision 没复用等待位（.tp3）"
    assert "textContent" in show, "决策那句话里带模型输出，不能用 innerHTML 碰 DOM"
    drop = _js_function(js, "dropSpin")
    assert ".agent-note" in drop, (
        "dropSpin 只收 .tp3 —— showDecision 是给同一个节点换类名，"
        "换过之后就不是 .tp3 了，那句决策会永远留在气泡里（看起来像还在跑）"
    )
    assert ".agent-note{" in css, "缺少 .agent-note 的样式（决策那句话会光秃秃地摊在气泡里）"

    # ---- ③ 一行执行摘要：助手页与结构化页各一处，且都要读后端的 summary
    assert "data.summary" in _js_function(js, "formatRagAnswer"), \
        "助手页没渲染执行摘要"
    assert "data.summary" in _js_function(js, "formatStructuredAnswer"), \
        "合同审查/结构化那页没渲染执行摘要"
    assert "summary" in _js_function(js, "answerShape"), (
        "适配层把 summary 丢了 —— 助手页那条链路会静默地不显示摘要"
        "（老 RAG 链路原样返回、照样显示，更像偶发问题）"
    )
    assert ".rag-meta{" in css, "缺少执行摘要的样式"

    # ---- ④ 类案检索页没有气泡：决策与步数都落在底部那行状态栏上
    search = _js_function(js, "doSearch")
    assert "ev === 'decision'" in search and "foot.textContent" in search, \
        "类案检索页没接决策帧（这页没有气泡，只能落在底部状态栏）"
    assert "used_nodes" in search, (
        "类案检索页的步数没读 used_nodes —— 别拿 r.steps 顶替："
        "回环会把同一节点跑第二遍，那时步数涨、节点数不涨"
    )
