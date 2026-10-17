/* ============================================================================
   衡律工作台 · 数据层（app.js）
   ----------------------------------------------------------------------------
   职责：把 index.html 这份界面接到后端真实接口。界面本身不改，只往里填数据。

   约定：
   - 令牌沿用登录页写入的 `sessionStorage.lp_token`；401 一律踢回登录页。
   - 接口全部走同源 `/api/v1`（经 `/ui/` 访问，无需 CORS）。
   - 会话走 `POST /chat/stream`（SSE：meta / trace / done / error），
     trace 用来显示"现在跑到哪个节点"，done 里带报告、引用、风险点等。

   三条刻意的做法：
   1. **界面上没有的东西不编。** 设计稿里的「本周开庭」「文书待审」「案件动态（事件流）」
      后端没有对应数据源，这里换成同位置的真实指标（待办任务 / 卷宗材料 / 最近更新案件），
      宁可换掉标签也不填示例数字。
   2. **每个区块独立容错。** 一个接口挂了只让那块显示原因，不让整页变成错误页。
   3. **渲染一律走 textContent / 转义。** 报告与案卷原文可能含任意字符，
      绝不用 innerHTML 直插后端文本（除自己拼的结构），避免 XSS。
   ========================================================================== */
(function () {
  'use strict';

  var API = '/api/v1';

  /* ------------------------------------------------------------ 基础工具 */
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined && html !== null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function toast(m, html) {
    var t = $('#toast');
    if (!t) return;
    // html=true 时按已构造的安全 HTML 渲染（用于含可点击链接的提示）；
    // 默认纯文本走 esc 转义，避免 XSS。
    t.innerHTML = html ? m : esc(m);
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove('show'); }, 2600);
  }
  function fmtTime(iso) {
    if (!iso) return '—';
    // 后端给的是本地时间字符串，**不要再经 new Date() 做时区换算**，
    // 否则东八区会整体早 8 小时。
    return String(iso).replace('T', ' ').slice(0, 16);
  }

  /* 全局兜底：未捕获的脚本异常用气泡提示出来，而不是页面无声白掉、
     看起来像"闪退"。会话失效（unauthorized）有专门处理，这里跳过避免重复弹。 */
  window.addEventListener('error', function (e) {
    toast('页面脚本异常：' + (e && e.message ? e.message : '未知错误'));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    if (r && (r.message === 'unauthorized' || String(r) === 'unauthorized')) return;
    toast('页面异常：' + (r && r.message ? r.message : String(r || '未知')));
  });
  function fmtMs(ms) {
    var v = Number(ms) || 0;
    if (v < 1000) return v + ' ms';
    if (v < 60000) return (v / 1000).toFixed(1) + ' s';
    return Math.floor(v / 60000) + ' 分 ' + Math.round((v % 60000) / 1000) + ' 秒';
  }
  function truncate(s, n) {
    var t = String(s === null || s === undefined ? '' : s);
    return t.length > n ? t.slice(0, n) + '…' : t;
  }
  /** 本地时间戳（**不能用 toISOString**：那是 UTC，东八区会差 8 小时）。 */
  function nowStamp() {
    var d = new Date();
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function icon(name, cls) {
    return '<svg class="ic ' + (cls || 'sm') + '"><use href="#i-' + name + '"/></svg>';
  }
  function tag(text, kind, withDot) {
    return '<span class="tag ' + (kind || 'ne') + '">' + (withDot === false ? '' : '<i></i>') + esc(text) + '</span>';
  }
  function pct(x, digits) {
    var v = Number(x);
    if (!isFinite(v)) return '—';
    return (v * 100).toFixed(digits === undefined ? 0 : digits) + '%';
  }

  /* 分帧/空闲调度。长报告的补插与收尾的次要请求都走这里 ——
     它们都不该和"此刻正在渲染的首屏"抢同一帧。 */
  var raf = window.requestAnimationFrame
    ? function (f) { return window.requestAnimationFrame(f); }
    : function (f) { return setTimeout(f, 16); };
  function idle(f) {
    if (window.requestIdleCallback) return window.requestIdleCallback(f, { timeout: 1500 });
    return setTimeout(f, 240);
  }

  /** 粘底滚动：只在用户本来就贴着底部时才跟随新内容 ——
   *  他往上翻记录时不能被反复拽回底部。
   *  同一帧里的多次调用合并成一次：每次读 ``scrollHeight`` 都会强制一次同步布局，
   *  气泡多、正文长的时候这是实打实的开销。 */
  function stick(box, force) {
    if (!box) return;
    if (force) box._force = 1;
    if (box._stick) return;
    box._stick = raf(function () {
      box._stick = 0;
      var f = box._force;
      box._force = 0;
      if (f || box.scrollHeight - box.scrollTop - box.clientHeight < 200) {
        box.scrollTop = box.scrollHeight;
      }
    });
  }

  function tok() { try { return sessionStorage.getItem('lp_token') || ''; } catch (e) { return ''; } }
  function me() { try { return JSON.parse(sessionStorage.getItem('lp_user') || '{}'); } catch (e) { return {}; } }
  function kick() {
    try { sessionStorage.removeItem('lp_token'); sessionStorage.removeItem('lp_user'); } catch (e) { /* 忽略 */ }
    location.replace('login.html?next=' + encodeURIComponent('index.html'));
  }

  /** 带鉴权的请求；401/403 视为会话失效，直接回登录页。 */
  function req(path, opt) {
    opt = opt || {};
    var headers = opt.headers || {};
    var t = tok();
    if (t) headers.Authorization = 'Bearer ' + t;
    if (opt.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(opt.json);
    }
    var url = API + path;
    if (opt.query) {
      var qs = Object.keys(opt.query)
        .filter(function (k) { return opt.query[k] !== '' && opt.query[k] !== undefined && opt.query[k] !== null; })
        .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(opt.query[k]); })
        .join('&');
      if (qs) url += '?' + qs;
    }
    // 全局请求超时兜底：监控中心的整刷/半刷都是 Promise.all，任一接口挂起
    // 就会把整轮加载卡死成永久「读取中…」。默认 20s 无响应直接放弃，让
    // 调用方走失败分支自行渲染/重试。
    var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var to = ac ? setTimeout(function () { ac.abort(); }, opt.timeout || 20000) : null;
    return fetch(url, {
      method: opt.method || 'GET', headers: headers, body: opt.body, cache: 'no-store',
      signal: ac ? ac.signal : undefined,
    }).then(function (r) {
      if (to) { clearTimeout(to); to = null; }
      if (r.status === 401) {
        // 只有 401 才是"会话失效"；403 是"没权限看这条资源"，踢下线会误伤。
        if (!t) kick();
        else r.clone().json().catch(function () { return {}; }).then(function (b) {
          toast((b && b.detail) || '登录状态已失效，正在返回登录页…');
          setTimeout(kick, 700);
        });
        throw new Error('unauthorized');
      }
      // 滑动续期：后端发现令牌快过期时会在响应头里带一张新令牌，静默换上，
      // 用户无感知，页面不会被 12 小时到期突然踢回登录页。
      var renewed = r.headers.get('X-Renewed-Token');
      if (renewed) { try { sessionStorage.setItem('lp_token', renewed); } catch (e) { /* 忽略 */ } }
      return r;
    }, function (e) {
      if (to) { clearTimeout(to); to = null; }
      throw e;
    });
  }
  function J(path, opt) {
    return req(path, opt).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (txt) {
          var d = '';
          try { d = JSON.parse(txt).detail || ''; } catch (e) { d = ''; }
          // FastAPI 对**不存在的路由**回的就是 `{"detail":"Not Found"}`（英文、且是
          // 框架默认串），而本项目的业务 404 一律带中文 detail（见 api/v1/cases.py 的
          // `_guard`）。所以这个组合能**准确地**翻译成"接口不存在"。
          //
          // 最常见的成因是**后端没重启**：前端已经发了新请求，而进程还是旧代码。
          // 这条在这个项目里反复出现（微信接口 503、模板目录、材料查看器…），
          // 每次都要从头查一遍"为什么 404"。直接把话说明白能省掉一整轮。
          if (r.status === 404 && d === 'Not Found') {
            throw new Error('接口 ' + path + ' 不存在 —— 后端可能尚未重启');
          }
          throw new Error(d || ('HTTP ' + r.status));
        });
      }
      return r.json();
    });
  }
  function fail(box, err, what) {
    if (box) box.innerHTML = '<div class="banner" style="border-color:var(--dg);color:var(--dg)">'
      + esc(what || '读取失败') + '：' + esc(err && err.message ? err.message : String(err)) + '</div>';
  }

  /* ------------------------------------------------------------ 弹窗
     设计稿里没有弹层，但"看案件详情 / 读草稿全文 / 新建案件"必须有地方放。
     样式沿用同一套令牌（扁平、1px 描边、同样圆角），不引入第二种视觉。 */
  var modalRoot = null;
  function ensureModal() {
    if (modalRoot) return modalRoot;
    var style = el('style');
    style.textContent = [
      '.lp-modal{position:fixed;inset:0;background:rgba(0,0,0,.34);display:flex;align-items:center;',
      'justify-content:center;padding:24px;z-index:90}',
      '.lp-modal[hidden]{display:none}',
      '.lp-modal .box{background:var(--bg);color:var(--tp);border:1px solid var(--br);border-radius:var(--r12);',
      'width:min(720px,100%);max-height:86vh;display:flex;flex-direction:column}',
      '.lp-modal .box.wide{width:min(920px,100%)}',
      '.lp-modal .head{display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--br);font-weight:600}',
      '.lp-modal .head button{margin-left:auto}',
      '.lp-modal .body{padding:16px;overflow:auto;font-size:13.5px;line-height:1.8}',
      '.lp-modal .foot{padding:12px 16px;border-top:1px solid var(--br);display:flex;gap:8px;justify-content:flex-end}',
      '.lp-modal pre,.lp-pre{white-space:pre-wrap;word-break:break-word;font-family:var(--sans);margin:8px 0;',
      'background:var(--mu);border:1px solid var(--br);border-radius:var(--r8);padding:10px 12px;font-size:12.5px;line-height:1.8}',
      '.lp-modal .kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin-bottom:12px}',
      '.lp-modal .kv dt{font-size:12.5px;color:var(--tt)}',
      '.lp-modal .kv dd{margin:0}',
      /* 导入技能（截图样式）：虚线拖拽上传区 + 复选框 + 文件要求列表 + ZIP 快捷链接 */
      '.sk-import-drop{border:1.5px dashed var(--br);border-radius:var(--r10);padding:26px 16px;text-align:center;',
      'cursor:pointer;color:var(--tt);transition:border-color .15s,background .15s}',
      '.sk-import-drop:hover,.sk-import-drop.over{border-color:var(--tq);background:rgba(79,110,247,0.05)}',
      '.sk-import-drop .ic{width:34px;height:34px;color:var(--ts);margin:0 auto}',
      '.sk-import-drop .tx{margin-top:8px;font-size:13px;color:var(--ts)}',
      '.sk-import-auto{display:flex;align-items:center;gap:8px;margin:14px 0 2px;font-size:13px;cursor:pointer}',
      '.sk-import-auto input{accent-color:var(--tq)}',
      '.sk-import-req{margin:12px 0 2px;font-weight:600;font-size:13px}',
      '.sk-import-req-list{margin:4px 0 0;padding-left:18px;font-size:12.5px;color:var(--tt);line-height:1.75}',
      '.sk-import-req-list li{margin:2px 0}',
      '.sk-import-zip{display:inline-block;margin-top:10px;font-size:13px;color:var(--tq);text-decoration:underline;',
      'cursor:pointer;user-select:none}',
      '.lp-field{margin-bottom:10px}',
      '.lp-field label{display:block;font-size:12.5px;color:var(--tt);margin-bottom:5px}',
      '.lp-field input,.lp-field textarea,.lp-field select{width:100%;border:1px solid var(--br);border-radius:var(--r8);',
      'padding:8px 11px;font-size:13.5px;background:var(--bg);color:var(--tp);outline:0;font-family:inherit}',
      '.lp-field textarea{min-height:96px;resize:vertical}',
      '.lp-modal .muted{color:var(--tt)}',
      /* 文书预览：看起来要像一份文稿，而不是一段日志 —— 这是"能不能直接交给律师审"的门槛 */
      '.doc-preview{background:var(--bg);border:1px solid var(--br);border-radius:var(--r10);',
      'padding:24px 28px;font-size:14px;line-height:1.95;max-height:560px;overflow:auto}',
      '.doc-preview h1{font-size:20px;font-weight:600;text-align:center;margin:0 0 6px}',
      '.doc-preview h2{font-size:15px;font-weight:400;color:var(--ts);text-align:center;margin:0 0 16px}',
      '.doc-preview h3{font-size:14px;font-weight:600;margin:18px 0 6px}',
      '.doc-preview p{margin:0 0 8px}',
      '.doc-preview ul{margin:0 0 10px 20px}',
      '.doc-preview li{margin:2px 0}',
      '.doc-preview pre{white-space:pre-wrap;word-break:break-word;background:var(--mu);',
      'border:1px solid var(--br);border-radius:var(--r8);padding:10px 12px;font-size:12.5px}',
      '.doc-preview code{background:var(--mu);border-radius:4px;padding:0 4px;font-size:12.5px}',
      '.doc-preview blockquote,.doc-preview .note{color:var(--tt);font-size:12.5px;border-left:3px solid var(--br);padding-left:10px}',
      // ``.doc-editor`` 随文书中心的旧内联结果区一起删了：那是 ``#docEditor``
      // 那个 textarea 的样式，编辑页改用 ``contenteditable`` + ``.de-doc``。
      // ``.doc-preview`` / ``.doc-field`` 留着 —— 合同审查页的 ``#contractResult``
      // 和 mdLite 还在用它们。
      '.doc-preview .doc-field{margin:14px 0 4px;font-size:13.5px}',
      /* 会话气泡里也可能出现标题（报告正文），别让 h1 用浏览器默认的巨号 */
      '.bb h1,.bb h2,.bb h3{font-size:14.5px;font-weight:600;margin:10px 0 4px}',
      '.bb h1{text-align:left}',
      /* 长报告渲染性能：一次审查的输出上万字、上百个块，屏外块本来也要走一遍
         样式计算 + 布局 + 绘制。content-visibility:auto 让浏览器跳过屏外块的这些工作，
         contain-intrinsic-size 给出占位高度（auto 关键字会记住实测高度，滚动条不会跳）。 */
      '.bb>p,.bb>ul,.bb>pre,.bb>h2,.bb>h3,.bb>.cite,',
      '.ans>p,.ans>ul,.ans>h2,.ans>h3,.ans>.cite,',
      '.doc-preview>p,.doc-preview>ul,.doc-preview>h1,.doc-preview>h3,',
      '.doc-preview>.doc-field{content-visibility:auto;contain-intrinsic-size:auto 30px}',
      '.ans>pre,.doc-preview>pre{content-visibility:auto;contain-intrinsic-size:auto 280px}',
      /* 修订版区块：风险分级 + 就地修订 + 导出 Word/PDF。
         合同审查的交付物最终是"改完的合同"，所以这一块必须能直接拿到文件。 */
      '.rev{margin-top:14px;border:1px solid var(--br);border-radius:var(--r10);padding:12px 14px;background:var(--bg)}',
      '.rev-h{display:flex;align-items:center;gap:10px;font-size:13.5px;font-weight:600;flex-wrap:wrap}',
      '.rev-h .lv{margin-left:auto;display:flex;gap:6px;font-weight:400;font-size:12px}',
      '.rev-h .lv span{border:1px solid var(--br);border-radius:6px;padding:1px 7px;color:var(--tt)}',
      '.rev-h .lv b{font-variant-numeric:tabular-nums}',
      '.rev .ops{margin-top:10px}',
      '.rev-chg{border-top:1px solid var(--br);margin-top:10px;padding-top:8px}',
      '.rev-chg .row{display:flex;gap:8px;font-size:12.5px;padding:5px 0;align-items:baseline}',
      '.rev-chg .row .no{flex:none;font-weight:600;white-space:nowrap}',
      '.rev-chg .row .tx{flex:1;min-width:0;color:var(--ts)}',
      '.rev-chg .row.un .tx{color:var(--wn)}',
      '.rev-hint{font-size:12.5px;color:var(--tt);margin-top:6px}',
      // 改后合同 / 对照稿全文的预览。后端交付正文带版式标记（`# 标题`、`### 章节`、
      // `<p align="left|right">`），mdLite 渲染出的 h1~h3 默认是浏览器样式
      // （很小、贴边），跟"一份合同长什么样"对不上。这里给一套像合同的排版。
      '.rev-body{margin-top:8px;font-size:13px;line-height:1.9;color:var(--ts)}',
      '.rev-body h1{font-size:17px;font-weight:700;text-align:center;margin:4px 0 12px}',
      '.rev-body h2{font-size:15px;font-weight:700;text-align:center;margin:10px 0 8px}',
      '.rev-body h3{font-size:14px;font-weight:700;margin:12px 0 6px}',
      '.rev-body p{margin:4px 0}',
      // 条款正文首行缩进两字（与导出 Word/PDF 的 firstLineIndent 一致）。
      // ⚠ 落款那几行走的是 `.doc-left`，必须**排除掉** —— 否则「甲方：」会被推进去两格，
      //   预览里和原件不一样、和导出的也不一样。
      '.rev-body p:not([class]){text-indent:2em}',
      '.rev-body .doc-left{text-indent:0}',
      /* 证据链 */
      '.evchain{margin-top:18px;border-top:1px solid var(--br);padding-top:14px}',
      '.evchain-h{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13.5px;margin-bottom:8px}',
      '.evchain-box{font-size:13px;line-height:1.7}',
      '.evc{border:1px solid var(--br);border-radius:10px;padding:10px 12px;margin-bottom:10px;background:var(--bg)}',
      '.evc-claim{font-weight:600;display:flex;gap:6px;align-items:center}',
      '.evc-ev{margin:6px 0 6px 2px;padding-left:18px}',
      '.evc-gap{font-size:12.5px;color:var(--wn);background:rgba(0,0,0,0.03);border-radius:6px;padding:5px 8px;margin-top:4px}',
      /* 案件详情 · 证据材料 */
      '.mat-box{font-size:13px}',
      '.mat-item{display:flex;align-items:center;gap:8px;border:1px solid var(--br);border-radius:8px;padding:7px 10px;margin-bottom:6px;background:var(--bg)}',
      '.mat-item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}',
      '.mat-item .meta{flex:none;font-size:12px;color:var(--tt)}',
      '.mat-empty{font-size:12.5px;color:var(--tt)}',
      /* 材料行可点：整行点开正文查看器。`解析` 按钮仍走它自己的监听器
         （委托里先判 `[data-mat-parse]`），两者不冲突。 */
      '.mat-item[data-mat-view]{cursor:pointer}',
      '.mat-item[data-mat-view]:hover{border-color:var(--tq)}',
      /* 材料正文查看器。`pre-wrap` 而不是 `pre`：材料原文里一行很长，
         横向滚动条比换行难读得多。字体走正文字体（不是等宽）——
         这是给人读的中文材料，不是代码。 */
      '.mat-view-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;font-size:12px}',
      '.mat-view-n{font-size:12.5px;color:var(--wn);margin:0 0 10px;line-height:1.7}',
      '.mat-view-t{white-space:pre-wrap;word-break:break-word;font-family:var(--sans);'
        + 'font-size:13.5px;line-height:1.85;margin:0;background:var(--mu);'
        + 'border:1px solid var(--br);border-radius:8px;padding:12px 14px}',
      /* 证据充分度评估 */
      '.as-tier{display:inline-block;font-size:14px;font-weight:700;padding:4px 12px;border-radius:999px;margin-bottom:8px}',
      '.as-reason{font-size:12.5px;color:var(--ts);margin:0 0 10px;line-height:1.6}',
      '.as-metrics{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}',
      '.as-m{flex:none;text-align:center;min-width:58px;background:var(--bg);border:1px solid var(--br);border-radius:8px;padding:6px 4px}',
      '.as-mv{font-size:17px;font-weight:700;line-height:1.1}',
      '.as-ml{font-size:11px;color:var(--tt);margin-top:2px}',
      '.as-gap{font-size:12.5px;line-height:1.6;border-radius:8px;padding:8px 10px;margin-bottom:8px}',
      '.as-gap.fatal{background:rgba(220,38,38,0.06);border:1px solid rgba(220,38,38,0.25)}',
      '.as-gap.fix{background:rgba(217,119,6,0.06);border:1px solid rgba(217,119,6,0.25)}',
      '.as-gap-h{font-weight:600;margin-bottom:4px}',
      '.as-gap-it{margin:3px 0}',
      '.as-sugg ul{margin:4px 0 0;padding-left:18px}',
      '.as-sugg li{margin:3px 0}',
      '.as-disc{font-size:11.5px;color:var(--tt);margin-top:10px;border-top:1px dashed var(--br);padding-top:8px}',
      /* 胜诉把握（复用上面 .as-* 那一套，这里只补大号数字与分层小标题） */
      '.win-head{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}',
      '.win-num{font-size:34px;font-weight:700;line-height:1.05}',
      '.win-num .u{font-size:16px;font-weight:600;margin-left:1px}',
      '.win-cap{font-size:12px;color:var(--tt);margin:0 0 10px;line-height:1.6}',
      '.win-sub{font-weight:600;font-size:13px;margin:12px 0 6px;padding-top:10px;border-top:1px dashed var(--br)}',
      '.win-model{font-size:12.5px;line-height:1.7}',
      '.win-stale{font-size:12px;color:var(--wn);background:rgba(217,119,6,0.08);border:1px solid rgba(217,119,6,0.25);border-radius:8px;padding:6px 9px;margin-bottom:8px}',
      /* 案件详情 · 阶段推进条 */
      '.stage-bar{display:flex;align-items:center;gap:10px;margin-top:12px}',
      '.stage-bar .hint{font-size:12px;color:var(--tt)}',
    ].join('');
    document.head.appendChild(style);
    modalRoot = el('div', 'lp-modal');
    modalRoot.hidden = true;
    modalRoot.addEventListener('click', function (e) { if (e.target === modalRoot) closeModal(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });
    document.body.appendChild(modalRoot);
    return modalRoot;
  }
  function closeModal() { if (modalRoot) { modalRoot.hidden = true; modalRoot.innerHTML = ''; } }
  /** 打开弹窗：opts: {title, closeText, body:Node|string, footer:[Node], wide:bool} */
  function openModal(opts) {
    var root = ensureModal();
    var box = el('div', 'box' + (opts.wide ? ' wide' : ''));
    var head = el('div', 'head');
    head.appendChild(el('span', null, esc(opts.title || '')));
    var close = el('button', 'btn sm', opts.closeText || '关闭');
    close.addEventListener('click', closeModal);
    head.appendChild(close);
    var body = el('div', 'body');
    if (typeof opts.body === 'string') body.innerHTML = opts.body;
    else if (opts.body) body.appendChild(opts.body);
    box.appendChild(head);
    box.appendChild(body);
    if (opts.footer && opts.footer.length) {
      var foot = el('div', 'foot');
      opts.footer.forEach(function (b) { foot.appendChild(b); });
      box.appendChild(foot);
    }
    root.innerHTML = '';
    root.appendChild(box);
    root.hidden = false;
    return { close: closeModal, body: body };
  }
  function btn(text, kind, onClick) {
    var b = el('button', 'btn' + (kind ? ' ' + kind : ''), esc(text));
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast('已复制'); },
        function () { toast('当前环境不支持自动复制，请手动选择'); });
    } else { toast('当前环境不支持自动复制，请手动选择'); }
  }

  /* ------------------------------------------------------------ 页面切换 */
  function switchPage(id) {
    $$('.pg').forEach(function (p) { p.classList.toggle('on', p.id === id); });
    $$('.nav').forEach(function (n) { n.classList.toggle('on', n.getAttribute('data-pg') === id); });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    renderPage(id);
  }
  var rendered = {};
  function renderPage(id) {
    var fn = RENDER[id];
    if (!fn || rendered[id]) return;
    rendered[id] = 1;
    try { fn(); } catch (e) { console.error('[app] 渲染失败 ' + id, e); }
  }
  function reloadPage(id) { rendered[id] = 0; renderPage(id); }

  /* ------------------------------------------------------------ 顶栏 / 侧栏 */
  function paintChrome(facts) {
    var u = me();
    var name = u.full_name || u.username || '律师';
    var d = new Date();
    var g = new Date().getHours();
    var word = g < 6 ? '夜深了' : (g < 12 ? '早上好' : (g < 14 ? '中午好' : (g < 18 ? '下午好' : '晚上好')));
    var wk = ['日', '一', '二', '三', '四', '五', '六'];
    var greetEl = $('#greet');
    if (greetEl) greetEl.textContent = word + '，' + name;
    var dateEl = $('#dateLine');
    if (dateEl) {
      var line = d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日 星期' + wk[d.getDay()];
      if (facts) line += ' · ' + facts;
      dateEl.textContent = line;
    }
    var ava = $('.tr .ava');
    if (ava && name) ava.textContent = name.slice(0, 1);
  }

  function paintFoot(health) {
    var sft = $('#sft');
    if (!sft) return;
    var status = health ? (health.status || 'unknown') : 'unknown';
    var text = { ok: '各子系统就绪', degraded: '部分子系统降级', error: '存在问题' }[status] || '状态未知';
    var color = status === 'error' ? 'var(--dg)' : (status === 'degraded' ? 'var(--wn)' : 'var(--ps)');
    sft.innerHTML = '<div style="display:flex;gap:6px;align-items:center;color:var(--tt)">'
      + icon('shield', 'sm') + '案卷空间已加密</div>'
      + '<div class="bar"><i style="width:' + (status === 'ok' ? 100 : status === 'degraded' ? 60 : 25)
      + '%;background:' + color + '"></i></div>'
      + esc(text + ' · v' + (health && health.version ? health.version : '—'));
  }

  /** 侧栏徽标：只用真实计数，取不到就不显示。 */
  function paintBadges(data) {
    var navs = $$('.side .nav');
    if (data.cases !== null && data.cases !== undefined) {
      var n = navs.filter(function (x) { return x.getAttribute('data-pg') === 'pg-case'; })[0];
      if (n) {
        var b = $('.bdg', n);
        if (!b) { b = el('span', 'bdg'); n.appendChild(b); }
        if (data.cases > 0) b.textContent = data.cases; else b.remove();
      }
    }
    // 「AI 助手」上原本挂着一个假的未读数（3）—— 后端没有未读会话这个概念，去掉。
    var ai = navs.filter(function (x) { return x.getAttribute('data-pg') === 'pg-ai'; })[0];
    if (ai) { var ab = $('.bdg', ai); if (ab) ab.remove(); }
    if (data.openTasks !== null && data.openTasks !== undefined) {
      var c = navs.filter(function (x) { return x.getAttribute('data-pg') === 'pg-cal'; })[0];
      if (c) {
        var cb = $('.bdg', c);
        if (!cb) { cb = el('span', 'bdg'); c.appendChild(cb); }
        if (data.openTasks > 0) cb.textContent = data.openTasks; else cb.remove();
      }
    }
  }

  /* ------------------------------------------------------------ 工作台 */
  function renderDash() {
    Promise.all([
      J('/cases/stats/summary').catch(function () { return null; }),
      J('/tasks/stats').catch(function () { return null; }),
      J('/tasks', { query: { limit: 8 } }).catch(function () { return null; }),
      J('/cases', { query: { limit: 6 } }).catch(function () { return null; }),
      J('/health').catch(function () { return null; }),
      J('/drafts', { query: { limit: 1 } }).catch(function () { return null; }),
    ]).then(function (r) {
      var caseStats = r[0], taskStats = r[1], tasks = r[2], cases = r[3], health = r[4], drafts = r[5];
      var items = (tasks && tasks.items) || [];
      var open = items.filter(function (t) { return t.status === 'pending' || t.status === 'running'; });
      var openCount = taskStats && taskStats.by_status
        ? (Number(taskStats.by_status.pending || 0) + Number(taskStats.by_status.running || 0))
        : open.length;
      var caseItems = (cases && cases.items) || [];
      var caseCount = caseStats ? Number(caseStats.cases || 0) : caseItems.length;

      // 顶栏摘要：用累计指标，不再显示「在办/待办」这两个恒为 0 的数字
      var facts = '累计分析 ' + (taskStats ? (taskStats.total || 0) : '—') + ' 次'
        + ' · 平均置信度 ' + (taskStats ? pct(taskStats.avg_confidence) : '—')
        + ' · 引用拦截 '
        + (taskStats && taskStats.citation_blocked !== undefined ? taskStats.citation_blocked : '—') + ' 次';
      // 「更新于 HH:MM」+ 刷新按钮（首次绑定，防止重复挂监听）
      var upd = $('#updAt');
      if (upd) {
        var n = new Date();
        upd.textContent = '更新于 ' + String(n.getHours()).padStart(2, '0') + ':' + String(n.getMinutes()).padStart(2, '0');
      }
      var rf = $('#dashRefresh');
      if (rf && !rf._bound) {
        rf._bound = true;
        rf.addEventListener('click', function () { renderDash(); });
      }

      paintChrome(facts);
      paintFoot(health);
      paintBadges({ cases: caseCount, openTasks: openCount });

      /* 统计卡：设计稿的「本周开庭 / 文书待审」后端没有数据源，换成真实指标 */
      var stats = $('#dashStats');
      if (stats) {
        /* 四张卡对齐设计稿：在办案件 / 开庭审理阶段 / 待复核任务 / 文书草稿 */
        var bs = taskStats && taskStats.by_status ? taskStats.by_status : null;
        var degradedN = bs ? Number(bs.degraded || 0) : null;
        var failedN = bs ? Number(bs.failed || 0) : null;
        var reviewN = (degradedN !== null && failedN !== null) ? degradedN + failedN : '—';
        // total 可能是 null（接口缺 total 字段时）——按数字解析，解析不出来再退本页条数
        var rawDraftTotal = drafts ? Number(drafts.total) : NaN;
        var draftN = isFinite(rawDraftTotal) ? rawDraftTotal
          : (drafts ? (drafts.items || []).length : '—');
        // 开庭审理阶段：按案件 stage=hearing 的真实计数
        var hearN = caseStats && caseStats.by_stage ? Number(caseStats.by_stage.hearing || 0) : 0;
        var cards = [];
        cards.push('<div class="stat" data-go="pg-case"><div class="lb">在办案件</div><div class="vl">'
          + caseCount + '</div><div class="tr">在办 ' + caseCount + ' 件 · 材料 '
          + (caseStats && caseStats.materials !== undefined ? caseStats.materials : '—') + ' 份</div></div>');
        cards.push('<div class="stat" data-go="pg-case"><div class="lb">开庭审理阶段</div><div class="vl">'
          + hearN + '</div><div class="tr">开庭审理阶段 ' + hearN + ' 件</div></div>');
        cards.push('<div class="stat" data-go="pg-cal"><div class="lb">待复核任务</div><div class="vl">'
          + reviewN + '</div><div class="tr">降级 ' + (degradedN !== null ? degradedN : '—')
          + ' · 失败 ' + (failedN !== null ? failedN : '—') + '</div></div>');
        cards.push('<div class="stat" data-go="pg-doc"><div class="lb">文书草稿</div><div class="vl">'
          + draftN + '</div><div class="tr">草稿箱共 ' + draftN + ' 份</div></div>');
        stats.innerHTML = cards.join('');
      }
      var todoN = $('#todoN');
      if (todoN) todoN.textContent = openCount;

      /* 待办任务 */
      var list = $('#todoList');
      if (list) {
        var show = open.length ? open : items.slice(0, 3);
        if (!show.length) {
          list.innerHTML = '<div class="tm" style="padding:10px 0;color:var(--tt)">当前没有待处理的任务。在「AI 助手」发起一次分析后会出现在这里。</div>';
        } else {
          // 没有进行中的任务时，退化成"最近执行记录"——必须在标题行说清楚，
          // 否则律师会以为这 3 条是待办。
          var hint = open.length ? '' : '<div class="tm" style="padding:2px 0 6px;color:var(--tt)">'
            + '当前没有待处理的任务，下面是最近的执行记录：</div>';
          list.innerHTML = hint + show.map(function (t) {
            var done = t.status === 'succeeded' || t.status === 'degraded';
            var running = t.status === 'pending' || t.status === 'running';
            var color = t.status === 'failed' ? 'var(--dg)' : (running ? 'var(--wn)' : 'var(--ps)');
            var desc = [INTENT_LABEL[t.intent] || t.intent || '自动判定', fmtTime(t.created_at), fmtMs(t.elapsed_ms)]
              .filter(Boolean).join(' · ');
            return '<div class="task' + (done ? ' done' : '') + '" data-task="' + esc(t.task_id) + '">'
              + '<span class="pri" style="background:' + color + '"></span>'
              + '<span class="box">' + icon('check', 'xs') + '</span>'
              + '<span style="flex:1;min-width:0"><div class="tt">' + esc(truncate(t.query || '（无请求）', 40)) + '</div>'
              + '<div class="tm">' + esc(desc) + '</div></span>'
              + STATUS_TAG(t.status) + '</div>';
          }).join('');
        }
      }

      /* 案件动态：真实数据是"最近更新的案件"（后端没有跨案事件流） */
      var act = $('#dashActivity');
      if (act) {
        var rows = caseItems.length
          ? caseItems.map(function (c) {
            return '<div class="it" data-case="' + esc(c.case_id) + '" style="cursor:pointer">'
              + '<span class="ic">' + icon('folder', 'sm') + '</span>'
              + '<span class="tx"><b>' + esc(truncate(c.title || c.case_id, 28)) + '</b>'
              + '<span class="mt">，' + esc([TYPE_LABEL[c.case_type] || c.case_type,
                STAGE_LABEL[c.stage] || c.stage, c.cause_of_action || '未填案由'].filter(Boolean).join(' · ')) + '</span></span>'
              + '<span class="tm">' + esc(fmtTime(c.updated_at)) + '</span></div>';
          }).join('')
          : '<div class="tm" style="padding:10px 0;color:var(--tt)">还没有案件。到「案件管理」新建后会在这里看到最近进展。</div>';
        // 只替换 .ch 之外的内容，保住卡片标题那一行
        $$('#dashActivity > *').forEach(function (n) { if (!n.classList.contains('ch')) n.remove(); });
        $$('#dashActivity > .ch').forEach(function (n) { /* 保留 */ });
        var holder = el('div');
        holder.innerHTML = rows;
        while (holder.firstChild) act.appendChild(holder.firstChild);
      }

      /* AI 能力状态：读 /health 的后端矩阵（字段名是 backends） */
      var tips = $('#dashTips'), live = $('#dashLive');
      var bk = (health && health.backends) || {};
      var wanted = [
        ['大模型', 'llm', '未启用时由确定性规则引擎兜底，风险维度会明显变少'],
        ['多模态识别', 'vlm', '未启用时合同扫描件 / 图片材料无法识别'],
        ['向量检索', 'vector_store', '未启用时语义召回退回本地 numpy 检索'],
        ['关系库', 'relational_db', '未启用时案件、任务、草稿都无法持久化'],
      ].filter(function (x) { return bk[x[1]]; });
      if (tips) {
        tips.innerHTML = wanted.length
          ? wanted.map(function (x, i) {
            var v = bk[x[1]];
            var t = v.target ? ' title="' + esc(v.target) + '"' : '';
            return '<li' + t + '><span class="n">' + (i + 1) + '</span><span><b>' + esc(x[0]) + '</b>'
              + (v.enabled ? '已启用。' : esc(x[2]) + '。') + '</span></li>';
          }).join('')
          : '<li><span class="n">!</span><span>健康接口暂时不可用，无法读取能力状态。</span></li>';
      }
      if (live) {
        var on = wanted.filter(function (x) { return bk[x[1]].enabled; }).length;
        live.innerHTML = '<i></i>' + (wanted.length ? '已启用 ' + on + '/' + wanted.length : '未知');
        live.style.color = (wanted.length && on < wanted.length) ? 'var(--wn)' : 'var(--ps)';
      }
    });
  }

  /* ------------------------------------------------------------ AI 助手 */
  /** ``thread`` 是追问用的最近两轮对话，**只喂给后端改写检索式与消解指代**，
   *  不作为证据（后端 ``rag_answer.resolve_followup`` 有同样的注释）。
   *  留两轮是刻意的：再多，Prompt 会被历史挤满，参考资料反而被模型忽略。
   *
   *  ``conversationId`` 是当前这段会话的号（后端落库后回传）。
   *  空串 = 还没提问 / 刚点了「新建」，下一句会开一段新会话。
   *  **它只在 ``done`` 里回填、在「新建」与删掉当前会话时清空**，
   *  ``finish()`` 不动它 —— 用户还要在同一段会话里追问。 */
  var chat = { running: false, lastTaskId: '', thread: [], conversationId: '', expertId: '', expertName: '' };

  /** 把拼好的 HTML **分帧**插入容器。
   *
   *  为什么要这个：``done`` 事件一次带来整篇报告（上万字、上百个块）。一次性
   *  ``innerHTML`` 会把主线程压在「HTML 解析 → 样式计算 → 布局」上，
   *  界面上就是几百毫秒的白屏与掉帧，正文越长越明显。
   *  分帧插入后第一帧就能看到摘要与风险点，剩下的在后续帧补齐；
   *  每帧最多占 8ms，滚动与输入全程不卡。
   *
   *  **只在顶层边界断开**：``insertAdjacentHTML`` 每一批都是独立解析的，
   *  切在 ``<ul>`` 中间会让浏览器先把前一批自动闭合、后一批的 ``</li>`` 变成
   *  游离标签 —— 结构直接散掉。所以按标签深度累计，回到 0 才允许断点。
   *  万一深度判断出错（遇到了没预料到的写法），深度不会归零，结果是"整篇一次插入"，
   *  也就是退化成改动前的行为，不会渲染出坏结构。 */
  var VOID_TAG = /^<(br|hr|img|input|link|meta|area|base|col|embed|source|track|wbr)\b/i;
  function depthOf(tk) {
    if (tk.charAt(0) !== '<') return 0;                              // 文本段
    if (tk.charAt(1) === '/') return -1;                            // 闭合标签
    if (tk.charAt(1) === '!' || tk.charAt(1) === '?') return 0;      // 注释 / 声明
    if (/\/>$/.test(tk)) return 0;                                  // 自闭合，如 <use …/>
    return VOID_TAG.test(tk) ? 0 : 1;
  }
  function paintChunks(host, html, onDone) {
    // 文本里的 < > 早已被 esc() 转义，所以按 < > 切成「标签」与「文本段」是安全的。
    var tokens = String(html === null || html === undefined ? '' : html).match(/<[^>]+>|[^<]+/g) || [];
    host.innerHTML = '';
    if (!tokens.length) { if (onDone) onDone(); return; }
    var i = 0;
    (function step() {
      var t0 = Date.now(), buf = '', depth = 0;
      while (i < tokens.length) {
        var tk = tokens[i];
        buf += tk;
        i++;
        depth += depthOf(tk);
        if (depth < 0) depth = 0;
        // 断点条件按**字节数**而不是耗时：拼接字符串本身是亚毫秒级的，
        // 真正花钱的是浏览器对这一批的解析与布局 —— 它发生在插入之后，
        // 用计时器量不到。按大小切才能保证每帧的解析量有上界。
        if (depth === 0 && (buf.length >= 16000 || Date.now() - t0 >= 12)) break;
      }
      if (buf) host.insertAdjacentHTML('beforeend', buf);
      if (i < tokens.length) raf(step);
      else if (onDone) onDone();
    })();
  }

  /** 往某个对话容器里追加一条消息。
   *
   *  ``host`` 是宿主元素。**必须传**：文书编辑页左列也是同一种气泡，
   *  之前这里把 ``#chatbox`` 写死了，两个页面会互相往对方框里塞消息。
   *  默认值留给 AI 助手页的既有调用点（它们不传第三个参数）。
   */
  function bubble(html, mine, host) {
    var box = host || $('#chatbox');
    if (!box) return null;
    var d = el('div', 'msg' + (mine ? ' me' : ''));
    if (mine) {
      var ub = el('div', 'ub');
      ub.textContent = html;                 // 用户输入一律纯文本
      d.appendChild(ub);
    } else {
      var av = el('span', 'bav', icon('scale', 'sm'));
      var bb = el('div', 'bb');
      bb.innerHTML = html;
      d.appendChild(av);
      d.appendChild(bb);
    }
    box.appendChild(d);
    // 自己发的话一定要滚到可见；对方的长回答由 renderAnswer 决定（粘底才跟）
    stick(box, true);
    return d;
  }

  /** 极简 Markdown → HTML：先转义，再做标题/加粗/列表/表格块。
   *  只支持报告里实际会出现的几种写法，不做完整解析（避免引入一堆边界问题）。
   *
   *  ``ctx`` 是**可选**的增强开关，默认全关 —— 这个方法同时被合同审查页、
   *  文书起草、草稿预览等 6 处调用（``:836/:912/:1026/:1027/:1976``），
   *  给它们换渲染方式的代价远大于收益。只有 AI 助手页传 ctx。
   *    - ``ctx.tables``：Markdown 表格渲染成真 ``<table>``（默认塞 ``<pre>``）；
   *    - ``ctx.chip(marker, raw, before)``：把 ``[S1]`` 换成可点击的法条标签。 */
  function mdLite(text, ctx) {
    ctx = ctx || {};
    var raw = String(text || '').replace(/\r\n/g, '\n');
    var out = [];
    raw.split(/\n{2,}/).forEach(function (block) {
      var lines = block.split('\n');
      if (/^\s*\|/.test(lines[0])) {
        var tbl = ctx.tables ? tableBlock(lines) : '';
        if (tbl) { out.push(tbl); return; }
        // 表格块：整块按等宽保留，强行拆成 <table> 反而更难读
        out.push('<pre class="lp-pre">' + esc(block.trim()) + '</pre>');
        return;
      }
      var isList = lines.every(function (l) { return /^\s*([-*]|\d+[.、)])\s+/.test(l); });
      if (isList) {
        // 模板正文是"表单"结构：`- **原告**` 是栏目名，缩进两级的是栏目下的填空项。
        // 一律拍平成 <li> 会看不出层级，所以栏目名单独成一条加粗标题、后续项才进列表。
        var open = false;
        lines.forEach(function (l) {
          var m = l.match(/^\s*[-*]\s+\*\*(.+?)\*\*\s*$/);
          if (m) {
            if (open) { out.push('</ul>'); open = false; }
            out.push('<p class="doc-field"><b>' + inline(m[1]) + '</b></p>');
            return;
          }
          if (!open) { out.push('<ul>'); open = true; }
          out.push('<li>' + inline(l.replace(/^\s*([-*]|\d+[.、)])\s+/, '')) + '</li>');
        });
        if (open) out.push('</ul>');
        return;
      }
      lines.forEach(function (l) {
        // 套模板时后端带出的**版式标记**（落款右对齐 / 居中），见 template_service._norm_align。
        // ⚠️ 必须放在 `inline()` **之前**：`inline` 里会走 `esc` 把 `<` 转成 `&lt;`，
        //    一旦走进去就再也认不出这是标签了，页面上会直接显示 "<p align="right">"。
        // 只认整行、且只认这两种取值 —— 别放宽成"任意 <p …>"，那等于给正文开了个 HTML 口子。
        // `left` 是交付合同加的（落款签名区免缩进，见后端 revise_service._contract_markdown）：
        // 没有这一支，预览里会**原样显示** "<p align=\"left\">甲方：…</p>" 这串标记。
        var al = l.match(/^\s*<p\s+align="(right|center|left)">([\s\S]*?)<\/p>\s*$/);
        if (al) {
          out.push('<p class="doc-' + al[1] + '">' + inline(al[2]) + '</p>');
          return;
        }
        var h = l.match(/^\s*(#{1,4})\s*(.+)$/);
        if (h) {
          var lv = h[1].length;
          out.push('<h' + Math.min(lv, 3) + '>' + inline(h[2]) + '</h' + Math.min(lv, 3) + '>');
          return;
        }
        if (/^\s*>/.test(l)) { out.push('<p class="note">' + inline(l.replace(/^\s*>\s?/, '')) + '</p>'); return; }
        if (!l.trim()) return;
        out.push('<p>' + inline(l) + '</p>');
      });
    });
    return out.join('');

    /** Markdown 表格 → 真 <table>。判据要同时满足两条，缺一不可：
     *  ① 首行以 ``|`` 开头；② 第二行是分隔行（``|---|---|``）。
     *  只看①的话，正文里以竖线开头的普通句子会被当成表格切开。 */
    function tableBlock(lines) {
      if (lines.length < 2 || !isSeparator(lines[1])) return '';
      var head = cells(lines[0]);
      var body = lines.slice(2).map(cells).filter(function (r) { return r.some(Boolean); });
      if (!head.length || !body.length) return '';
      return '<div class="tblw"><table class="tbl"><thead><tr>'
        + head.map(function (h) { return '<th>' + inline(h) + '</th>'; }).join('')
        + '</tr></thead><tbody>'
        + body.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('')
        + '</tbody></table></div>';
    }
    function isSeparator(line) {
      if (line.indexOf('|') < 0 || line.indexOf('-') < 0) return false;
      return line.replace(/[|\-:\s]/g, '') === '';
    }
    function cells(line) {
      return line.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); });
    }

    function inline(s) {
      var h = esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>');
      // 给报告中的风险等级文字上色（高/中/低）
      h = h.replace(/(等级[：:])(高（必须改）|高|中（建议改）|中|低（可斟酌）|低)/g, function (_, prefix, lv) {
        var cls = /高/.test(lv) ? 'risk-high' : (/中/.test(lv) ? 'risk-medium' : 'risk-low');
        return prefix + '<b class="' + cls + '">' + lv + '</b>';
      });
      // 行内引用标记 → 可点击标签。**必须在 esc 之后**（上面第一行已经转义），
      // 否则模型输出里的 `<img onerror=...>` 会绕过转义直接进 DOM。
      // 回调的 offset 用来把「标记前面写了什么」交给调用方（避免法条名重复渲染），
      // 注意此时 h 仍是替换前的原串 —— 用 h.slice 取到的就是原文。
      if (ctx.chip) {
        h = h.replace(INLINE_CITE_RE, function (m, kind, num, offset) {
          return ctx.chip(kind + num, m, h.slice(Math.max(0, offset - 30), offset));
        });
      }
      return h;
    }
  }

  /** 行内引用标记：``[S1]`` / ``[C2]`` / ``[K3]``，与后端
   *  ``rag_answer.MARKER_RE``、``prompts.LEGAL_SYSTEM_PROMPT`` 三处必须一致。 */
  var INLINE_CITE_RE = /\[([SCK])(\d{1,2})\]/g;

  /** 真实核验失败判据：与后端 report_node._failed_reason 保持一致。
   *  方案B：只有命中真实失败原文才标"不可引用"，其余非失败条目标"参考引用"。 */
  function citeFailedReason(r) {
    r = r || '';
    return /未找到该案号|案号未能核验|校验工具不可用|疑似编造案号|未在本次检索的类案证据中|已废止|不得作为裁判依据|未通过类案真实性核验/.test(r);
  }
  /** 引用卡状态文案：可引用 / 参考引用 / 不可引用 三态。 */
  function citeOkLabel(c) {
    var r = c.reason || '';
    if (c.verified === true) {
      return /未逐条回溯|作参考引用/.test(r) ? '参考引用 · ' + r : '已核验通过';
    }
    if (c.verified === false) {
      return citeFailedReason(r) ? '不可引用 · ' + (r || '不得正式引用') : '参考引用 · ' + (r || '');
    }
    return '核验状态未知';
  }

  function citeBlock(c) {
    var name = c.doc_name || c.title || c.name || '引用';
    var art = c.article_no || c.article || '';
    var text = c.text || c.quote || '';
    var ok = citeOkLabel(c);
    return '<div class="cite"><div class="ct"><b>' + esc(name) + '</b>'
      + (art ? '<span class="art">' + esc(art) + '</span>' : '') + '</div>'
      + (text ? '<p class="cd">' + esc(truncate(text, 320)) + '</p>' : '')
      + '<div class="ok">' + icon('check', 'xs') + esc(ok) + '</div></div>';
  }

  /* ------------------------------------------------------- 纯 RAG 问答（AI 助手页） */
  /** 效力状态 → 样式类。**只做视觉区分，不改写文字** ——
   *  界面上写的就是后端返回的状态，不能把"已废止"渲染成看着还行的样子。 */
  function statusCls(status) {
    if (!status) return 'ne';
    if (/现行有效/.test(status)) return 'ok';
    if (/已废止|尚未生效/.test(status)) return 'bad';
    return 'mid';                       // 已修订 / 未知
  }

  function shortLaw(c) {
    var n = String(c.doc_name || c.title || '').replace(/^《|》$/g, '');
    return n ? (n + (c.article_no || '')) : '';
  }

  /** 行内标签的文字。优先用**效力状态**：模型正文里通常已经写了
   *  「《劳动合同法》第四十条」（``LEGAL_SYSTEM_PROMPT`` 第 4 条要求的格式），
   *  标签再重复一遍法条名反而读起来像口吃。没有状态时退回法条名。 */
  function chipLabel(c, marker) {
    if (c.type === 'case') return '类案';
    if (c.type === 'knowledge') return '指引';
    if (c.status) return c.status;
    return shortLaw(c) || marker;
  }

  function citeTitle(c) {
    var head = c.type === 'case'
      ? [c.case_name || '', c.case_no || '', c.court || '']
      : [shortLaw(c), c.status || ''];
    var body = c.type === 'case' ? (c.gist || c.result || '') : (c.text || '');
    return [head.filter(Boolean).join(' · '), truncate(body, 140)].filter(Boolean).join('\n');
  }

  /** 行内引用标签。
   *
   *  **类案标签可点**（用户反馈：标签点不开，案例就没地方看）—— 参考法规卡片区
   *  只放法条，类案在正文之外没有任何落点，所以点它弹 :func:`openCaseModal`。
   *  卡片本身走 ``CASE_CACHE`` 存，不塞进属性里 —— 一条案例有七八个字段，
   *  序列化进 HTML 属性既难看又难转义。
   *
   *  ``ansId`` 是这一条回答的序号：``C1`` 在每条回答里都从 1 开始，
   *  只按 marker 存会让后一条回答的 C1 覆盖掉前一条 —— 点旧答案里的标签
   *  弹出新答案的案例，是那种很难复现的错。
   *
   *  指引（K）仍不可点：参考区里没有它的落点，点了没反应比看着不可点更糟。 */
  function citeChip(marker, raw, byMarker, ansId) {
    var c = byMarker[marker];
    if (!c) return raw;                 // 后端没给这条引用 → 原样显示，界面不编
    var cls = 'rcite ' + statusCls(c.status) + (c.type === 'case' ? ' cas' : '');
    var title = esc(citeTitle(c));
    if (c.type === 'case') {
      CASE_CACHE[ansId + ':' + marker] = c;
      return '<button type="button" class="' + cls + '" data-case-ans="' + esc(ansId)
        + '" data-case-marker="' + esc(marker) + '" title="' + title + '">'
        + esc(chipLabel(c, marker)) + '</button>';
    }
    if (c.type === 'knowledge') {
      return '<span class="' + cls + '" title="' + title + '">' + esc(chipLabel(c, marker)) + '</span>';
    }
    return '<button type="button" class="' + cls + '" data-marker="' + esc(marker) + '" title="'
      + title + '">' + esc(chipLabel(c, marker)) + '</button>';
  }

  /** 案例详情正文。**类案检索页的「查看全文」与问答正文里的「类案」标签共用一份** ——
   *  两处各写一遍的话，读的字段早晚对不上（后台白名单一改，另一处还在读旧字段）。
   *
   *  相似度只在 ``0 < score <= 1`` 时显示：启发式精排给的是归一化分，
   *  而 CrossEncoder 精排给的是**原始 logit（可正可负）**，直接乘 100
   *  会渲染出「相似度 -173%」这种东西。宁可这一行不显示，也不能显示错的。 */
  function caseDetailBody(c) {
    var rows = [kvRow('案号', c.case_no), kvRow('法院', c.court),
      kvRow('裁判日期', c.judge_date), kvRow('案由', c.cause_of_action),
      kvRow('来源', c.source)];
    var s = Number(c.score);
    if (s > 0 && s <= 1) rows.push(kvRow('相似度', Math.round(s * 100) + '%'));
    var h = rows.filter(Boolean).length ? '<dl class="kv">' + rows.join('') + '</dl>' : '';
    if (c.gist) h += '<p><b>裁判要旨</b></p><p>' + esc(c.gist) + '</p>';
    if (c.result) h += '<p><b>裁判结果</b></p><p>' + esc(c.result) + '</p>';
    if (c.reasoning) h += '<p><b>裁判理由</b></p><p>' + esc(c.reasoning) + '</p>';
    if ((c.related_statutes || []).length) {
      h += '<p class="muted">相关法条：' + esc(c.related_statutes.join(' / ')) + '</p>';
    }
    if ((c.keywords || []).length) {
      h += '<p class="muted">关键词：' + esc(c.keywords.join(' / ')) + '</p>';
    }
    return h || '<p class="muted">这条引用没有可展示的案例内容。</p>';
  }

  function openCaseModal(c) {
    openModal({
      title: c.case_name || c.case_no || '案例详情',
      wide: true,
      body: caseDetailBody(c),
    });
  }

  /** 参考法规卡片区：**只有法条内容**（用户决策：沿革/新旧对照先不做）。 */
  function refBlock(refs) {
    var h = ['<details class="refs" open><summary>' + icon('scale', 'sm')
      + '参考法规 · ' + refs.length + ' 条</summary><div class="refs-b">'];
    refs.forEach(function (c) {
      h.push('<div class="ref" data-marker="' + esc(c.marker) + '">'
        + '<div class="ref-h"><span class="ref-n">' + esc(shortLaw(c)) + '</span>'
        + (c.status ? '<span class="ref-st ' + statusCls(c.status) + '">' + esc(c.status) + '</span>' : '')
        + '<span class="ref-m">' + esc(c.marker) + '</span></div>'
        + (c.publisher ? '<div class="ref-p">' + esc(c.publisher) + '</div>' : '')
        + (c.text ? '<p class="ref-t">' + esc(c.text) + '</p>' : '')
        + '</div>');
    });
    h.push('</div></details>');
    return h.join('');
  }

  /** 追问输入行。**只做这一个交互元素**（用户决策：不要分享/反馈/复制/下载表格）。 */
  function askMoreBox() {
    return '<div class="askmore">'
      + '<input type="text" class="askin" placeholder="继续追问，例如「那加班费怎么算」">'
      + '<button type="button" class="askgo" title="发送">' + icon('send', 'sm') + '</button></div>';
  }

  /** 纯 RAG 问答的渲染。
   *
   *  **不复用 ``formatStructuredAnswer``**：那一个会打印「置信度 X% · 引用 N/M 通过」
   *  摘要行并渲染证据缺口，正是用户要求去掉的报告脚手架；而且它被合同审查页共用，
   *  动它就会连带改掉那个页面。这条链路的交付物就是正文 + 参考法规，没有别的。 */
  function formatRagAnswer(data, elapsed) {
    var byMarker = {};
    (data.citations || []).forEach(function (c) { byMarker[c.marker] = c; });
    var ansId = ++ANS_SEQ;              // 类案卡片按「回答序号:marker」存，见 citeChip

    var parts = [];
    // 一行执行摘要（约束 4 的「结束」层）。**排在最前面**，与合同审查页
    // ``formatStructuredAnswer`` 的 ``.hm`` 同一位置 —— 这个气泡的第一行就是它，
    // 四个入口的成本口径于是长得一样：跑了几个节点、花了多久。
    //
    // ⚠️ 这**不是**把那个被摘掉的头加回来。摘掉的是对**法律结论**的背书
    // （意图 / 置信度 / 引用通过率），而这一行说的是这次跑了多久 ——
    // 它不替任何一条依据担保，也不给任何结论打分。
    // 后端没给摘要时（历史会话里回放的老行）整行不出现，不显示空壳。
    if (data.summary) parts.push('<p class="rag-meta">' + esc(data.summary) + '</p>');

    parts.push('<div class="rag-a">' + mdLite(data.answer || '', {
      tables: true,
      chip: function (marker, raw) { return citeChip(marker, raw, byMarker, ansId); },
    }) + '</div>');

    // 正文里写了、但检索结果中查无此条的条文号（后端已把条号换成"相应条款"）。
    // **如实告知**：这是红线「无来源不出引用」唯一会让步的地方，让步了就得说。
    var uns = (data.ref_check || {}).unsupported || [];
    if (uns.length) {
      parts.push('<p class="rag-warn">正文中有 ' + uns.length
        + ' 处条文号在本次检索结果里找不到对应条文，已按「相应条款」表述，请核对后再引用。</p>');
    }

    var statutes = (data.citations || []).filter(function (c) { return c.type !== 'case'; });
    if (statutes.length) parts.push(refBlock(statutes));
    parts.push(askMoreBox());
    return parts.join('');
  }

  /** 把后端 done 数据包渲染成 HTML。同时被 AI 助手聊天和合同审查专属页复用。 */
  function formatStructuredAnswer(data, elapsed, opts) {
    opts = opts || {};
    var parts = [];
    var cs = data.citation_summary || {};
    var counts = data.counts || {};

    // 一行执行摘要（约束 4 的「结束」层）。后端 done 帧自带 ``summary``
    // （「本次执行：4 步 · 8.4s」）。步数取 ``used_nodes`` 的条数 ——
    // **实际跑过哪几个节点**（去重保序），不是 ``steps``：回环会把同一个节点
    // 跑第二遍，那时 ``steps`` 涨而节点数不涨，两个数本来就不是一回事。
    // 它**取代**了原先并排的「N 步」与「耗时」两格 —— 那两个数已经在摘要里，
    // 再留一份就是同一行里报两遍。后端没给摘要时（历史会话里存下的旧行）回退旧写法。
    var execLine = data.summary
      || [data.steps ? data.steps + ' 步' : '', fmtMs(data.elapsed_ms || elapsed)]
        .filter(Boolean).join(' · ');

    parts.push('<div class="hm">'
      + esc([INTENT_LABEL[data.intent] || data.intent || '自动判定',
        '置信度 ' + pct(data.confidence),
        cs.checked !== undefined
          ? '引用 ' + (cs.verified || 0) + '/' + (cs.checked || 0) + ' 通过'
          + (cs.blocking ? '，拦截 ' + cs.blocking : '')
          : '引用 ' + (counts.citations || 0) + ' 条',
        execLine].filter(Boolean).join(' · '))
      + '</div>');

    // 引用校验不通过时的黄字提醒。**合同审查页不显示**（2026-09-29 用户点名「注意 不需要」）：
    // 这页每跑一趟几乎都拦下 2~3 处（模型引了检索池里没有的条文号），一律出这行等于
    // 每份结果上都贴一张一样的告示，律师读两次就不读了。逐条的依据该不该信，
    // 卡片上的「依据」栏已经说了（被剔空的会写「（需补充法律依据）」）——
    // 那才是能定位到条款的信息，这一行只是一句笼统的免责。
    // AI 助手那条链路**保留**：那里没有风险卡，这行是它唯一的引用可信度提示。
    if (data.citation_check && data.citation_check !== 'passed' && !opts.contract) {
      parts.push('<p style="color:var(--wn)"><b>注意：</b>本次引用校验未全部通过（'
        + esc(data.citation_check) + '），请核对依据后再使用。</p>');
    }
    // 合同审查的**主产出就是这几张卡**，所以它排在报告前面。
    // 原来这里是一个 ``<ul>`` 且 ``slice(0, 8)``，还被压在几千字的报告下面 ——
    // 律师要滚过整篇报告才看得到风险点，第 9 条之后的（往往是兜底条款上的
    // 低风险项）则**永远不显示**。别把 slice 加回来。
    // 修订版区块：合同审查的最终交付物是"改完的合同"，不是风险清单。
    // 风险点、修改建议、依据在扫描阶段就都有了，这里只把它们落到正文物上 + 给导出入口。
    var revBlock = (data.risk_points || []).length && data.task_id ? revisionBlock(data) : '';

    // 合同审查页把它放在**风险卡前面**（2026-09-29 用户：「改后的合同放在前面，
    // 放在后面我要滑动很久」）：十几张风险卡每张都带一段条款原文，一屏放不下三条，
    // 「生成改后合同」被压在整份清单的后面，每次都要滑到底。
    // ⚠ 助手那条链路**不跟着挪**：那里报告是主产出、风险点只是附带，改后合同排在
    // 报告之后读起来是"先看分析、再拿交付物"，与合同页"先拿交付物、再核清单"是两回事。
    if (opts.contract && revBlock) parts.push(revBlock);

    var nCards = (data.risk_points || []).length;
    // 双模型交叉复核摘要：第二模型复审过才出现（后端没配复审模型时不给这个键，
    // 这里就一条都不渲染 —— 别让"没复审"装成"复审过了"）。
    var sp = data.second_pass || {};
    if (sp.ran) {
      parts.push('<p class="cd">已由第二模型复审（' + esc(sp.model || '复审模型')
        + '）：维持 ' + (sp.confirmed || 0) + ' · 降级 ' + (sp.downgraded || 0)
        + ' · 删除 ' + (sp.removed || 0) + ' · 补漏 ' + (sp.added || 0) + '</p>');
    }
    if (data.spot_check && data.spot_check.needed) {
      parts.push('<p style="color:var(--wn)"><b>待人工抽查：</b>本次审查已按抽样规则标记，请安排人工复核。</p>');
    }
    if (nCards) {
      parts.push('<p><b>风险点（' + nCards + '）</b></p>' + ctRiskCards(data.risk_points));
    }
    // 证据缺口：后端给的是 {kind, reason, impact} 对象，不是字符串。
    // 合同审查页**只在一条风险卡都没有时才显示**（2026-09-29 用户点名：正常跑完
    // 有卡时它只是噪声）。但**不能跟着一起删干净** —— 降到规则引擎兜底那一趟
    // 没有卡，这行是"为什么一条风险点都没有"的唯一解释，删了就是一片空白。
    // ⚠ 别把上面那句 nCards 判断去掉改成无条件：那正是用户要的"不要"的反面。
    if (!opts.contract || !nCards) {
      (data.evidence_gap || []).slice(0, 5).forEach(function (g) {
        var text = typeof g === 'string' ? g
          : [(g.kind ? GAP_LABEL[g.kind] || g.kind : ''), g.reason || g.note || g.detail || '',
            g.impact ? '（影响：' + g.impact + '）' : ''].filter(Boolean).join('：');
        if (text) parts.push('<p class="cd">证据缺口：' + esc(text) + '</p>');
      });
    }
    // 报告实测 4000~8000 字。
    // ⚠ **合同审查页不渲染它**（2026-09-29 用户点名「完整报告，不要」）：后端照旧
    // 产出全文，要读点「查看任务详情」——那个弹层的『结果正文』就是它。
    // ⚠ 这个函数**AI 助手那条链路共用**（app.js:1389），而助手的主产出恰恰是
    // 这份报告：删的时候必须带 ``!opts.contract``，改成无条件删等于把回答删了。
    if (data.report && !opts.contract) {
      parts.push('<details class="ct-report"><summary>完整报告（' + data.report.length
        + ' 字）</summary>' + mdLite(data.report) + '</details>');
    }
    if ((data.issues || []).length) {
      parts.push('<p><b>争议焦点（' + data.issues.length + '）</b></p><ul>'
        + data.issues.slice(0, 8).map(function (i) {
          return '<li>' + esc(i.title || i.issue || i.name || String(i)) + '</li>';
        }).join('') + '</ul>');
    }
    if ((data.strategy || []).length) {
      parts.push('<p><b>应对策略（' + data.strategy.length + '）</b></p><ul>'
        + data.strategy.slice(0, 8).map(function (s) {
          return '<li>' + esc(typeof s === 'string' ? s : (s.title || s.point || JSON.stringify(s))) + '</li>';
        }).join('') + '</ul>');
    }
    if (data.draft) {
      parts.push('<p><b>文书初稿（' + data.draft.length + ' 字）</b></p><pre class="lp-pre">' + esc(data.draft) + '</pre>');
    }
    (data.citations || []).slice(0, 6).forEach(function (c) { parts.push(citeBlock(c)); });

    // 修订版（助手那条链路的位置，见上面 revBlock 的说明）
    if (revBlock && !opts.contract) parts.push(revBlock);

    if (!opts.noOps) {
      var ops = ['<div class="ops">'];
      if (data.report || data.draft) {
        ops.push('<button class="btn sm" data-copy="' + esc(data.task_id || '') + '">'
          + icon('doc', 'sm') + '复制全文</button>');
      }
      ops.push('<button class="btn sm" data-run="case">' + icon('search', 'sm') + '检索同类案例</button>');
      if (data.task_id) {
        ops.push('<button class="btn sm" data-task="' + esc(data.task_id) + '">' + icon('clock', 'sm') + '查看任务详情</button>');
        // 证据链只对诉讼案件开放（见 openCase 上方那段说明）。合同审查页的结果**一定是**
        // 「非诉/合同」案件（case_service 按 intent 定类型：contract_review → contract），
        // 所以那一页直接不出这个按钮 —— 别让用户点了才被反查/后端拦回来。
        // AI 助手那条链路不传 noEvchain：那边的 task 可能归属诉讼案件，由 evchainAction 反查后判。
        if (!opts.noEvchain) {
          ops.push('<button class="btn sm" data-evchain-task="' + esc(data.task_id) + '">' + icon('scale', 'sm') + '生成证据链</button>');
        }
      }
      ops.push('</div>');
      parts.push(ops.join(''));
    }
    return parts.join('');
  }

  /** 修订版区块：两条交付路径 —— ①按风险重写成"改完的合同"（主）②带标记的对照稿（核对用）。 */
  function revisionBlock(data) {
    var rps = data.risk_points || [];
    var lv = { high: 0, medium: 0, low: 0 };
    rps.forEach(function (r) { if (lv[r.level] !== undefined) lv[r.level]++; });
    var h = ['<div class="rev">',
      '<div class="rev-h">' + icon('edit', 'sm') + '改后合同'
      + '<span class="lv">'
      + '<span>高 <b class="risk-high">' + lv.high + '</b></span>'
      + '<span>中 <b class="risk-medium">' + lv.medium + '</b></span>'
      + '<span>低 <b class="risk-low">' + lv.low + '</b></span>'
      + '</span></div>',
      // 这段说明**不能说"上面的/下面的"风险**：这个区块在合同审查页排在风险卡**前面**，
      // 在 AI 助手里排在后面（见 formatStructuredAnswer 里 revBlock 的说明），
      // 两边共用同一段文字，写死方向就有一边指错地方。
      '<div class="rev-hint">按风险点逐条重写合同条款，直接产出改完的合同全文'
      + '（不含批注标记，可下载 Word / PDF 直接用）。风险条款之外的内容一字不动；'
      + '某条改写失败会保留原文并在结果里标出来。</div>',
      '<div class="ops">',
      '<button class="btn sm pr" data-revise="' + esc(data.task_id || '') + '" data-mode="rewrite">'
      + icon('zap', 'sm') + '生成改后合同（约 10~30 秒）</button>',
      '<button class="btn sm" data-revise="' + esc(data.task_id || '') + '" data-mode="annotate">'
      + icon('edit', 'sm') + '只要对照稿（秒级）</button>',
      '</div>',
      '<div class="rev-out" hidden></div>'];
    if (data.revision_draft) {
      h.push('<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12.5px;color:var(--tt)">'
        + '逐条对照稿（原文 → 修改后 → 依据）</summary>'
        + mdLite(data.revision_draft) + '</details>');
    }
    h.push('</div>');
    return h.join('');
  }

  /** 生成改后合同 / 对照稿。mode 决定后端走重写还是只做标注。 */
  function generateRevision(btn) {
    if (btn.disabled) return;
    var taskId = btn.getAttribute('data-revise') || '';
    var mode = btn.getAttribute('data-mode') || 'rewrite';
    var box = btn.closest('.rev');
    var out = box ? $('.rev-out', box) : null;
    btn.disabled = true;
    var old = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>' + (mode === 'rewrite' ? '改写中…' : '生成中…');
    if (out) {
      out.hidden = false;
      out.innerHTML = '<div class="tm muted">' + (mode === 'rewrite'
        ? '正在逐条把风险条款改写成正式条文（每条约几秒，请稍候）…'
        : '正在把风险点落回合同正文…') + '</div>';
    }
    J('/contract/revise', { method: 'POST', json: { task_id: taskId, save: true, mode: mode } })
      .then(function (r) { renderRevision(r, box, out); })
      .catch(function (e) {
        if (out) out.innerHTML = '<p style="color:var(--dg)">' + (mode === 'rewrite' ? '生成改后合同失败：' : '生成对照稿失败：') + esc(e.message) + '</p>';
        toast('失败：' + e.message);
      })
      .then(function () { btn.disabled = false; btn.innerHTML = old; });
  }

  function renderRevision(r, box, out) {
    if (!out) return;
    var st = r.stats || {};
    var isRewrite = (r.mode || 'rewrite') === 'rewrite';
    REVISE_CACHE[r.draft_id || ''] = r.clean_text || '';
    // 导出用哪一份正文：改后合同 = 干净全文；对照稿 = 带删除线/插入标记的那份。
    // 草稿里存的就是交付正文，但导出接口的 body.content 优先，明确传过去最稳。
    REVISE_MD[r.draft_id || ''] = isRewrite
      ? (r.contract_text || r.rewritten_text || '')
      : (r.revised_markdown || '');
    var h = [];
    if (isRewrite) {
      h.push('<div class="rev-hint" style="margin-top:10px">'
        + '按风险重写：应改 <b>' + (st.applied || 0) + '</b> 条，已改写 <b class="risk-low">'
        + (st.rewritten || 0) + '</b> 条'
        + ((st.rewrite_skipped || 0) ? '，<b class="risk-medium">' + st.rewrite_skipped + ' 条改写失败已保留原文（需人工处理）</b>' : '')
        + (st.unlocated ? '，' + st.unlocated + ' 处未能定位（需人工处理）' : '')
        + '。以下是改完的合同全文，风险条款已替换为合规条文，其余内容原样保留。</div>');
    } else {
      h.push('<div class="rev-hint" style="margin-top:10px">'
        + '对照稿：风险 ' + (st.total || 0) + ' 处（高 ' + (st.high || 0) + ' / 中 ' + (st.medium || 0)
        + ' / 低 ' + (st.low || 0) + '），已就地标注 ' + (st.applied || 0) + ' 处'
        + (st.merged ? '，' + st.merged + ' 处与相邻修订重叠已合并' : '')
        + (st.unlocated ? '，<b class="risk-medium">' + st.unlocated + ' 处未能定位（需人工处理）</b>' : '')
        + '</div>');
    }
    if (r.warning) h.push('<p style="color:var(--wn);font-size:12.5px"><b>注意：</b>' + esc(r.warning) + '</p>');
    // 跨条款重复：把具体是哪两句摊出来，律师才知道去哪删（只提示，不自动删）
    if ((r.duplicates || []).length) {
      h.push('<div class="rev-chg" style="border-top:none;padding-top:0">'
        + '<div class="rev-hint">疑似重复表述（同一个问题被两条条款各写了一遍，建议删掉一处）：</div>'
        + r.duplicates.slice(0, 6).map(function (d) {
          return '<div class="row"><span class="no">' + esc(d.a_clause || '') + '↔' + esc(d.b_clause || '')
            + '</span><span class="tx">' + esc(truncate(d.a_text || '', 70))
            + '<br><span class="muted">相似度 ' + esc(String(d.similarity || '')) + '</span></span></div>';
        }).join('') + '</div>');
    }
    h.push('<div class="ops">');
    if (r.draft_id) {
      var fname = esc((r.title || '改后合同').replace(/·修订版$/, isRewrite ? '·改后版' : '·对照稿'));
      h.push('<button class="btn sm pr" data-revdl="docx" data-draft="' + esc(r.draft_id) + '" data-title="' + fname + '">'
        + icon('dl', 'sm') + '下载 Word</button>');
      h.push('<button class="btn sm" data-revdl="pdf" data-draft="' + esc(r.draft_id) + '" data-title="' + fname + '">'
        + icon('dl', 'sm') + '下载 PDF</button>');
    }
    h.push('<button class="btn sm" data-revcopy="' + esc(r.draft_id || '') + '">' + icon('doc', 'sm')
      + (isRewrite ? '复制改后合同' : '复制对照稿') + '</button>');
    h.push('<span class="spacer"></span></div>');

    // 逐条对照：改前 → 改后。这是律师唯一能快速核对"改对没改对"的地方，必须摊开。
    h.push('<div class="rev-chg">');
    (r.changes || []).forEach(function (c) {
      var lvText = { high: '高', medium: '中', low: '低' }[c.level] || '';
      var lvCls = 'risk-' + (c.level || 'medium');
      var rw = c.rewritten || '';
      var note = c.merged ? '（与相邻修订重叠，已合并）'
        : (c.located ? '' : '（未能定位，需人工处理）');
      var tail;
      if (c.deleted) tail = '<b class="risk-medium">整条删除</b>';
      else if (rw) tail = esc(truncate(rw, 220));
      else if (c.located) tail = '<span class="muted">（未改写，保留原文）</span>';
      else tail = esc(truncate(c.suggestion || c.risk || '', 120)) + note;
      h.push('<div class="row' + (c.located || c.merged ? '' : ' un') + '">'
        + '<span class="no">' + esc(c.clause_no || '—') + '</span>'
        + '<span class="' + lvCls + '" style="flex:none">' + esc(lvText) + '</span>'
        + '<span class="tx">'
        + ((rw || c.deleted || isRewrite)
            ? '<span class="muted">改前：</span>' + esc(truncate(c.original || '', 90))
              + '<br><span style="color:var(--ac)">改后：</span>' + tail
            : esc(truncate(c.suggestion || c.risk || '', 90)) + note)
        + (c.rewrite_note ? '<br><span class="muted">说明：' + esc(truncate(c.rewrite_note, 90)) + '</span>' : '')
        + '</span></div>');
    });
    h.push('</div>');
    h.push('<details style="margin-top:8px"' + (isRewrite ? ' open' : '')
      + '><summary style="cursor:pointer;font-size:12.5px;color:var(--tt)">'
      + (isRewrite ? '查看改后合同全文' : '查看对照稿全文（原文删除线 + 修订建议）')
      + '</summary><div class="rev-body"></div></details>');
    out.innerHTML = h.join('');
    // 全文分帧插入：整份合同长度不比报告短
    var body = $('.rev-body', out);
    if (body) {
      paintChunks(body, isRewrite
        ? mdLite(r.contract_text || r.rewritten_text || '')
        : mdLite(r.revised_markdown || ''));
    }
    setReviseState(r.draft_id || '');
  }

  /** 下载修订版：先导出再下载，走的是文书草稿那套既有能力。 */
  function downloadRevision(btn) {
    var id = btn.getAttribute('data-draft') || '';
    var fmt = btn.getAttribute('data-revdl') || 'docx';
    var title = btn.getAttribute('data-title') || '修订版合同';
    if (!id) { toast('修订版还没生成'); return; }
    if (btn.disabled) return;
    btn.disabled = true;
    var old = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>生成中…';
    toast(fmt === 'docx' ? '正在生成 Word…' : '正在生成 PDF…');
    // 带上带标记的修订稿：Word/PDF 里才有删除线与蓝色插入，能看出"改了什么"。
    // 不带的话导出的是草稿里的干净版，等于把修订痕迹丢了。
    var body = { content: REVISE_MD[id] || '' };
    J('/drafts/' + encodeURIComponent(id) + '/exports?fmt=' + fmt, { method: 'POST', json: body })
      .then(function () { return req('/drafts/' + encodeURIComponent(id) + '/download'); })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.blob().then(function (blob) {
          downloadBlob(blob, title + '.' + (fmt === 'pdf' ? 'pdf' : fmt));
          toast('已开始下载');
        });
      })
      .catch(function (e) { toast('下载失败：' + e.message); })
      .then(function () { btn.disabled = false; btn.innerHTML = old; });
  }

  var REVISE_CACHE = {};
  var REVISE_MD = {};
  var reviseState = { draftId: '' };
  function setReviseState(id) { reviseState.draftId = id; }

  /** 结果区里修订相关按钮的统一入口（AI 助手与合同审查页共用）。 */
  function reviseAction(e) {
    var gen = e.target.closest('[data-revise]');
    if (gen) { generateRevision(gen); return true; }
    var dl = e.target.closest('[data-revdl]');
    if (dl) { downloadRevision(dl); return true; }
    var cp = e.target.closest('[data-revcopy]');
    if (cp) { copyText(REVISE_CACHE[cp.getAttribute('data-revcopy') || ''] || ''); return true; }
    return false;
  }

  /** 把 ``done`` 的结果画进气泡。
   *
   *  结果区是气泡里的 ``.ans``（不是整个 ``.bb``）：上面的「执行轨迹」面板要留下，
   *  被 ``innerHTML`` 一起冲掉就没法复盘"慢在哪一步"了。
   *  正文走 :func:`paintChunks` 分帧插入，首屏立刻可见。
   *
   *  **AI 助手页走 :func:`formatRagAnswer`，不是 :func:`formatStructuredAnswer`。**
   *  后者渲染的是「报告」——含置信度摘要行与证据缺口，正是这个页面要去掉的东西。
   *  它仍被合同审查页直接调用（见 ``formatStructuredAnswer`` 的注释），所以原样保留。 */
  /** AI 助手的结果包 → ``formatRagAnswer`` 认得的形状。
   *
   *  **为什么要有这一层。** 助手页现在有两条数据来源：老的 ``/chat/rag/stream``
   *  （引用卡自带 ``marker``）和 Agent 链路的 ``/agents/assistant/stream``
   *  （给的是 ValidatorNode 的**终审记录**，没有 ``marker``，法条全文在 ``statutes`` 里）。
   *  渲染层只认前一种，直接把后一种喂进去：``byMarker[c.marker]`` 全是 ``undefined``，
   *  ``refBlock`` 只渲染出一张标题空白的卡 —— 页面上看起来只是"少了几条依据"。
   *
   *  判据有两条，**都要有**：``ref_check`` 存在（RAG 的返回体恒有它）、
   *  或 ``citations`` 里带 ``marker``。只看 marker 会误伤一种 RAG 结果 ——
   *  检索零命中而模型仍写了 ``[S1]``，此时 ``citations`` 是空的、``ref_check``
   *  里却记着"有 N 处条文号找不到对应条文"。适配器一接管就把那句提醒吞了，
   *  而它正是这条链路唯一会告诉律师"这条依据查不到"的地方。
   *
   *  判定为"已适配过"就原样返回（含历史会话里存下来的老行），避免二次包装。
   *  所以 ``replayConversation`` 那条路**不用改** —— 它把库里的 payload 原样
   *  交给 ``renderAnswer``，适配在这里统一发生。
   *
   *  ⚠ 类案不进口径：``refBlock`` 本来就过滤掉 ``type === 'case'``（类案有自己
   *  的弹窗，不走参考法规区），而且这条链路的报告正文里有代码生成的「引用清单」
   *  会逐条列出它们。塞进来只会让 ``CASE_CACHE`` 多几条取不到的键。
   */
  function answerShape(data) {
    data = data || {};
    var clips = data.citations || [];
    if (data.ref_check) return data;
    for (var i = 0; i < clips.length; i++) { if (clips[i] && clips[i].marker) return data; }

    // 字段名两条链路本来就是同一套（``doc_name`` / ``article_no`` / ``text`` /
    // ``status`` / ``publisher``，见 backend/tools/registry.py 的 statute_search 返回），
    // 所以这里只补 ``marker`` 与 ``type``，不做字段翻译。
    var cards = (data.statutes || []).map(function (s, i) {
      var c = {};
      for (var k in s) { if (Object.prototype.hasOwnProperty.call(s, k)) c[k] = s[k]; }
      c.marker = 'S' + (i + 1);
      c.type = 'statute';
      return c;
    }).concat((data.knowledges || []).map(function (k, i) {
      var c = {};
      for (var kk in k) { if (Object.prototype.hasOwnProperty.call(k, kk)) c[kk] = k[kk]; }
      c.marker = 'K' + (i + 1);
      c.type = 'knowledge';
      return c;
    }));

    return {
      answer: data.answer || data.report || '',
      // **摘要要显式搬过来。** 这一层把结果包重建成「渲染层认得的形状」，
      // 没列进来的键一律丢掉 —— 漏了它，助手页那行「本次执行：N 步 · X.Xs」
      // 会在 Agent 链路上**静默不显示**（老 RAG 链路走上面那两处原样返回，
      // 照样显示，于是只在一条链路上消失，更像渲染层的偶发问题）。
      summary: data.summary || '',
      citations: cards,
      // 这条链路的"正文里有条文号检索不到"由 ValidatorNode 直接处理掉了
      // （拦下的引用不进 citations，报告正文里也有「被拦截引用清单」一节），
      // 所以没有 unsupported 这回事，给空数组即可 —— 硬造一条会把
      // 报告里已经写清楚的事再说一遍。
      ref_check: { unsupported: [], ok: true },
    };
  }

  function renderAnswer(box, data, elapsed) {
    var bb = $('.bb', box);
    if (!bb) return;
    var host = dropTyping(bb);
    var chatbox = $('#chatbox');
    // 专家徽标：专家会话的回答在正文头部打上醒目标签，普通助手不显示，
    // 让"这条回答是谁给的"第一眼可辨（与后端落款前后呼应）。
    if (chat.expertId && chat.expertName) {
      var tag = el('div', 'ex-tag');
      tag.textContent = '专家 · ' + chat.expertName;
      host.insertBefore(tag, host.firstChild);
    }
    paintChunks(host, formatRagAnswer(answerShape(data), elapsed), function () { stick(chatbox); });
    stick(chatbox, true);
  }

  /** 收掉"正在思考"的三个点，返回正文该落进去的容器。
   *
   *  **两条链路都要走这里。** 抽出来的直接原因：``askAgent`` 恢复时自己拼了
   *  ``paintChunks(ansHost, …)`` 而没走 ``renderAnswer``，于是那三个点永远留在
   *  报告正上方 —— 看起来像"还在生成"，而报告早就出完了。
   *  这类"渲染入口有两份、其中一份漏了一步"的重复，抽成一处比再抄一遍可靠。 */
  /** 只收掉"正在思考"的三个点，不要返回值。
   *
   *  **失败分支也必须调**：错误文案已经写出来了、转圈却还在转 —— 看起来像
   *  "还在生成"，比直接报错更误导。 */
  /** 收掉等待位上的东西。**转圈和"决策那一句"都要收。**
   *
   *  ``showDecision`` 是给同一个节点**换类名**（``.tp3`` → ``.agent-note``），
   *  换过之后它就不再是 ``.tp3`` 了 —— 这里只找 ``.tp3`` 的话，
   *  凡是这一趟出现过决策，那句就永远留在气泡里、压在正文上方：
   *  看起来像"还在跑"，而四个入口的表现会各不相同（只有真的裁决过才留下）。
   */
  function dropSpin(host) {
    var sp = $('.tp3', host) || $('.agent-note', host);
    if (sp && sp.parentNode) sp.parentNode.removeChild(sp);
  }

  function dropTyping(bb) {
    dropSpin(bb);
    return $('.ans', bb) || bb;
  }

  /** 把 Agent 的一句**关键决策**显示在等待位上（约束 4 的「过程中」层）。
   *
   *  **替换而不是追加**：一次任务可能裁决多轮（补一次、再审一轮、再补一次），
   *  逐条堆起来就又成了一个执行轨迹面板 —— 而那个面板 2026-09-29 是特意摘掉的。
   *  这里永远只留最新的一句。
   *
   *  **不新建容器**：复用等待中那三个点（``.tp3``）所在的位置，有决策就把圆点
   *  换成文字；没决策时它还是三个点，页面上什么都不会多出来。
   *  ``done`` 一到就随 ``dropTyping`` 一起收走，不留痕。
   *
   *  ``textContent`` 而不是 ``innerHTML``：这句话里带模型的输出，
   *  别让它碰到 DOM。 */
  function showDecision(host, text) {
    var sp = $('.tp3', host) || $('.agent-note', host);
    if (!sp || !text) return;
    sp.className = 'agent-note';
    sp.textContent = text;
  }
  var ANSWER_CACHE = {};
  /** 类案卡片：``"<回答序号>:C1"`` → 后端给的那张引用卡。点标签弹窗时取。 */
  var CASE_CACHE = {};
  var ANS_SEQ = 0;

  /** 极简 SSE：POST + ReadableStream，按空行切帧。
   *  注意帧分隔是 `\n\n`（不是 `\r\n\r\n`），两者都要认。 */
  function stream(path, body, handlers) {
    var headers = { 'Content-Type': 'application/json' };
    if (tok()) headers.Authorization = 'Bearer ' + tok();
    var ctrl = new AbortController();
    fetch(API + path, {
      method: 'POST', headers: headers, body: JSON.stringify(body), signal: ctrl.signal,
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          var d = '';
          try { d = JSON.parse(t).detail || ''; } catch (e) { d = ''; }
          throw new Error(d || ('HTTP ' + res.status));
        });
      }
      // 帧解析只有一份实现（见 ssePump）—— 这条流与任务进度流共用它。
      return ssePump(res, handlers);
    }).catch(function (e) {
      if (handlers.onError) handlers.onError(e);
    });
    return { abort: function () { ctrl.abort(); } };
  }

  /** 把一条 SSE 响应喂给 `handlers`（`onFrame(事件名, 数据)` / `onEnd`）。
   *
   *  ⛔ **帧解析只有这一份实现**：`stream()`（POST 发起并订阅）与
   *  `taskStream()`（GET 只订阅一条已在跑的任务）都用它。拆成两份的代价不是
   *  多写 20 行，而是**分叉** —— 表现是"AI 助手页的进度能动、合同解析页的不动"，
   *  而两边代码看着一模一样（帧分隔到底认 `\n\n` 还是 `\r\n\r\n`、keep-alive
   *  这种没有 `data:` 的注释帧该不该丢，这些坑只该踩一次）。
   */
  function ssePump(res, handlers) {
    var reader = res.body.getReader();
    var dec = new TextDecoder('utf-8');
    var buf = '';
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) { if (handlers.onEnd) handlers.onEnd(); return; }
        buf += dec.decode(r.value, { stream: true });
        for (;;) {
          var i = buf.indexOf('\n\n');
          var j = buf.indexOf('\r\n\r\n');
          var cut = -1, len = 2;
          if (i >= 0 && (j < 0 || i < j)) { cut = i; len = 2; }
          else if (j >= 0) { cut = j; len = 4; }
          if (cut < 0) break;
          var frame = buf.slice(0, cut);
          buf = buf.slice(cut + len);
          var evName = 'message';
          var dataLines = [];
          frame.split(/\r?\n/).forEach(function (line) {
            if (line.indexOf('event:') === 0) evName = line.slice(6).trim();
            else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5).trim());
          });
          // 没有 `data:` 的帧是 keep-alive 注释（后端每轮插一行 `: keep-alive`），
          // 丢掉它对：它唯一的用途是让中间的代理别把长连接掐掉。
          if (!dataLines.length) continue;
          var raw = dataLines.join('\n');
          var parsed = raw;
          try { parsed = JSON.parse(raw); } catch (e) { /* 非 JSON 原样给出 */ }
          handlers.onFrame(evName, parsed);
        }
        return pump();
      });
    }
    return pump();
  }

  /** 订阅一条**已存在任务**的进度：`GET /tasks/{id}/stream`（SSE）。
   *
   *  与 `stream()` 的分工：那个是"发起并订阅"（POST 本身会把任务跑起来），
   *  这个是"只订阅"（任务已由 `POST /contract/parse` 入队）。
   *
   *  ⛔ 不用 `EventSource`：它**不能带 `Authorization`** 头，而本项目的令牌只走头
   *  （放 query 上会进各级访问日志与浏览器历史）。所以是 fetch + 读流，
   *  帧解析与 `stream()` 共用 `ssePump`。
   *
   *  后端这条流是**轮询落库快照**（有变化才发一帧，任务进终态时发 `done` 后收尾），
   *  帧名见 `backend/app/api/v1/tasks.py`：`progress` / `done` / `error`。
   */
  function taskStream(taskId, handlers) {
    var headers = {};
    if (tok()) headers.Authorization = 'Bearer ' + tok();
    var ctrl = new AbortController();
    fetch(API + '/tasks/' + encodeURIComponent(taskId) + '/stream', {
      method: 'GET', headers: headers, cache: 'no-store', signal: ctrl.signal,
    }).then(function (res) {
      if (!res.ok || !res.body) return failDetail(res);
      return ssePump(res, handlers);
    }).catch(function (e) {
      if (handlers.onError) handlers.onError(e);
    });
    return { abort: function () { ctrl.abort(); } };
  }

  /** 读一个失败响应的 `detail` 并抛出去（FastAPI 的业务原因都在这个字段里）。
   *  三处上传入口都要做同一件事，抽出来免得各写一遍 —— 各写一遍的后果是
   *  有一处忘了解析 JSON，界面上就会出现裸的 `HTTP 500`。 */
  function failDetail(r) {
    return r.text().then(function (t) {
      var d = '';
      try { d = JSON.parse(t).detail || ''; } catch (e) { d = ''; }
      throw new Error(d || ('HTTP ' + r.status));
    });
  }

  /** 上传合同材料的**统一入口**：受理 → 订阅进度 → 取正文。
   *
   *  三处上传入口（合同审查页、文书页的「相关材料」、类案检索的附件）原先都是
   *  "一次请求解析完"。而 `req()` 有 20 秒全局超时兜底，一份扫描件要多模态整页
   *  转写几十秒 —— 于是文件明明没问题，界面上报的却是
   *  「解析失败：signal is aborted without reason」（abort 掉的是等结果的那条请求）。
   *
   *  现在 `POST /contract/parse` 只做**受理**：落盘 + 入队 + 立刻回 `task_id`，
   *  真正的解析在 worker 里跑、进度写进任务表。这里把后面两步包起来：
   *  订阅 `/tasks/{id}/stream` 直到终态，再取 `GET /contract/parse/{id}` 的正文。
   *
   *  **resolve 出来的是与改造前同形的结果对象**（`files[].text` / `merged` /
   *  `merged_chars`），所以三个调用方的取数逻辑一行都不用改，只是"怎么等"变了。
   *
   *  `opt.onAccepted(acc)`：受理完成时回调，`acc.accepted` / `acc.rejected` 是
   *  逐份的受理结果 —— 类型不支持、空文件、超过 50M 的当场就能告诉用户哪一份没收下
   *  （旧同步实现在这几条上也是当场提示的，不能因为改成异步就丢掉）。
   *  `opt.onNote(text)`：每份解析完的一句进度。
   */
  function parseUpload(files, opt) {
    opt = opt || {};
    var form = new FormData();
    for (var i = 0; i < files.length; i++) form.append('files', files[i]);
    // 受理本身很快（只落盘 + 写一行队列），但它仍是一次网络往返：给它 60 秒。
    // **显式写出来**而不是沿用 `req()` 的 20 秒默认值 —— 这里的超时意味着
    // 后端没受理成功，早失败早报错，别让人对着"正在解析…"干等。
    return req('/contract/parse', { method: 'POST', body: form, timeout: 60000 })
      .then(function (r) { return r.ok ? r.json() : failDetail(r); })
      .then(function (acc) {
        if (opt.onAccepted) opt.onAccepted(acc);
        return waitParsed(acc, opt);
      });
  }

  /** 等解析任务跑完并取回正文（`parseUpload` 的后半段）。 */
  function waitParsed(acc, opt) {
    var taskId = (acc && acc.task_id) || '';
    if (!taskId) return Promise.reject(new Error('后端没有返回解析任务 ID'));
    return new Promise(function (resolve, reject) {
      var over = false;
      var sub = null;
      function stop() { over = true; if (sub) sub.abort(); }
      // 终态 → 取正文。**必须单独再取一次**：正文不在任务表里
      // （`la_task.result` 只有轻量元信息 —— 几万字的合同正文塞进去会让任务中心
      //  每次刷新都拖着它走，更早的版本就是被它撑爆的：界面上出现半截合同）。
      function fetchText() {
        req('/contract/parse/' + encodeURIComponent(taskId), { timeout: 120000 })
          .then(function (r) { return r.ok ? r.json() : failDetail(r); })
          .then(resolve, reject);
      }
      sub = taskStream(taskId, {
        onFrame: function (name, d) {
          if (over) return;
          if (name === 'progress') {
            // 过程信息只有 `trace[-1].detail` 一句（后端 `_report` 写的），
            // 例如「2/3 已解析：xx.pdf（813 字，缓存命中）」。
            var note = (d && d.latest && d.latest.detail) || '';
            if (note && opt.onNote) opt.onNote(note, d);
            return;
          }
          if (name === 'done') {
            if (d && d.status === 'failed') {
              // 一份都没读出来（例如全部是加密 PDF）。带上后端那句原因 ——
              // "有一份没认出来"和"全部都没认出来"要做的事完全不同。
              stop();
              reject(new Error((d && d.error) || '全部材料都未能解析出文字'));
              return;
            }
            stop();       // 拿到终态就不必再挂着这条流
            fetchText();
            return;
          }
          if (name === 'error') {
            stop();
            reject(new Error((d && d.error) || '解析任务出错'));
          }
        },
        onEnd: function () {
          // 流收尾但没等到 `done`（后端一小时兜底把流关了、或代理截断了缓冲）：
          // 这**不等于**解析完了，只能去取一次结果 —— 取不到就如实说还没好。
          if (over) return;
          over = true;
          fetchText();
        },
        onError: function (e) {
          // 流断了 ≠ 任务死了（后端还在跑，任务表里有进度）。所以**退化为直接取结果**：
          // 跑完了就拿到正文，没跑完就报"还没好，可在任务中心看"。
          //
          // ⛔ 这里绝不能重新提交一次解析：那会变成两份材料、两次模型调用，
          //    而用户只是网络抖了一下。
          if (over) return;
          over = true;
          if (opt.onNote) opt.onNote('进度连接中断，正在直接查询结果…');
          fetchText();
        },
      });
    });
  }

  /** AI 助手页的问答。**走 Agent 编排**（``/agents/assistant/stream``）。
   *
   *  2026-10-07 改：这个页面原先打 ``/chat/rag/stream``（纯检索 + 生成，
   *  一个 Agent 节点都不跑）。侧边栏对外的 4 个入口里只有它和类案检索、文书中心
   *  绕开了编排 —— "4 个 Agent"于是只是界面上的一句话。
   *  现在它和其他三个走同一条 ``orchestrator.execute`` 链路：意图判定、法条检索、
   *  类案检索、本所指引、争议焦点、引用终审、报告汇总，走几步由问题复杂度定。
   *
   *  **页面形态不变**：交付物仍是对话式正文 + 参考法规卡片 + 追问输入行
   *  （后端把报告正文归一化了一份到 ``answer``，见 ``chat.py._persist_agent_turn``），
   *  所以 ``renderAnswer`` / ``remember`` / ``chat.conversationId`` / 历史会话栏
   *  四样全部照旧可用。老端点 ``/chat/rag/stream`` 保留没删。
   *
   *  代价是慢一些、且每问一句都会往任务表写一条记录（任务中心能看到）——
   *  换来的是每条结论都附引用清单与证据缺口。 */
  function ask(text, opt) {
    if (chat.running) { toast('上一次回答还在生成，请稍候或等它结束。'); return; }
    opt = opt || {};
    var box = $('#chatbox');
    if (!box) return;
    chat.running = true;
    var sendBtn = $('#sendBtn'), input = $('#input');
    if (sendBtn) sendBtn.disabled = true;
    if (input) input.disabled = true;
    setAskMoreDisabled(true);
    setConvDisabled(true);

    bubble(text, true);
    var pending = bubble('<div class="tp3"><i></i><i></i><i></i></div>', false);
    var bb = $('.bb', pending);
    var started = Date.now();
    var ansHost = el('div', 'ans');        // 结果渲染区
    bb.appendChild(ansHost);

    stream('/agents/assistant/stream', {
      query: text,
      case_id: opt.caseId || '',
      history: opt.history || chat.thread,
      // 专家人设：从专家页点卡进来时带上 expert_id，后端 AgentAskRequest.expert_id
      // 会把它写进 RunRequest.expert_persona，注入各生成节点的 system 前缀。
      // 空串 = 普通助手（页面未挂任何专家，或用户已点「退出专家」）。
      expert_id: chat.expertId || '',
      // ⚠ 这里**不发 ``top_k``**。它在这条链路上只有一个去处 —— CaseNode 的召回条数，
      //    与"检索几条法条"是两件事（老端点那个 8 指的是后者）。发一个数过去
      //    只会让类案召回跟着变，而页面上的任何一处都看不出这个因果关系。
      //    要控制类案条数的是类案检索页的「返回条数」面板，走它自己的入口。
      // 空串 = 开新会话；后端对"查不到 / 无权限"的号也是当作新会话，不报错
      conversation_id: chat.conversationId,
    }, {
      onFrame: function (ev, data) {
        // 等待期只有气泡里的转圈，没有可报的进度：meta / trace 一概不理，只等 done。
        // （这条链路会推 trace 帧 —— 它是节点级执行过程，执行轨迹面板已经摘掉了，
        //   没人消费；渲染成文字就是"StatuteNode · 法规检索"，等于把面板请回来。）
        //
        // ``decision`` 是**唯一**在等待期显示的东西（约束 4 的「过程中」层）：
        // Agent 自己做的关键决策一句话，由后端单独分流下发（见 chat.py 的 _on_event）。
        // 它替换掉那三个点、done 一到就跟着收走 —— 一句话，不是一块面板。
        if (ev === 'decision') { showDecision(bb, data.text); return; }
        if (ev === 'done') {
          renderAnswer(pending, data, Date.now() - started);
          // 回填会话号：下一句追问要落在同一段会话里。落库失败时后端回空串，
          // 这里跟着归零 —— 下一句另起一段，总好过挂在一个不存在的号上。
          chat.conversationId = data.conversation_id || '';
          remember(text, data);
          finish();
          // 侧栏在 done **之后**刷：后端是先落库再发 done（见 chat.py 的顺序说明），
          // 所以这里一定看得到刚问的这条。用 idle 包一层，别和正在分帧插入的正文抢主线程。
          idle(function () { loadConversations(); });
          return;
        }
        if (ev === 'error') {
          dropSpin(bb);
          ansHost.innerHTML = '<p style="color:#c0392b">回答失败：' + esc(data.error || '未知错误') + '</p>';
          finish();
        }
      },
      onError: function (e) {
        dropSpin(bb);
        ansHost.innerHTML = '<p style="color:#c0392b">连接中断：' + esc(e.message || e) + '，请重试。</p>';
        finish();
      },
      onEnd: function () { finish(); },
    });

    /** 记一轮对话供追问用。**上限 2 轮**，与后端 ``RagTurn`` 的 max_length=4 对齐。
     *
     *  ``answer`` 是后端归一化好的（Agent 链路的 ``report`` 也有一份），
     *  这里再兜一次 ``report`` 是为了"后端哪天不归一了"的时候追问不静默失忆 ——
     *  追问带上空上文，模型会拿一句无指代的问题去检索，召回的是别的条文。 */
    function remember(q, data) {
      chat.thread.push({ q: q, a: (data.answer || data.report || '').slice(0, 400) });
      if (chat.thread.length > 2) chat.thread = chat.thread.slice(-2);
    }

    function finish() {
      if (!chat.running) return;
      chat.running = false;
      if (sendBtn) sendBtn.disabled = false;
      if (input) { input.disabled = false; input.focus(); }
      setAskMoreDisabled(false);
      // **不动 chat.conversationId**：用户还要在同一段会话里继续追问
      setConvDisabled(false);
      // 统计与待办会变，但它是次要内容：等长回答渲染完、浏览器空下来再拉，
      // 别和"正在插入的首屏"抢主线程。
      idle(function () { reloadPage('pg-dash'); });
    }
  }

  /** 「贴材料 → 跑某个意图」那条路（合同审查 / 案件分析）**走完整 Agent 编排**。
   *
   *  **不能复用 ``ask()``。** 那个是 AI 助手输入框的路径，请求体里既没有 ``intent``
   *  也没有 ``material_text``：用户刚粘进来的那份合同会被整段丢掉，然后拿一个
   *  **没看过这份合同**的回答糊弄过去 —— 界面上看不出任何异常，这是法律场景里
   *  比直接报错更坏的结果。
   *
   *  两条路各有各的交付物：这条出**报告模板**（引用清单 / 证据缺口 / 综合置信度），
   *  ``ask()`` 出对话式回答。所以渲染也必须分开 —— 这里用
   *  ``formatStructuredAnswer``，不能用 ``formatRagAnswer``。
   *
   *  ⚠ 它**目前没有调用方**（合同审查页走的是 ``startReview``）。保留是因为
   *    ``tests/test_ui_static.py`` 点名要它在、且失败分支要收转圈；而它带的
   *    ``material_text`` + ``intent`` 是 ``ask()`` 那条链路没有的能力 ——
   *    将来"贴一份材料问一个特定意图"的入口直接用它，别再造第三个。
   *
   *  2026-10-07：``opt.intent`` **现在真的会钉住路由**了。以前它只写进任务表，
   *    IntentNode 无条件重跑分类并覆写意图，传什么都会被盖掉（见
   *    ``orchestrator.RunRequest.intent`` 的说明）。 */
  function askAgent(text, opt) {
    if (chat.running) { toast('上一次分析还在进行，请稍候或等它结束。'); return; }
    opt = opt || {};
    var box = $('#chatbox');
    if (!box) return;
    chat.running = true;
    var sendBtn = $('#sendBtn'), input = $('#input');
    if (sendBtn) sendBtn.disabled = true;
    if (input) input.disabled = true;

    bubble(text, true);
    var pending = bubble('<div class="tp3"><i></i><i></i><i></i></div>', false);
    var bb = $('.bb', pending);
    var started = Date.now();
    var ansHost = el('div', 'ans');        // 结果渲染区
    bb.appendChild(ansHost);

    stream('/chat/stream', {
      query: text,
      case_id: opt.caseId || '',
      intent: opt.intent || '',
      doc_type: opt.docType || 'consult',
      material_text: opt.materialText || '',
      material_paths: [],
    }, {
      onFrame: function (ev, data) {
        if (ev === 'meta') { chat.lastTaskId = data.task_id || ''; return; }
        // 关键决策一句话（约束 4 的「过程中」层），同 ``ask()``
        if (ev === 'decision') { showDecision(bb, data.text); return; }
        if (ev === 'done') {
          // dropTyping 返回的就是上面那个 ansHost（它是 bb 里唯一的 .ans）
          paintChunks(dropTyping(bb), formatStructuredAnswer(data, Date.now() - started));
          finish();
          return;
        }
        if (ev === 'error') {
          dropSpin(bb);
          ansHost.innerHTML = '<p style="color:var(--dg)">分析失败：' + esc(data.error || '未知错误') + '</p>';
          finish();
        }
      },
      onError: function (e) {
        dropSpin(bb);
        ansHost.innerHTML = '<p style="color:var(--dg)">连接中断：'
          + esc(e.message || e) + '（任务可能仍在后端执行，稍后可在「日程任务」查看）</p>';
        finish();
      },
      onEnd: function () { finish(); },
    });

    function finish() {
      if (!chat.running) return;
      chat.running = false;
      if (sendBtn) sendBtn.disabled = false;
      if (input) { input.disabled = false; input.focus(); }
      // 统计与待办会变，但它是次要内容：等长报告渲染完、浏览器空下来再拉，
      // 别和"正在插入的首屏"抢主线程。
      idle(function () { reloadPage('pg-dash'); });
    }
  }

  /** 生成期间禁用所有追问行（连点两次会让两个请求抢同一个 chat.thread）。 */
  function setAskMoreDisabled(disabled) {
    $$('#chatbox .askin').forEach(function (i) { i.disabled = disabled; });
    $$('#chatbox .askgo').forEach(function (b) { b.disabled = disabled; });
  }

  /** 发送一次追问。走 ``ask()``，历史上下文由它自己带上（``chat.thread``）。 */
  function sendFollowUp(wrap) {
    if (!wrap || chat.running) return;
    var input = $('.askin', wrap);
    var v = ((input && input.value) || '').trim();
    if (!v) { if (input) input.focus(); return; }
    input.value = '';
    ask(v);
  }

  /** 点行内法条标签 → 展开参考法规并滚到那张卡。纯前端定位，不发请求。 */
  function jumpToRef(btn) {
    var marker = String(btn.getAttribute('data-marker') || '').replace(/[^A-Za-z0-9]/g, '');
    var msg = btn.closest('.msg');
    var card = msg && $('.ref[data-marker="' + marker + '"]', msg);
    if (!card) return;
    var d = card.closest('details');
    if (d) d.open = true;                   // 折叠着就点不到，先展开
    card.scrollIntoView({ block: 'center', behavior: 'smooth' });
    card.classList.add('on');
    setTimeout(function () { card.classList.remove('on'); }, 1600);
  }

  /* ------------------------------------------------------------ 历史会话（AI 助手页左栏） */
  /** 会话行的时间。与「执行轨迹」页的分组同一套写法：**只切字符串，不经 ``new Date()``**
   *  —— 理由见 ``fmtTime``：后端给的时间戳再做一次时区换算会整体差 8 小时。 */
  function fmtConvTime(iso) {
    var s = String(iso || '');
    if (s.length < 16) return fmtTime(iso);
    var day = s.slice(0, 10), hm = s.slice(11, 16);
    var t = new Date(), pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    var todayStr = t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate());
    var y = new Date(t.getTime() - 86400000);
    var yestStr = y.getFullYear() + '-' + pad(y.getMonth() + 1) + '-' + pad(y.getDate());
    if (day === todayStr) return '今天 ' + hm;
    if (day === yestStr) return '昨天 ' + hm;
    return day.slice(5) + ' ' + hm;
  }

  /* ------------------------------------------------------------ 分页
     两处列表（历史会话 / 案件管理）共用同一套页码式分页：
     - 分页状态对象 {n 当前页, size 每页条数, total 总数, pages 总页数}；
     - 请求显式带 page / page_size；
     - 渲染走 renderPager，按钮点击经 box 一次性委托取共享变量 pagerGo。 */
  var convPage = { n: 1, size: 10, total: 0, pages: 0 };
  var casePage = { n: 1, size: 10, total: 0, pages: 0, type: 'all' };
  var pagerGo = null;

  /** 页码序列生成（纯函数）：总页数 <=7 时平铺全部页码；否则首页/末页必显，
   *  当前页前后各最多 2 个连续页码，断档处插入省略号占位 '...'。
   *  @return 数字页码与 '...' 组成的数组（'...' 由调用方渲染为不可点击占位）。 */
  function buildPagerPages(current, total) {
    total = Math.max(1, total | 0);
    current = Math.min(Math.max(1, current | 0), total);
    if (total <= 7) {
      var all = [];
      for (var i = 1; i <= total; i++) all.push(i);
      return all;
    }
    var lo = Math.max(1, current - 2), hi = Math.min(total, current + 2);
    var set = {};
    set[1] = true; set[total] = true;
    for (var j = lo; j <= hi; j++) set[j] = true;
    var keys = Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
    var out = [];
    for (var k = 0; k < keys.length; k++) {
      if (k > 0 && keys[k] - keys[k - 1] > 1) out.push('...');
      out.push(keys[k]);
    }
    return out;
  }

  /** 通用页码栏：「共 N 条 · M 页」+ 上一页/页码/下一页，当前页高亮。
   *  总页数 <= 1 时整栏隐藏。onGo(p) 是"跳转到第 p 页"的回调，
   *  每次渲染赋给共享变量 pagerGo，容器只在第一次渲染时绑一次委托。 */
  function renderPager(box, st, onGo) {
    if (!box) return;
    st.pages = Math.max(1, Math.ceil((st.total || 0) / st.size));
    if (st.pages <= 1) { box.innerHTML = ''; box.style.display = 'none'; return; }
    box.style.display = '';
    var nums = buildPagerPages(st.n, st.pages).map(function (p) {
      if (p === '...') return '<span class="pgs">...</span>';
      return '<button class="pgn' + (p === st.n ? ' on' : '') + '" data-pg="' + p + '">' + p + '</button>';
    });
    box.innerHTML = '<span class="pginfo">共 ' + st.total + ' 条 · ' + st.pages + ' 页</span>'
      + '<button class="pgbtn" data-pg="' + (st.n - 1) + '"' + (st.n <= 1 ? ' disabled' : '') + '>上一页</button>'
      + nums.join('')
      + '<button class="pgbtn" data-pg="' + (st.n + 1) + '"' + (st.n >= st.pages ? ' disabled' : '') + '>下一页</button>';
    if (!box._pagerBound) {
      box._pagerBound = true;
      box.addEventListener('click', function (e) {
        var b = e.target.closest('[data-pg]');
        if (!b || b.disabled || !pagerGo) return;
        pagerGo(parseInt(b.getAttribute('data-pg'), 10) || 1);
      });
    }
    pagerGo = onGo;
  }

  function loadConversations() {
    var box = $('#convList');
    if (!box) return;
    J('/chat/conversations', { query: { page: convPage.n, page_size: convPage.size } }).then(function (res) {
      var items = res.items || [];
      if (res.total != null) convPage.total = res.total;
      // 删除当前页最后一条后自动回退前一页（只回退一次，防止死循环）
      if (!items.length && convPage.n > 1) {
        convPage.n -= 1;
        loadConversations();
        return;
      }
      renderConversations(box, items);
      renderPager($('#convPager'), convPage, function (p) { convPage.n = p; loadConversations(); });
    }).catch(function (e) {
      box.innerHTML = '<div class="tm" style="padding:12px 0;color:var(--tt)">列表加载失败：'
        + esc(e.message) + '</div>';
    });
  }

  function renderConversations(box, items) {
    if (!items.length) {
      // 文案是设计稿定死的，别自创：它要回答"这东西什么时候会有内容"
      box.innerHTML = '<div class="tm" style="padding:12px 0;color:var(--tt)">'
        + '还没有历史会话。提问之后会自动保存成一段对话。</div>';
      return;
    }
    box.innerHTML = items.map(function (c) {
      var on = c.conversation_id === chat.conversationId;
      var title = c.title || '未命名会话';
      return '<div class="drow' + (on ? ' on' : '') + '" data-conv="' + esc(c.conversation_id)
        + '" title="' + esc(title) + '">'
        + '<span class="tx"><div class="tt">' + esc(title) + '</div>'
        + '<div class="mm">' + esc(fmtConvTime(c.updated_at)) + '</div></span>'
        // 删除用文字按钮：图标表里**没有** cross（`:2591` 那里已经踩过，渲染出来是个空白）
        + '<span class="ops"><button class="btn sm" data-conv-del="'
        + esc(c.conversation_id) + '">删除</button></span></div>';
    }).join('');
  }

  function markActiveConversation(id) {
    $$('#convList .drow').forEach(function (r) {
      r.classList.toggle('on', (r.getAttribute('data-conv') || '') === id);
    });
  }

  /** 打开一段历史会话：把每一轮**完整重渲染**回对话区。
   *
   *  三条不能省的步骤，每条都对应一个静默故障：
   *  1. **必须走 ``renderAnswer`` → ``formatRagAnswer``。** 只有它会为本轮分配新的
   *     ``ansId``；自己拼 HTML 会让正文里的 ``[C1]`` 在 ``CASE_CACHE`` 里取到
   *     **另一段回答的**案例卡 —— 标题对、内容错，而且不报任何错。
   *  2. **必须重建 ``chat.thread``。** 后端 ``rag_answer.resolve_followup`` 拿它改写
   *     检索式；不重建的话下一句追问会拿**上一段会话**的问题去拼接 → 召回错误的法条。
   *  3. **先请求成功再清空 ``#chatbox``。** 反过来的话请求一失败，用户当前正在看的
   *     这段对话就没了。 */
  function replayConversation(id) {
    // 历史会话不区分专家身份（后端未存 expert_id）：打开历史会话一律回到
    // 普通助手视角，避免挂着「专家」banner 看普通会话造成误导。
    delete window.__expert;
    applyExpertContext();
    if (chat.running) { toast('上一次回答还在生成，请稍候或等它结束。'); return; }
    var box = $('#chatbox');
    if (!box) return;
    // 返回 promise：调用方（含测试）能等到请求真的落地，而不是立刻往下走
    return J('/chat/conversations/' + encodeURIComponent(id)).then(function (res) {
      var turns = res.turns || [];
      if (!turns.length) { toast('这段会话没有可显示的内容'); return; }
      box.innerHTML = '';
      turns.forEach(function (p) {
        bubble(p.query || '', true);                  // 用户轮
        var m = bubble('', false);                    // AI 轮（空壳，等 renderAnswer 填）
        renderAnswer(m, p, p.elapsed_ms || 0);
      });
      chat.conversationId = id;
      chat.thread = turns.slice(-2).map(function (p) {
        return { q: p.query || '', a: (p.answer || '').slice(0, 400) };
      });
      markActiveConversation(id);
      stick(box, true);
    }).catch(function (e) { toast('打开会话失败：' + e.message); });
  }

  /** 开一段新会话。**不插开场白** —— 开场白由 ``bindChat`` 插一次，
   *  这里再插一次会让每次「新建」都多一段 AI 自我介绍。 */
  function newConversation() {
    if (chat.running) { toast('上一次回答还在生成，请稍候或等它结束。'); return; }
    var box = $('#chatbox');
    if (box) box.innerHTML = '';
    chat.conversationId = '';
    chat.thread = [];
    markActiveConversation('');
    var input = $('#input');
    if (input) input.focus();
  }

  function delConversation(id) {
    if (chat.running) { toast('上一次回答还在生成，请稍候或等它结束。'); return; }
    return confirmBox('删除这段会话？', '删除后不会再出现在历史列表里（库中保留记录，管理员可查）。', '删除')
      .then(function (yes) {
        if (!yes) return;
        // 同样把 DELETE 的 promise 交出去：这样 `delConversation(...)` 的调用方
        // 等到的是"真的删完了"，而不是"用户点了确认"
        return J('/chat/conversations/' + encodeURIComponent(id), { method: 'DELETE' }).then(function () {
          toast('已删除');
          // 删掉的正是当前正在看的那段：状态要一起归零，否则下一句追问会
          // 落进一段已经从列表消失的会话里
          if (chat.conversationId === id) { chat.conversationId = ''; chat.thread = []; }
          loadConversations();
        }).catch(function (e) { toast('删除失败：' + e.message); });
      });
  }

  /** 生成期间锁住历史栏。
   *
   *  **必须锁。** ``ask()`` 还在跑的时候清空 ``#chatbox``（新建 / 切换会话），
   *  那个"正在生成"的气泡就成了游离节点：``done`` 照样能选到它、把答案写进去，
   *  不报任何错 —— 但屏幕上什么都不会出现，看起来就是答案丢了。 */
  function setConvDisabled(disabled) {
    var btn = $('#convNew');
    if (btn) btn.disabled = disabled;
    var list = $('#convList');
    if (!list) return;
    $$('[data-conv-del]', list).forEach(function (b) { b.disabled = disabled; });
    $$('.drow', list).forEach(function (r) { r.style.pointerEvents = disabled ? 'none' : ''; });
  }

  var INTENT_LABEL = {
    contract_review: '合同审查', case_search: '类案检索', case_analysis: '案件分析',
    doc_draft: '文书起草', statute_lookup: '法规查询', consult: '法律咨询',
    /* 系统按复杂度自动降档的两类，不是律师可选的"业务类型"，但轨迹里要看得懂 */
    quick_answer: '快速问答（轻量路径）', off_topic: '非法律问题（直答）',
  };
  var STATUS_TAG = function (s) {
    var m = {
      pending: ['等待中', 'ne'], running: ['执行中', 'ac'], succeeded: ['已完成', 'ps'],
      degraded: ['降级完成', 'wn'], failed: ['失败', 'dg'],
    }[s] || [s || '未知', 'ne'];
    return tag(m[0], m[1]);
  };
  var TYPE_LABEL = { litigation: '诉讼', contract: '非诉/合同', consult: '咨询' };
  var STAGE_LABEL = { intake: '接案', evidence: '举证', hearing: '开庭', judgment: '判决', closed: '结案', in_progress: '进行中' };
  // in_progress 两边都要有：后端 STAGES 里有它，`_status_of_stage` 又把它当**状态**产出
  // （库里已有这样的行）。少一边，列表页就会原样吐英文 in_progress。
  var CASE_STATUS_LABEL = { active: '在办', suspended: '中止', closed: '已结', archived: '归档', in_progress: '在办' };
  var GAP_LABEL = {
    missing_evidence: '缺少证据', missing_facts: '事实不清', missing_document: '缺少文件',
    unclear_request: '诉求不明', missing_party: '当事人信息不全', missing_clause: '缺少条款',
    // 文书生成侧新加的一类：本案证据链没带上（未关联 / 没材料 / 非诉案件 / 未生成）。
    // 它和上面那些"检索到的依据有缺"不是一回事 —— 缺的是**事实与证据**。
    chain: '证据链未带入',
  };

  function bindChat() {
    // 应用"当前专家"上下文（从专家页点卡进来时生效；普通进入则恢复默认助手）。
    // 事件绑定靠 RENDER 表的 ``rendered`` 守卫只跑一次，这里每次进页都会被
    // ``renderPage`` 调一次 —— 但上下文必须每次重算，不能只应用一次。
    applyExpertContext();
    var input = $('#input'), sendBtn = $('#sendBtn');
    if (sendBtn) sendBtn.addEventListener('click', function () {
      var v = (input.value || '').trim();
      if (!v) { toast('请先输入要分析的问题'); input.focus(); return; }
      input.value = '';
      ask(v);
    });
    if (input) input.addEventListener('keydown', function (e) {
      // 与占位文案一致：Enter 发送、Shift+Enter 换行
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendBtn.click(); }
    });
    $$('.chip[data-q]').forEach(function (c) {
      c.addEventListener('click', function () {
        var q = c.getAttribute('data-q');
        if (input) input.value = q;
        if (sendBtn) sendBtn.click();
      });
    });
    // 会话体里的按钮（复制 / 检索同类案例 / 查看任务 / 修订版 / 行内法条标签 / 追问）
    var box = $('#chatbox');
    if (box) box.addEventListener('click', function (e) {
      if (reviseAction(e)) return;
      var cp = e.target.closest('[data-copy]');
      if (cp) { copyText(ANSWER_CACHE[cp.getAttribute('data-copy')] || ''); return; }
      var run = e.target.closest('[data-run]');
      if (run) {
        // 用最近一条提问当检索式 —— 这是"检索同类案例"最合理的默认值
        var us = $$('#chatbox .msg.me .ub');
        goSearch(us.length ? us[us.length - 1].textContent : '');
        return;
      }
      var tk = e.target.closest('[data-task]');
      if (tk) { openTask(tk.getAttribute('data-task')); return; }
      // 行内类案标签 → 弹出案例详情（参考法规区只放法条，类案没有别的落点）
      var cm = e.target.closest('.rcite[data-case-marker]');
      if (cm) {
        var card = CASE_CACHE[cm.getAttribute('data-case-ans') + ':' + cm.getAttribute('data-case-marker')];
        if (card) { openCaseModal(card); return; }
      }
      // 行内法条标签 → 展开参考法规并滚到那张卡
      var ci = e.target.closest('.rcite[data-marker]');
      if (ci) { jumpToRef(ci); return; }
      if (e.target.closest('.askgo')) { sendFollowUp(e.target.closest('.askmore')); }
    });
    // 追问行里的 Enter（与主输入框一致：Enter 发送、Shift+Enter 不拦）
    if (box) box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.shiftKey) return;
      var wrap = e.target.closest && e.target.closest('.askmore');
      if (!wrap) return;
      e.preventDefault();
      sendFollowUp(wrap);
    });
    // 会话体里的引用/报告数据懒加载：先给一句真实开场白
    J('/cases/stats/summary').catch(function () { return null; }).then(function (cs) {
      var c = cs ? Number(cs.cases || 0) : null;
      var nm = (me().full_name || me().username || '');
      // 这段介绍必须与页面**实际能做的事**一致：这个入口只做法规与类案检索问答，
      // 起草与合同审查在各自的工作区。写着"也能起草审查"会把律师引到死路上。
      bubble('<p>' + esc((nm ? nm + '，' : '') + '我是衡律 AI。')
        + (c !== null ? '已同步你名下 <b>' + c + ' 件在办案件</b>。' : '')
        + '直接问法律问题即可 —— 回答基于法规库与类案库的混合检索，'
        + '正文里带下划线的标签可以点开看对应条文；检索结果里没有的条文，我不会写给你。'
        + '需要起草文书或审查合同，请到对应的工作区。</p>', false);
    });
    // ---- 历史会话栏。**绑在这里而不是 renderPage**：``bindChat`` 有 ``rendered``
    // 守卫（只跑一次），用 ``reloadPage('pg-ai')`` 做"每次进页刷新"会重复绑事件、
    // 重复插上面那句开场白。
    var convNew = $('#convNew');
    if (convNew) {
      // 防的是 body 末尾那个全局 ``[data-toast]`` 委托（``index.html`` 里）：
      // 谁要是照着旁边的「归档到案件」抄一个 data-toast 上去，
      // 这个按钮就会"弹个成功提示但什么都没发生"。
      convNew.removeAttribute('data-toast');
      convNew.addEventListener('click', newConversation);
    }
    var convList = $('#convList');
    if (convList) convList.addEventListener('click', function (e) {
      var del = e.target.closest('[data-conv-del]');
      if (del) { e.stopPropagation(); delConversation(del.getAttribute('data-conv-del')); return; }
      var row = e.target.closest('[data-conv]');
      if (row) replayConversation(row.getAttribute('data-conv'));
    });
    loadConversations();
  }

  /* ------------------------------------------------------------ 案件管理 */
  /* ------------------------------------------------------------ 合同审查（专属页）
     两段式：落地页 ``.ct-land``（**只上传，没有任何输入框**）→ 审查页 ``.ct-run``，
     靠 ``#pg-contract`` 上的 ``.run`` 类切换。两段共用一份状态与同一条 SSE。

     ⚠️ 事件全部挂在 ``bindContractOnce`` 里，``renderContract`` 只按状态刷 DOM。
        这条纪律来自 ``renderPage`` 的 ``rendered[id]`` 守卫：它让 ``renderContract``
        一辈子只跑一次，所以现在把监听器写在里面也不会出事；但哪天有人为了
        "每次进页重置状态"去 ``reloadPage('pg-contract')``，一个没有 bound 守卫的
        ``addEventListener`` 就会重复挂上 —— 表现是**上传一次、解析两遍**。
        ``bindDocEditOnce`` 是同一个套路，这里用自守卫的标志实现同样效果。 */
  var contractState = {
    // 两个框各一份。**两份都单独可用**：只传任一份都能开跑；
    // 两份都在时，待审当被审查的材料、范本当比对基准。
    // ``edited``：左栏合同全文被律师改过。只影响两处显示（文件卡上的"已改"、
    // 左栏的「已修改」标记），**不参与"材料变了没"的判断** —— 那个由
    // ``ctStale()`` 比文本本身，比标志位可靠（改回原样就不该算脏）。
    main: { text: '', name: '', chars: 0, err: '', edited: false },
    tpl: { text: '', name: '', chars: 0, err: '', edited: false },
    // 上一次执行**实际发出去**的那套材料。用来判断"跑完之后手上的材料又变了" ——
    // 变了就说明眼前这份结果不是当前材料跑出来的，结果页要出现重审按钮。
    usedMaterial: '',
    usedTpl: '',
    usedSrc: '',          // 上次拿来当材料的是哪个框：'main' / 'tpl'
    // 审查立场。'' = 中立（甲乙双方风险都提示）。
    party: '',
    // 甲乙方名称。从正文里正则抽出来预填，**可以改** —— 抽错或抽不到时
    // 律师自己填；它会跟着立场一起发给后端（见 ctRoleValue）。
    names: { '甲方': '', '乙方': '' },
    usedRole: '',         // 上次实际发出去的立场串
    parsing: { main: false, tpl: false },
    // 解析进度的那一句话。解析已经挪到后端 worker 里跑，这句是它**逐份上报**
    // 的进度（「2/3 已解析：xx.pdf（813 字）」），不是前端自己编的"正在解析…" ——
    // 一份 40 页的扫描件要跑几分钟，只有真进度才回答得了"它是不是卡住了"。
    parseNote: { main: '', tpl: '' },
    running: false,
  };

  // 立场串的两种形态是同一个东西："乙方" 或 "乙方（李明轩）"。
  // 名称塞进同一个字段而不是单开一个：后端那句提示词本来就在拼
  // ``审查立场：{client_role}``，带上名称正好读成"审查立场：乙方（李明轩）"，
  // 不用为了一个附属信息再穿一层字段。
  var CT_PARTIES = ['甲方', '乙方'];
  // 立场面板上那三张卡。**点哪张就以哪个立场开跑** —— 这是全页唯一的开跑入口，
  // 上传不再自动跑（用户原话："我还没有点击甲方或者乙方，他自己就跑了，
  // 我想我点过之后再跑"）。中立 = 不发 client_role，后端按"中立（双向提示风险）"审。
  var CT_STANCES = ['甲方', '乙方', '中立'];
  var CT_CARDS = { '甲方': '#ctPartyA', '乙方': '#ctPartyB', '中立': '#ctPartyN' };
  // 中立那张没有名称框 —— 没有当事人可写名字。
  var CT_NAMEBOX = { '甲方': '#ctPartyAName', '乙方': '#ctPartyBName' };

  // 两个上传框结构一样，只是写到不同的 state 槽位、走不同的 file input。
  // **不共用一个 input**：共用了 ``ctParse`` 就无从知道这份文件该落哪个框。
  var CT_BOXES = {
    main: { card: '#ctCardMain', file: '#ctFileMain', st: '#ctMainSt', x: '#ctMainX' },
    tpl: { card: '#ctCardTpl', file: '#ctFileTpl', st: '#ctTplSt', x: '#ctTplX' },
  };
  // 与后端 ``settings.MAX_PARSE_MB`` 对齐（后端也拦一道，超限落 ``files[].ok=false``）。
  // 这里拦是为了不让 50M+ 的文件白传一趟、再等一个"超过上限"。
  var CT_MAX_MB = 50;

  function ctBox(kind) {
    var c = CT_BOXES[kind];
    return { card: $(c.card), file: $(c.file), st: $(c.st), x: $(c.x) };
  }

  /** 画一个框。**唯一的绘制入口** —— 改完 state 调它，不要在别处直接改 DOM。 */
  function ctPaint(kind) {
    var e = ctBox(kind), s = contractState[kind];
    if (!e.card) return;
    var has = !!s.text;
    e.card.classList.toggle('on', has);
    e.card.classList.toggle('busy', !!contractState.parsing[kind]);
    if (e.x) e.x.hidden = !has;
    if (!e.st) return;
    // 解析不在这个请求里跑了（后端入队、worker 执行），所以这里显示的是
    // **后端逐份上报的进度**；它还没上报到第一份时退回"正在解析…"。
    if (contractState.parsing[kind]) {
      e.st.className = 'st';
      e.st.textContent = contractState.parseNote[kind] || '正在解析…';
      return;
    }
    if (s.err) { e.st.className = 'st bad'; e.st.textContent = s.err; return; }
    if (has) {
      e.st.className = 'st ok';
      // 「已改」：左栏改过正文后字数与文件本身对不上了，标出来免得律师
      // 以为解析变了。状态行里不写"已修改"三个字，那颗 ✕ 已经占了右边。
      e.st.textContent = '✓ ' + s.name + ' · ' + s.chars + ' 字' + (s.edited ? ' · 已改' : '');
      return;
    }
    e.st.className = 'st';
    e.st.textContent = '';
  }

  /** 现在这一刻「应该发什么」。
      有待审 → 待审当材料、范本当基准；只有范本 → **范本自己当材料**（没有可比的基准，
       ``tpl`` 于是为空）。这条规则放在前端，是为了不动后端那条
      「范本单独存在不算有材料、仍须 400」的判定（它有自己的测试钉着），
      后端只管"发过来什么就审什么"。 */
  function ctResolve() {
    if (contractState.main.text) {
      return { src: 'main', material: contractState.main.text,
               chars: contractState.main.chars, tpl: contractState.tpl.text || '',
               edited: contractState.main.edited };
    }
    if (contractState.tpl.text) {
      return { src: 'tpl', material: contractState.tpl.text,
               chars: contractState.tpl.chars, tpl: '',
               edited: contractState.tpl.edited };
    }
    return { src: '', material: '', chars: 0, tpl: '', edited: false };
  }

  /** 手上的材料跟上次跑的那套是不是不一样了。没跑过就谈不上"变"。 */
  function ctStale() {
    if (!contractState.usedSrc) return false;
    var r = ctResolve();
    return r.material !== contractState.usedMaterial || r.tpl !== contractState.usedTpl;
  }

  /** 要发给后端的立场串：``''`` / ``"乙方"`` / ``"乙方（李明轩）"``。
      名称可有可无 —— 它在正文里本来就有，带上只是让提示词读着更明确，
      律师改了名称也能立刻生效（不必回合同里改）。 */
  function ctRoleValue() {
    var p = contractState.party;
    // 中立与"还没选"都返回空串：后端拿到空 client_role 就走"中立（双向提示风险）"，
    // 不需要一个叫"中立"的立场串 —— 那是提示词里本来就有的说法，别自造一个。
    if (!p || p === '中立') return '';
    var n = (contractState.names[p] || '').trim();
    return n ? p + '（' + n + '）' : p;
  }

  /** 从正文里尽力抽甲乙方名称。抽不到就留空，不猜 —— 猜错的名字会被律师当真。 */
  function ctGuessParties(text) {
    var out = {};
    for (var i = 0; i < CT_PARTIES.length; i++) {
      var p = CT_PARTIES[i];
      // 要求"甲/乙方"后面紧跟冒号才算："甲方根据工作需要"这种不会误命中。
      // 中间只容许全角空格与一个括号说明（如"甲方代表人（委托人）："里的
      // "代表人"不匹配，于是会跳到签名区那条真正的"甲方："上去）。
      var re = new RegExp(p + '[\\s\\u3000]*(?:（[^）]{0,12}）)?[\\s\\u3000]*[：:]'
                        + '[\\s\\u3000]*([^\\s\\u3000，,。；;、\\n]{2,40})');
      var m = re.exec(text || '');
      out[p] = m ? m[1].trim() : '';
    }
    return out;
  }

  /** 画立场面板：三张卡片 + 重审按钮。 */
  function ctPaintParty() {
    CT_STANCES.forEach(function (p) {
      var card = $(CT_CARDS[p]);
      if (card) card.classList.toggle('on', contractState.party === p);
      var sel = CT_NAMEBOX[p];
      if (!sel) return;                       // 中立那张没有名称框
      var inp = $(sel);
      // 正在输入时不覆盖，否则光标会跳回开头
      if (inp && document.activeElement !== inp && inp.value !== contractState.names[p]) {
        inp.value = contractState.names[p];
      }
    });
    var go = $('#ctPartyGo');
    if (go) {
      var role = ctRoleValue();
      // 只在"选的立场跟这次跑用的不是同一个"时出现 —— 没变就没必要亮个
      // 按下去等于白跑一遍（还可能撞上缓存）的按钮。
      var stale = !!ctResolve().material && !!contractState.usedSrc && role !== contractState.usedRole;
      go.hidden = !stale;
      if (stale) go.textContent = '按' + (contractState.party || '中立') + '立场重审';
    }
    var hint = $('#ctPartyHint');
    if (hint) {
      // 「中立」不能走 ctRoleValue() —— 它返回的是空串，拼进去就成了
      // "风险点将按的立场判定"，少一个词而页面上不报任何错。
      if (!contractState.party) {
        hint.textContent = '点一方开始审查。选「中立」则甲乙双方的风险都提示。';
      } else if (contractState.party === '中立') {
        hint.textContent = '中立：甲乙双方的风险都提示，不替任何一方说话。';
      } else {
        hint.textContent = '风险点将按' + ctRoleValue() + '的立场判定，修改建议也替这一方争取。';
      }
    }
  }

  /** 重审按钮的文案。说清"重跑一次会变成什么"，别只写「重新审查」让人猜。 */
  function ctRetryHint(r) {
    if (contractState.usedSrc === 'tpl' && r.src === 'main') {
      return { label: '改用待审合同重审',
               title: '刚才只审查了范本这一份，现在改用待审合同作为被审查对象重跑' };
    }
    if (!contractState.usedTpl && r.tpl) {
      return { label: '带范本重审', title: '把范本合同作为比对基准，重新审查一次' };
    }
    return { label: '重新审查', title: '材料变了，用当前这套材料重新跑一次' };
  }

  /** 按 state 刷落地页的可变部分：两个框、底部按钮与说明、以及重审按钮。 */
  function ctReflow() {
    ctPaint('main');
    ctPaint('tpl');
    var r = ctResolve();
    var btn = $('#ctStart');
    if (btn) btn.disabled = !r.material || contractState.running;
    var note = $('#ctNote');
    if (note) {
      note.textContent = contractState.running ? '审查进行中…'
        : (!r.material ? '上传任意一份合同后进入审查页，选好立场再开始'
         : (r.src === 'tpl' ? '已就绪：当前只有范本这一份，将直接审查它'
                            : '已就绪：点「进入审查」回到审查页选立场，或直接换一份文件'));
    }
    // 重审按钮只在"手上的材料跟这次跑的不是同一套"时出现 —— 跑完才补传、
    // 跑完又换了一份、以及只传范本跑完之后又传了待审，三种都算。
    var rt = $('#ctRetpl');
    if (rt) {
      // 前面那个 ``!!r.material``：把框都清空也算"材料变了"，但那时点重审只会
      // 弹一句"请先上传合同文件"—— 亮一个按下去必然失败的按钮没有意义。
      var stale = !!r.material && ctStale();
      rt.hidden = !stale;
      if (stale) {
        var h = ctRetryHint(r);
        var tx = $('#ctRetplTx');
        if (tx) tx.textContent = h.label;
        rt.title = h.title;
      }
    }
    ctPaintParty();
  }

  /** 顶栏那行元信息：这份结果到底是用什么跑出来的。 */
  function ctMetaText() {
    var parts = [];
    // 被审查的那一份取自哪个框，就按那个框的口径称呼它 —— 只传范本时
    // 它是"被审查的合同"，不该在这里还叫范本，否则跟下面那行的范本撞名。
    var src = contractState.usedSrc === 'tpl' ? contractState.tpl : contractState.main;
    if (contractState.usedSrc && src.name) {
      parts.push((contractState.usedSrc === 'tpl' ? '合同：' : '待审：')
                 + src.name + '（' + src.chars + ' 字）');
    }
    if (contractState.usedTpl) {
      parts.push('范本：' + (contractState.tpl.name || '已按范本比对'));
    } else if (contractState.usedSrc === 'main' && contractState.tpl.name) {
      parts.push('范本：' + contractState.tpl.name + '（未参与本次）');
    }
    // 立场要写进顶栏：结论是"站在谁那边"审出来的，光看风险点分不出来 ——
    // 同一条款两种立场给出的建议**完全相反**，不标出来会被当成审错了。
    parts.push('立场：' + (contractState.usedRole || '中立'));
    return parts.join('　·　');
  }

  /* ------------------------------------------------- 左栏：合同全文（可编辑）

     后端 ``/contract/parse`` 给的是**按 PDF 版面硬换行**的纯文本：一个自然段被
     切成「…同意签订本」/「合同，共同遵守本合同所列条款。」两行，中间还夹着
     解析器留下的 ``<!-- 第 1 页 -->`` 与孤立的页脚页码。原来直接塞进 ``<pre>``
     —— 读得出内容，但不像一份合同，也没法改。这里把它排成文稿并开放编辑。

     ⛔ **只做版式分类，一个字都不改**：``ctDocBlocks`` 只把续行接到上一块后面，
     块的顺序与文字原样保留；``ctReadDoc`` 按同一套块把文本还原回去。
     这条往返必须成立 —— 字数徽标的口径就是后端 ``count_chars``，
     少一个字符，律师改完就会看见"我什么都没改，字数怎么就少了"。 */

  /** 正文字数：与后端 ``count_chars``（``chat.py``）**同一口径** ——
   *  不计空白、不计 ``_``/``＿``/``□``。
   *
   *  ⛔ 别退回 ``text.length``：这个项目为此栽过（同一张扫描件每次识别的翻页
   *  空白与填空线长度都不同，字数每次都不一样，极差 14）。改后端口径时这里要一起改。 */
  function countChars(t) {
    return ((t || '').replace(/[\s_＿□]/g, '')).length;
  }

  var CT_RE_CLAUSE = /^第[一二三四五六七八九十百千零〇两0-9]+条/;
  var CT_RE_CHAPTER = /^[一二三四五六七八九十]+[、.]/;
  var CT_RE_PAGE = /^<!--\s*第\s*(\d+)\s*页\s*-->$/;
  var CT_RE_PAGENUM = /^\d{1,3}$/;
  var CT_RE_HEAD = /^【.*】$/;
  var CT_RE_SIGN = /^(甲方|乙方|丙方|用人单位|劳动者|出租人|承租人|买受人|出卖人|地址|住所|联系电话|电话|盖章|签名|日期)[\s　]*[：:]/;
  // 页面水印行。样例合同每页底部压着一行「示例文件 · 无法律效力 · 仅用于产品
  // 演示 · DEMO SAMPLE」，落款处还有一枚「样例章 · 无效」—— PDF 转文本时被
  // 当成正文行夹了进来。它们不是条款，律师看合同不该看见这个。
  // ⛔ **只认这几个人工水印标记，别放宽成"含 仅供/参考"这类泛词**：
  //    真合同里「本文件仅供…参考」是常见措辞，误删等于替当事人改了合同。
  var CT_RE_NOISE = /示例文件|样例章|无法律效力|仅用于产品演示|DEMO\s*SAMPLE/;
  // 解析器插进正文的**图片占位符行**：``[IMAGE_001]``，回插语义后长成
  // ``[IMAGE_001: 红色圆形公章，中心为五角星图案，外圈文字沿圆周排列，内容为"…"]``
  // （见 ``backend/parser/text_extractor.py``）。那是解析管道的东西 ——
  // 合同里没有这一行，摆在"合同全文"里只会让人以为真写了这么一句话。
  // ⛔ **只认图片占位符，不碰 ``[TABLE_00N: …]``**：表格那块回插的是**真数据**
  //    （工资表、清单…），删掉等于把当事人的材料吃了。
  var CT_RE_PLACEHOLDER = /^\s*\[IMAGE_\d{3}(?::.*)?\]\s*$/;
  // 落款日期：**整行就是一个日期**（「2024 年 2 月 28 日」「二〇二四年二月二十八日」）。
  // 句子中间嵌的日期（"合同期自 2024年3月1日 起…"）不会命中 —— 那些留在条款里。
  var CT_RE_DATE = /^[0-9〇零一二三四五六七八九十]{2,4}\s*年\s*[0-9〇零一二三四五六七八九十]{1,3}\s*月\s*[0-9〇零一二三四五六七八九十]{1,3}\s*日。?$/;
  // 句末标点：上一行以它收尾就说明这段话写完了，下一行是新的一段。
  // **少了这条整份合同会被接成一坨** —— PDF 是按版面宽度断行的，
  // 光看"行"分不出"断行"与"换段"。
  var CT_RE_END = /[。；：！？”』」）)\]]$/;

  /** 把这几种**看着像正文、其实不属于合同**的行摘掉：
   *  ``CT_RE_NOISE`` 页面水印、``CT_RE_PLACEHOLDER`` 图片占位符。
   *  解析回来先过这一道，后面切块/回收/字数/送审材料看到的都是同一份干净正文。
   *
   *  ⛔ 摘掉的行**不进 data-mark**（跟分页线不一样）：分页线是解析器标的结构，
   *  本来就在材料里、也计进字数；水印与占位符是解析管道留下的东西，没有任何
   *  留着的理由。所以这里连字数一起减掉（见 ``ctParse``）—— 不然徽标会把它们
   *  算进合同字数。
   */
  function ctStripNoise(text) {
    var lines = String(text || '').split('\n');
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      if (CT_RE_NOISE.test(lines[i])) continue;
      if (CT_RE_PLACEHOLDER.test(lines[i])) continue;
      out.push(lines[i]);
    }
    return out.join('\n');
  }

  /** 把合同正文切成文稿块：``[{kind, text, page?}]``。
   *
   *  kind：``head`` 文件名头 / ``title`` 大标题 / ``chapter`` 章 / ``clause`` 条 /
   *  ``para`` 正文段 / ``sign`` 落款 / ``date`` 落款日期 / ``page`` 分页 /
   *  ``pagenum`` 孤立页码。
   */
  function ctDocBlocks(text) {
    var lines = String(text || '').split('\n');
    var out = [];
    var titleLeft = true;   // 只有**第一个**内容块够格当大标题，免得正文里的短句被标题化
    function prev() { return out.length ? out[out.length - 1] : null; }
    function mergeable(p) {
      if (!p) return false;
      if (p.kind !== 'para' && p.kind !== 'clause' && p.kind !== 'sign') return false;
      return !CT_RE_END.test(p.text);
    }
    for (var i = 0; i < lines.length; i++) {
      // 行尾空白不参与判断（也不留进块里）：它不计字数，但会把"这话说完了吗"
      // 的判断带偏 —— 上一行末尾一个空格，下一行就会被当成新段落。
      var ln = lines[i].replace(/[\s　]+$/, '');
      if (!ln.replace(/[\s　]/g, '')) continue;          // 空行 = 段落边界
      // 水印行**在合并之前**就丢掉：它上面那行（落款日期）不以句号收尾，
      // 留着就会被接进日期那一段，屏幕上变成"2024 年 2 月 28 日示例文件 · …"。
      if (CT_RE_NOISE.test(ln)) continue;
      // 图片占位符同理（不丢的话会被接进上一条的正文里，看上面 3 行）。
      if (CT_RE_PLACEHOLDER.test(ln)) continue;
      var m = CT_RE_PAGE.exec(ln);
      if (m) { out.push({ kind: 'page', text: ln, page: m[1] }); continue; }
      if (CT_RE_HEAD.test(ln) && ln.length <= 40) { out.push({ kind: 'head', text: ln }); continue; }
      if (CT_RE_PAGENUM.test(ln)) { out.push({ kind: 'pagenum', text: ln }); continue; }
      var kind;
      if (CT_RE_CHAPTER.test(ln)) kind = 'chapter';
      else if (CT_RE_CLAUSE.test(ln)) kind = 'clause';
      // 日期排在大标题之前判：大标题那条只看"够短且没有句号"，
      // 落在正文最前面的一个日期会被它抢走。日期独占一行，也不并入上一段
      //（下面的续行只接 para）—— 它是落款的一部分，要单独右对齐。
      else if (CT_RE_DATE.test(ln)) kind = 'date';
      else if (titleLeft && ln.length <= 20 && ln.indexOf('。') < 0) kind = 'title';
      else kind = CT_RE_SIGN.test(ln) ? 'sign' : 'para';
      titleLeft = false;
      // 续行：只有普通段才往上接（条款/章标题自己起头）。
      // 接的时候**不加空格** —— 断行处的空格本来就在上一行的行尾（已剥掉）。
      if (kind === 'para' && mergeable(prev())) { prev().text += ln; continue; }
      out.push({ kind: kind, text: ln });
    }
    // 孤立的页脚页码（整行只有一个 "1"）**并进它上面那条分页线**：它本来就是那一页
    // 的页脚，单画一行会在正文中间留下一个孤零零的数字 —— 用户嫌过这个。
    // 并进去之后由 ``foot`` 带着，``ctReadDoc`` 照样原样吐出来（它计进字数）。
    var folded = [], lastPage = null;
    for (var k = 0; k < out.length; k++) {
      if (out[k].kind === 'pagenum' && lastPage && !lastPage.foot) {
        lastPage.foot = out[k].text;
        continue;
      }
      folded.push(out[k]);
      if (out[k].kind === 'page') lastPage = out[k];
    }
    return folded;
  }

  function ctBlockHTML(b) {
    // 分页：原始标记 ``<!-- 第 1 页 -->`` 留在 ``data-mark`` 里给 ctReadDoc 回收，
    // 屏幕上只画一条分页线与页号。**那串标记是计进字数的**（后端只排除空白与
    // ``_＿□``），所以它必须能被原样收回去，不能只是"看起来消失了"。
    if (b.kind === 'page') {
      // ``data-foot``：并进来的那行页脚页码。屏幕上**只画一个页号**（分页线本来
      // 就在说"第 N 页"），回收时它还要原样吐回去。
      return '<div class="ct-p ct-sep" contenteditable="false" data-mark="' + esc(b.text) + '"'
        + (b.foot ? ' data-foot="' + esc(b.foot) + '"' : '') + '>第 '
        + esc(b.page || b.foot || '') + ' 页</div>';
    }
    // 没并进分页线的页脚页码（上一块不是分页线，比如中间隔了张图）：单独画一行
    // 小字页号，标记同样留在 ``data-mark`` 里 —— 屏幕上不出现裸数字。
    if (b.kind === 'pagenum') {
      return '<div class="ct-p ct-pagenum" contenteditable="false" data-mark="' + esc(b.text) + '">第 '
        + esc(b.text) + ' 页</div>';
    }
    return '<div class="ct-p ct-' + b.kind + '">' + esc(b.text) + '</div>';
  }

  /** 把正文画进左栏。**幂等**：正在显示的就是这份文本时一个字都不动。
   *
   *  ⛔ 不能无条件重绘：``ctEnter()`` 每次上传后、``startReview()`` 每次重审前
   *  都会走到这里，无条件重绘会把律师刚改的字冲掉 —— 而且界面一切正常、
   *  没有任何报错（本项目在文书编辑页踩过同一个坑）。 */
  function ctPaintDoc(text) {
    var doc = $('#ctSrcDoc');
    if (!doc) return;
    text = text || '';
    if (doc._src === text) return;
    doc._src = text;
    var blocks = ctDocBlocks(text);
    var html = '';
    for (var i = 0; i < blocks.length; i++) html += ctBlockHTML(blocks[i]);
    doc.innerHTML = html;
    ctSyncDocBadge();
  }

  /** 把左栏 DOM 还原成文本。**按块取**，不是 ``paper.innerText`` ——
   *  分页块屏幕上只有"第 1 页"，原始标记在 ``data-mark`` 里，innerText 拿不到。 */
  function ctReadDoc() {
    var doc = $('#ctSrcDoc');
    if (!doc) return '';
    var out = [];
    for (var i = 0; i < doc.children.length; i++) {
      var c = doc.children[i];
      if (c.hasAttribute && c.hasAttribute('data-mark')) {
        out.push(c.getAttribute('data-mark') || '');
        // 并进分页线的那行页脚页码要挨着标记吐回来 —— 它就是原文的下一行。
        if (c.hasAttribute('data-foot')) out.push(c.getAttribute('data-foot') || '');
      }
      else out.push((c.innerText || '').replace(/[\s　]+$/, ''));
    }
    return out.join('\n');
  }

  /** 字数徽标 + 「已修改」标记。取的是**当前正文**，所以没改之前它正好
   *  等于后端给的那个数（同一口口径），改完之后是真的新数字。 */
  function ctSyncDocBadge() {
    var r = ctResolve();
    var dc = $('#ctDocChars');
    if (dc) dc.textContent = r.chars + ' 字';
    var d = $('#ctDocDirty');
    if (d) d.hidden = !r.edited;
  }

  /** 编辑落回 state。写回的是**这一刻真正当材料的那一份**（``ctResolve().src``）
   *  —— 只传了范本时，左栏显示的就是范本那份，改的也是它。 */
  function ctDocCommit() {
    var doc = $('#ctSrcDoc');
    if (!doc) return;
    var r = ctResolve();
    if (!r.src) return;
    var t = ctReadDoc();
    if (t === r.material) return;           // 只是点了点、挪了光标，没真的改
    doc._src = t;
    contractState[r.src].text = t;
    contractState[r.src].chars = countChars(t);
    contractState[r.src].edited = true;
    ctSyncDocBadge();
    ctReflow();       // ctStale() 随之成立 → 顶栏出现「重新审查」
  }

  function bindContractDoc() {
    var doc = $('#ctSrcDoc');
    if (!doc || doc._bound) return;
    doc._bound = true;
    var timer = null;
    doc.addEventListener('input', function () {
      // 防抖 300ms：每敲一个字就重算一次字数并刷顶栏，长合同里是白烧的。
      if (timer) clearTimeout(timer);
      timer = setTimeout(ctDocCommit, 300);
    });
    // 粘贴只收纯文本：网页带过来的字体/颜色/链接会绕过 ctReadDoc 的还原口径
    // （它按块取 innerText），而这一栏是**要当材料发出去**的正文。
    doc.addEventListener('paste', function (e) {
      if (!e.clipboardData) return;
      var t = e.clipboardData.getData('text/plain');
      if (t === null || t === undefined) return;
      e.preventDefault();
      var html = String(t).replace(/\r\n/g, '\n').split('\n').map(function (ln) {
        return ln ? esc(ln) : '<br/>';
      }).join('<br/>');
      try { document.execCommand('insertHTML', false, html); } catch (err) { /* 不支持就算了 */ }
      if (timer) clearTimeout(timer);
      timer = setTimeout(ctDocCommit, 0);
    });
  }

  /* ------------------------------------------------------- 右栏：逐条风险卡 */

  // 等级文案与配色。配色沿用全站既有的 ``.risk-high/medium/low``（index.html）。
  var CT_LEVEL = {
    high: { cls: 'risk-high', text: '高（必须改）' },
    medium: { cls: 'risk-medium', text: '中（建议改）' },
    low: { cls: 'risk-low', text: '低（可斟酌）' }
  };

  // 双模型交叉复核的卡片角标：``r.review`` 由复审模型判定（review_node._second_pass）。
  // confirmed 也标出来 —— "这条被第二模型核过、维持原判"本身就是有分量的信息，
  // 不标的话律师分不清"核过"和"压根没复核"。
  var CT_REVIEW_TAG = {
    confirmed: ['ps', '已复核'],
    downgraded: ['wn', '复审降级'],
    added: ['ac', '复审补充']
  };

  /** 逐条风险卡：一条一卡。律师是拿着这几张卡回头改合同的，
   *  原文、问题、建议必须分得清（后端在 ``risk_points`` 里就是分开给的）。
   *
   *  ``clause_text`` 直接用后端那份 —— 它是 ``review_node._filter_by_evidence``
   *  用**拆条原文权威回填**的，不是模型回抄的片段（模型回抄的会截断、走样）。
   */
  /** 卡片里那段「原文」要干净。后端回填的 ``clause_text`` 是**按位置切**的，
   *  跨页的条款会把解析器留下的东西一起带进来：``<!-- 第 3 页 -->``、孤立的
   *  页脚页码「3」、图片占位符 ``[IMAGE_001: 红色圆形公章…]``。
   *  它们在条款原文里本就不存在，摆在律师眼前只会让人以为合同真写了这几行
   *  （用户 2026-09-29 点名的就是这个）。
   *
   *  只清**整行**是这些标记的；句中出现的原样留着。 */
  function ctCleanQuote(t) {
    var lines = String(t || '').split('\n');
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].replace(/[\s　]+$/, '');
      if (CT_RE_PAGE.test(ln)) continue;
      if (CT_RE_PAGENUM.test(ln)) continue;
      if (CT_RE_NOISE.test(ln)) continue;
      if (CT_RE_PLACEHOLDER.test(ln)) continue;
      out.push(ln);
    }
    return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  function ctRiskCards(rps) {
    return rps.map(function (r) {
      var lv = CT_LEVEL[r.level] || CT_LEVEL.medium;
      var rt = CT_REVIEW_TAG[r.review];
      var head = [r.clause_no || '', r.clause_title || ''].filter(Boolean).join(' ');
      var quote = ctCleanQuote(r.clause_text);
      var h = ['<div class="ct-risk">',
        '<div class="ct-risk-h"><span>' + esc(head || '全文') + '</span>',
        '<span class="lv ' + lv.cls + '">' + esc(lv.text) + '</span>',
        (rt ? '<span class="tag ' + rt[0] + '"><i></i>' + esc(rt[1]) + '</span>' : ''),
        '</div>'];
      if (quote) h.push('<div class="ct-risk-q">' + esc(quote) + '</div>');
      if (r.risk) h.push('<p class="ct-risk-r"><b>风险：</b>' + esc(r.risk) + '</p>');
      if (r.suggestion) h.push('<p class="ct-risk-s"><b>建议：</b>' + esc(r.suggestion) + '</p>');
      if (r.review_note) h.push('<p class="ct-risk-b">复审意见：' + esc(r.review_note) + '</p>');
      var basis = (r.basis || []).filter(Boolean);
      // 依据被后端过滤空时如实写"需补充"，别让卡片看起来像"没有依据也成立"
      h.push('<div class="ct-risk-b">依据：'
        + esc(basis.length ? basis.join('；') : '（需补充法律依据）') + '</div></div>');
      return h.join('');
    }).join('');
  }

  /** 上传 → 解析 → 写入 state。``kind`` 是 ``main`` / ``tpl``。 */
  function ctParse(kind, files) {
    if (contractState.parsing[kind] || !files || !files.length) return;
    var list = [];
    for (var i = 0; i < files.length; i++) {
      if (files[i].size > CT_MAX_MB * 1024 * 1024) {
        contractState[kind].err = '「' + files[i].name + '」超过 ' + CT_MAX_MB + 'M 上限';
        ctPaint(kind);
        ctReflow();
        toast('文件超过 ' + CT_MAX_MB + 'M 上限，请压缩或拆分后再上传');
        return;
      }
      list.push(files[i]);
    }
    contractState.parsing[kind] = true;
    contractState.parseNote[kind] = '';
    contractState[kind].err = '';
    ctPaint(kind);
    ctReflow();

    // 解析在**后端 worker** 里跑（一份 40 页的扫描件要几分钟），这个请求不再
    // 等它跑完 —— 所以 `req()` 那条 20 秒超时不会再误伤它。而"解析失败：
    // signal is aborted without reason"的根源，正是那条超时把等待的请求掐了。
    parseUpload(list, {
      // 受理结果：哪几份收下、哪几份当场退回（类型不支持 / 空文件 / 超 50M）。
      // 退回的必须当场说清楚，否则用户会以为"传上去了，只是还没解析完"。
      onAccepted: function (acc) {
        var bad = (acc && acc.rejected) || [];
        if (bad.length) {
          toast(bad.map(function (x) {
            return '「' + x.name + '」' + (x.error || '未受理');
          }).join('；'));
        }
      },
      // 后端逐份上报的进度（"2/3 已解析：xx.pdf（813 字）"）。左卡状态行显示的
      // 就是这个字符串 —— 前端不自己估百分比，估出来的数字只会让人更焦虑。
      onNote: function (text) {
        contractState.parseNote[kind] = text;
        ctPaint(kind);
      },
    })
      .then(function (res) {
        var good = (res.files || []).filter(function (f) { return f.ok && f.text; });
        if (!good.length) {
          var why = (res.files || []).map(function (f) { return f.error; }).filter(Boolean)[0];
          throw new Error(why || '未从文件中识别出可审查文本');
        }
        // ⛔ 字数一律用后端给的 ``chars``（不计空白），**不要退回 ``f.text.length``**：
        //    同一张扫描件每次识别出的换行/空格/填空线长度都不同，text.length 会
        //    每次给出不同数字（实测同一张图跑 6 次得 6 种，极差 14 字）；
        //    chars 的极差是 1，剩下那 1 个字是模型确实读错了字，该露出来而不是抹平。
        var s = contractState[kind];
        // 水印行先摘掉（见 ``CT_RE_NOISE``）：材料、切块、回收、字数看到的是同一份
        // "没有水印的合同"。**只减去摘掉的那几个字**，其余仍以后端 ``chars`` 为准。
        var raw = (res.merged || '').trim();
        var clean = ctStripNoise(raw);
        var dropped = countChars(raw) - countChars(clean);
        s.text = clean;
        s.chars = (typeof res.merged_chars === 'number')
          ? Math.max(0, res.merged_chars - dropped)
          : Math.max(0, good.reduce(function (a, f) { return a + (f.chars || 0); }, 0) - dropped);
        s.name = good.length === 1 ? good[0].name : (good.length + ' 份文件');
        s.err = '';
        // 顺手把甲乙方名称抽出来预填立场面板。**抽不到就保留现状** ——
        // 抽失败时清空会把律师手填的名字一起抹掉，那比不预填更烦人。
        var guess = ctGuessParties(s.text);
        CT_PARTIES.forEach(function (p) {
          if (guess[p]) contractState.names[p] = guess[p];
        });
        toast('已解析 ' + good.length + ' 份文件，共 ' + s.chars + ' 字');
      })
      .catch(function (e) {
        var s = contractState[kind];
        s.text = '';
        s.name = '';
        s.chars = 0;
        s.err = '解析失败：' + (e.message || e);
        toast('文件解析失败：' + (e.message || e));
      })
      .then(function () {
        contractState.parsing[kind] = false;
        contractState.parseNote[kind] = '';
        ctPaint(kind);
        // **不自动开跑**：开跑的唯一入口是点立场卡。解析好了就切到审查页 ——
        // 左边先把合同全文摆出来，让人读完再决定站哪一方。
        // 解析失败**不切段**：切过去也是一片空白，还让人以为跑起来了。
        // 跑到一半又补传一份也不打断：这一趟发出去的材料在 startReview 里已定死，
        // 后补的那份由重审按钮消费。
        if (contractState[kind].text && !contractState.running) {
          ctEnter();
        } else {
          ctReflow();
        }
      });
  }

  /** 切到审查页但**不跑**，停在"等律师选立场"这一刻。
      左边摆合同全文，右边三张立场卡 —— 点哪张才开始跑（绑定见 ``bindContractOnce``）。 */
  function ctEnter() {
    var r = ctResolve();
    if (!r.material) { ctReflow(); return; }
    $('#pg-contract').classList.add('run');
    var meta = $('#ctMeta');
    if (meta) meta.textContent = ctReadyText(r);
    // 左栏按文稿排版（不再是 <pre> 里那坨纯文本）。ctPaintDoc 幂等 ——
    // 已经显示这一份就不动 DOM，律师改过的字不会被这次切页冲掉。
    ctPaintDoc(r.material);
    ctSyncDocBadge();
    ctReflow();
    ctIdle();
  }

  /** 还没跑过时顶栏那行：说的是"手上有什么"，不是"跑的是什么"。
      跑起来之后由 ``ctMetaText()`` 接手（那份要标出实际发出去的立场）。 */
  function ctReadyText(r) {
    var s = r.src === 'tpl' ? contractState.tpl : contractState.main;
    var parts = [(r.src === 'tpl' ? '合同：' : '待审：') + (s.name || '')
                 + '（' + r.chars + ' 字）'];
    if (r.tpl) {
      parts.push('范本：' + (contractState.tpl.name || '已就绪'));
    } else if (r.src === 'main' && contractState.tpl.name) {
      parts.push('范本：' + contractState.tpl.name + '（未参与本次）');
    }
    parts.push('请选择审查立场');
    return parts.join('　·　');
  }

  /** 结果区占位。**空着会让人以为页面坏了** —— 明说"在等你点立场"。
      跑过（``usedSrc`` 有值）就不再覆盖：那里面可能是上一次的结论或报错。 */
  function ctIdle() {
    var box = $('#contractResult');
    if (!box || contractState.running || contractState.usedSrc) return;
    box.innerHTML = '<p style="color:var(--tt)">合同已解析好，左侧是全文。'
      + '请在上方选择审查立场 —— 点甲方或乙方，结论就替那一方争取；'
      + '点中立则甲乙双方的风险都提示。</p>';
  }

  /** 跑一次审查。三个按钮（进入/重新/带范本重审）与三张立场卡都走这里。 */
  function startReview() {
    var box = $('#contractResult');
    if (!box || contractState.running) return;
    // 这一趟发什么，由 ``ctResolve`` 在这一刻定死（只有范本时它就是材料）。
    var r = ctResolve();
    if (!r.material) { toast('请先上传合同文件'); return; }

    // 同理，**发出去的那套材料在此刻定死**：跑到一半又补了一份，不该影响正在跑的
    // 这一趟（那样结果和顶栏显示的会对不上）。后补的那份由重审按钮消费。
    contractState.usedSrc = r.src;
    contractState.usedMaterial = r.material;
    contractState.usedTpl = r.tpl;
    contractState.usedRole = ctRoleValue();
    contractState.running = true;
    $('#pg-contract').classList.add('run');
    ctReflow();

    var meta = $('#ctMeta');
    if (meta) meta.textContent = ctMetaText();
    ctPaintDoc(r.material);
    ctSyncDocBadge();

    var started = Date.now();
    var taskId = '';
    // 等待期只留转圈：一次合同审查 1~3 分钟，界面必须有东西在动、让人知道
    // "还在跑"。但**不要**摆节点名和内部判定细节 —— 那是开发期信息，
    // 给律师看的页面不该露出（见 tests/test_ui_static.py 的那条回归用例）。
    box.innerHTML = '<div class="tp3"><i></i><i></i><i></i></div>';
    var ansHost = el('div', 'ans');
    box.appendChild(ansHost);

    stream('/contract/review/stream', {
      query: '请审查这份合同，逐条指出风险条款、法律依据与修改建议。',
      material_text: r.material,
      // 范本合同正文。后端为此专门开了字段：**不能**拼进 material_text ——
      // 拼了以后两份合同的条款编号会在拆条时混进同一个序列，风险点里的
      // 「第 X 条」就可能指到范本上去，律师照着去待审合同里翻会翻到别处。
      // 只传范本时这里为空（那份已经当 material_text 发了），后端一句不用改。
      template_text: r.tpl,
      // 审查立场。**必须显式传**：后端从 query 里正则抓甲方/乙方，
      // 而这里的 query 是写死的、不含这两个词 —— 不传的话界面上选了立场，
      // 后端照样按中立跑，且从报告到日志一切正常，没有一处会报错。
      client_role: contractState.usedRole,
      material_paths: [],
    }, {
      onFrame: function (ev, data) {
        if (ev === 'meta') { taskId = data.task_id || ''; return; }
        // 关键决策一句话（约束 4 的「过程中」层）。这条链路一次 1~3 分钟，
        // 一句"补检到了 2 条法条、正在重跑引用终审"比一个转圈更能说明**还在动**。
        // 宿主是 ``box``（#contractResult）—— 这里的等待元素是它**直接**的子节点，
        // 不像聊天页那样还有个 ``.bb`` 包着。
        if (ev === 'decision') { showDecision(box, data.text); return; }
        if (ev === 'done') {
          if (taskId) data.task_id = taskId;
          ANSWER_CACHE[data.task_id || ''] = (data.draft || data.report || '');
          dropSpin(box);
          // 结果可能上万字：分帧插入，首屏（摘要 + 风险点）立刻可见
          // ``contract: true`` —— 这页不渲染完整报告，证据缺口也只在没有风险卡时出
          //（见 formatStructuredAnswer 里那两个开关；AI 助手那条链路不带它）。
          // ``noEvchain: true`` —— 这页产出的案件是「非诉/合同」，证据链只对诉讼开放，
          // 那个按钮在这页一定点不通（见 ops 里 noEvchain 的说明）。
          paintChunks(ansHost, formatStructuredAnswer(data, Date.now() - started, { contract: true, noEvchain: true }));
          ctStop();
          idle(function () { reloadPage('pg-dash'); });
          return;
        }
        if (ev === 'error') {
          dropSpin(box);
          ansHost.innerHTML = '<p style="color:var(--dg)">分析失败：' + esc(data.error || '未知错误') + '</p>';
          ctStop();
        }
      },
      onError: function (e) {
        dropSpin(box);
        ansHost.innerHTML = '<p style="color:var(--dg)">连接中断：' + esc(e.message || e)
          + '（任务可能仍在后端执行，稍后可在「日程任务」查看）</p>';
        ctStop();
      },
      onEnd: function () { ctStop(); },
    });
  }

  /** 收尾：无论成功、报错还是断线都要放行，否则按钮会永久卡在禁用态。 */
  function ctStop() {
    if (!contractState.running) return;
    contractState.running = false;
    ctReflow();
  }

  var contractBound = false;
  function bindContractOnce() {
    if (contractBound) return;
    var sec = $('#pg-contract');
    if (!sec) return;
    contractBound = true;

    // 左栏合同全文的编辑绑定（自守卫，重复调用不会挂两遍 —— 挂两遍的表现是
    // 敲一个字提交两次，而界面看不出任何异常）。
    bindContractDoc();

    Object.keys(CT_BOXES).forEach(function (kind) {
      var e = ctBox(kind);
      if (!e.card || !e.file) return;
      e.card.addEventListener('click', function () {
        // 解析中不弹选择框：连着点两下会叠出两个选择框，第二个的结果盖掉第一个
        if (!contractState.parsing[kind]) e.file.click();
      });
      e.card.addEventListener('dragover', function (ev) {
        ev.preventDefault();
        e.card.classList.add('drag');
      });
      e.card.addEventListener('dragleave', function () { e.card.classList.remove('drag'); });
      e.card.addEventListener('drop', function (ev) {
        ev.preventDefault();
        e.card.classList.remove('drag');
        if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files.length) {
          ctParse(kind, ev.dataTransfer.files);
        }
      });
      e.file.addEventListener('change', function () {
        if (e.file.files && e.file.files.length) ctParse(kind, e.file.files);
        // 清空才能重选同一个文件：不清的话第二次选它不会触发 change
        e.file.value = '';
      });
      if (e.x) e.x.addEventListener('click', function (ev) {
        // 不 stopPropagation 的话，点 ✕ 会冒泡到卡片上又弹一次文件选择框
        ev.stopPropagation();
        contractState[kind].text = '';
        contractState[kind].name = '';
        contractState[kind].chars = 0;
        contractState[kind].err = '';
        // 抽出来的名称跟着正文走：两份都移除了还留着上一份的人名，
        // 下一份合同会被张冠李戴。
        if (!contractState.main.text && !contractState.tpl.text) {
          CT_PARTIES.forEach(function (p) { contractState.names[p] = ''; });
        }
        ctPaint(kind);
        ctReflow();
      });
    });

    // 立场面板。名称输入框停在自己的 click 上（不冒泡），否则想改"另一方"的
    // 名字时会顺手把选中态也切过去 —— 改名字和选立场是两件事。
    CT_STANCES.forEach(function (p) {
      var card = $(CT_CARDS[p]);
      if (card) card.addEventListener('click', function () {
        // **点立场 = 开跑**。全页唯一的开跑入口 —— 上传只负责解析和切页，
        // 就是用户要的"我点过之后再跑"。重复点同一张 = 再跑一遍（明确动作，允许）。
        // 跑着的时候挡掉：一次 1~3 分钟，连点会在队列里堆一串白跑的任务。
        if (contractState.running) { toast('审查进行中，等这一趟跑完再换立场'); return; }
        contractState.party = p;
        startReview();
      });
      var sel = CT_NAMEBOX[p];
      if (!sel) return;
      var inp = $(sel);
      if (inp) {
        inp.addEventListener('click', function (ev) { ev.stopPropagation(); });
        inp.addEventListener('input', function () {
          contractState.names[p] = inp.value;
          ctPaintParty();
        });
      }
    });
    var pgo = $('#ctPartyGo');
    if (pgo) pgo.addEventListener('click', startReview);

    // 落地页这颗按钮**只切页、不开跑**：开跑是立场卡的活。上传完会自动切过去，
    // 所以它实际只用在"点返回回到落地页之后想再进去"这一种情况。
    var start = $('#ctStart');
    if (start) start.addEventListener('click', ctEnter);
    var again = $('#ctAgain');
    if (again) again.addEventListener('click', startReview);
    var retpl = $('#ctRetpl');
    if (retpl) retpl.addEventListener('click', startReview);
    var back = $('#ctBack');
    if (back) back.addEventListener('click', function () {
      // 只切回落地页，**不清任何状态**：文件还在框里，想再进去点「进入审查」。
      // 跑到一半也允许返回 —— 后端本来就不因客户端断开而取消任务，结果照样落库。
      sec.classList.remove('run');
      ctReflow();
    });

    // 结果区按钮：复制 / 检索同类案例 / 查看任务详情 / 修订版（生成与导出）
    var box = $('#contractResult');
    if (box) box.addEventListener('click', function (ev) {
      if (reviseAction(ev)) return;
      var cp = ev.target.closest('[data-copy]');
      if (cp) { copyText(ANSWER_CACHE[cp.getAttribute('data-copy')] || ''); return; }
      var run = ev.target.closest('[data-run]');
      if (run) { switchPage('pg-search'); return; }
      var tk = ev.target.closest('[data-task]');
      if (tk) { openTask(tk.getAttribute('data-task')); }
    });
  }

  function renderContract() {
    var sec = $('#pg-contract');
    if (!sec) return;
    bindContractOnce();
    ctReflow();
    var box = $('#contractResult');
    // 还没跑过：给一句说明，别让结果区白着。跑过就别覆盖了（renderContract
    // 虽然只跑一次，但这段判断要能扛住将来有人 reloadPage 改法）
    if (box && !box.textContent.trim()) {
      box.innerHTML = '<p class="muted">上传合同文件后，风险点、法律依据与修改建议会在这里显示。</p>';
    }
  }

  function renderCases() {
    var tb = $('#caseTbody');
    if (tb) tb.innerHTML = '<tr><td colspan="6" class="ct2">正在读取案件…</td></tr>';
    var flt = $('#caseFilter');
    if (flt) $$('.fchip', flt).forEach(function (x) {
      x.classList.toggle('on', x.getAttribute('data-f') === casePage.type);
    });
    var q = { page: casePage.n, page_size: casePage.size };
    if (casePage.type && casePage.type !== 'all') q.case_type = casePage.type;
    J('/cases', { query: q }).then(function (res) {
      var items = res.items || [];
      if (res.total != null) casePage.total = res.total;
      casePage.pages = Math.max(1, Math.ceil(casePage.total / casePage.size));
      var sub = $('#caseSub');
      if (sub) sub.textContent = '共 ' + casePage.total + ' 件 · ' + casePage.pages + ' 页';
      if (!tb) return;
      tb.innerHTML = items.length ? items.map(function (c) {
        return '<tr data-f="' + esc(c.case_type || '') + '">'
          + '<td><div class="cn">' + esc(c.title || '（未命名案件）') + '</div>'
          + '<div class="cc">' + esc(c.case_id) + (c.cause_of_action ? ' · ' + esc(c.cause_of_action) : '') + '</div></td>'
          + '<td>' + tag(TYPE_LABEL[c.case_type] || c.case_type || '—', 'ne', false) + '</td>'
          + '<td>' + tag(STAGE_LABEL[c.stage] || c.stage || '—', 'ac') + '</td>'
          + '<td>' + tag(CASE_STATUS_LABEL[c.status] || c.status || '—', c.status === 'active' ? 'ps' : 'ne') + '</td>'
          + '<td class="ct2">' + esc(fmtTime(c.updated_at)) + '</td>'
          + '<td><button class="ibtn" data-case="' + esc(c.case_id) + '">' + icon('arrow', 'sm') + '</button></td>'
          + '</tr>';
      }).join('') : '<tr><td colspan="6" class="ct2">还没有案件。点右上角「新建案件」创建第一件。</td></tr>';
      renderPager($('#casePager'), casePage, function (p) { casePage.n = p; renderCases(); });
    }).catch(function (e) { fail(tb ? tb.parentNode : null, e, '读取案件失败'); });

    var nb = $('#caseNew');
    if (nb) nb.addEventListener('click', newCaseModal);
  }

  function openCase(id) {
    var m = openModal({ title: '案件详情', body: '<p class="muted">正在读取…</p>', wide: true });
    reloadCaseInto(m, id);
  }

  /** 重新拉一次案件并就地重渲染。
      推进阶段之后用它刷新 —— **不重开弹层**：用户还停在弹层里看证据链，
      重开既闪、又会叠出第二个弹层。 */
  function reloadCaseInto(m, id) {
    J('/cases/' + encodeURIComponent(id)).then(function (c) {
      renderCaseInto(m, c);
    }).catch(function (e) {
      m.body.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>';
    });
  }

  function renderCaseInto(m, c) {
    m.body.innerHTML = '<dl class="kv">'
      + kvRow('案件编号', c.case_id) + kvRow('名称', c.title)
      + kvRow('类型', TYPE_LABEL[c.case_type] || c.case_type)
      + kvRow('案由', c.cause_of_action)
      // data-kv-stage 是给「推进到开庭」用的锚点：点完之后只改这一个 dd，
      // 不去猜 dd 的序号 —— kvRow 遇到空值会少渲染一行，序号并不稳定。
      + kvRow('阶段', STAGE_LABEL[c.stage] || c.stage, 'data-kv-stage')
      + kvRow('状态', CASE_STATUS_LABEL[c.status] || c.status)
      + kvRow('委托人', c.client_name) + kvRow('对方', c.opponent_name)
      + kvRow('摘要', c.summary) + kvRow('更新时间', fmtTime(c.updated_at))
      + '</dl>'
      + stageAdvanceBar(c)
      + (c.case_type === 'litigation' ? evidenceBlocks(c) : noEvidenceBlocks(c.case_type));
    if (c.case_type !== 'litigation') return;
    // 评估按钮（与证据链同层，无 .lp-modal 排除问题）
    var ab = m.body.querySelector('[data-assess]');
    if (ab) ab.addEventListener('click', function () { runAssessment(c.case_id); });
    // 胜诉测算
    var wb = m.body.querySelector('[data-win]');
    if (wb) wb.addEventListener('click', function () { runWinAssessment(c.case_id); });
    // 推进到开庭
    var sb = m.body.querySelector('[data-tostage]');
    if (sb) sb.addEventListener('click', function () {
      runStageAdvance(sb.getAttribute('data-tostage'), 'hearing', sb, m);
    });
    // 上传材料：按钮唤起隐藏文件框，选完即传（传完自动解析，证据链才有正文可读）
    var fin = m.body.querySelector('#caseMatFile');
    var upbtn = m.body.querySelector('[data-mat-upload]');
    if (upbtn && fin) {
      upbtn.addEventListener('click', function () { fin.click(); });
      fin.addEventListener('change', function () {
        if (fin.files && fin.files.length) uploadMaterials(c.case_id, fin.files);
        fin.value = '';
      });
    }
    var mlb = m.body.querySelector('#matListBox');
    if (mlb) {
      mlb.addEventListener('click', function (e) {
        var pb = e.target.closest('[data-mat-parse]');
        if (pb) { parseOneMaterial(pb.getAttribute('data-mat-parse'), pb); return; }
        var vb = e.target.closest('[data-mat-view]');
        if (vb) openMaterialView(vb.getAttribute('data-mat-view'), matNameFrom(vb));
      });
    }
    loadMaterials(c.case_id);
    loadEvidenceChain(c.case_id);
    loadWinAssessment(c.case_id);
  }

  /* ------------------------------------------------------------ 阶段推进
     「让这件需要开庭的诉讼真的进到开庭审理阶段」是用户点名要的。
     后端其实早就铺好了：STAGES 里有 hearing、add_progress 会顺手改 case.stage、
     工作台那张卡也一直在数 by_stage.hearing —— 缺的只是一个**人工入口**
     （此前只有新建案件弹窗里那个下拉能把它设成 hearing，设完就再也回不去了）。
     走 POST /cases/{id}/progress：它的语义就是「追加办理进度」，
     推进阶段正是一条进度，不必另开一条路由。 */
  var STAGE_NEXT = { intake: 'hearing', evidence: 'hearing', in_progress: 'hearing' };

  function stageAdvanceBar(c) {
    if (c.case_type !== 'litigation') return '';
    if (STAGE_NEXT[c.stage]) {
      return '<div class="stage-bar">'
        + '<button class="btn sm pr" data-tostage="' + esc(c.case_id) + '">'
        + icon('court', 'sm') + '推进到开庭审理</button>'
        + '<span class="hint">推进后列表页「阶段」列与工作台「开庭审理阶段」卡同步更新</span></div>';
    }
    // 已到开庭就给一个明确的到达态。**不留空白** —— 按钮凭空消失会被当成
    // 页面没加载出来（这个坑在证据链那几块上已经踩过）。
    if (c.stage === 'hearing') {
      return '<div class="stage-bar"><span class="tag ac">已在开庭审理阶段</span></div>';
    }
    return '';
  }

  function kvRow(k, v, attr) {
    if (v === undefined || v === null || v === '') return '';
    return '<dt>' + esc(k) + '</dt><dd' + (attr ? ' ' + attr : '') + '>' + esc(v) + '</dd>';
  }

  /* ------------------------------------------------------------ 证据链的准入门槛
     **只有「诉讼」案件才有证据链这一组**（2026-09-29 用户点名：非诉/合同 与 咨询 不能传）。
     证据材料 → 证据链 → 充分度评估是一条链：链的正文就是从材料解析产物里读出来的，
     拆开只留一半没有意义，所以整组一起出现、一起消失。
     后端同样拦着（``case_service.generate_evidence_chain`` / 上传材料路由），
     这里隐藏是"别把做不到的按钮摆出来"，不是唯一防线。 */
  function evidenceBlocks(c) {
    return '<div class="evchain">'
      + '<div class="evchain-h">' + icon('folder', 'sm') + '证据材料'
      + '<button class="btn sm" data-mat-upload style="margin-left:auto">' + icon('plus', 'sm') + '上传材料</button>'
      + '<input type="file" id="caseMatFile" multiple style="display:none" '
      + 'accept=".pdf,.doc,.docx,.txt,.md,.jpg,.jpeg,.png,.bmp"></div>'
      + '<div id="matListBox" class="mat-box"><p class="muted">读取材料…</p></div>'
      + '</div>'
      + '<div class="evchain">'
      + '<div class="evchain-h">' + icon('scale', 'sm') + '证据链'
      + '<button class="btn sm pr" data-evchain="' + esc(c.case_id) + '" style="margin-left:auto">生成证据链</button></div>'
      + '<div id="evchainBox" class="evchain-box"><p class="muted">尚未生成证据链。</p></div>'
      + '</div>'
      + '<div class="evchain">'
      + '<div class="evchain-h">' + icon('check', 'sm') + '证据充分度评估'
      + '<button class="btn sm pr" data-assess="' + esc(c.case_id) + '" style="margin-left:auto">评估</button></div>'
      + '<div id="assessBox" class="evchain-box"><p class="muted">点右上角「评估」基于证据链给出充分度结论与补证建议。</p></div>'
      + '</div>'
      // 第四块：胜诉把握。**必须在 evidenceBlocks 里**，非诉/咨询才跟着一起拿不到
      // （见上面的证据链准入门槛说明）—— 别把它挪到 openCase 里单独渲染。
      + '<div class="evchain">'
      + '<div class="evchain-h">' + icon('gavel', 'sm') + '胜诉成功率'
      + '<button class="btn sm pr" data-win="' + esc(c.case_id) + '" style="margin-left:auto">测算</button></div>'
      + '<div id="winBox" class="evchain-box"><p class="muted">点右上角「测算」按证据链算出证据覆盖率，并给出 AI 法律分析。</p></div>'
      + '</div>';
  }
  /** 非诉讼案件的位置提示。留一句话是**故意的**：整块不出现会被当成页面没加载出来，
      写清楚"这类案件不走证据链"才不会让人反复刷新或来问。 */
  function noEvidenceBlocks(caseType) {
    return '<div class="evchain">'
      + '<p class="mat-empty">证据链只对「诉讼」案件开放。本案类型是「'
      + esc(TYPE_LABEL[caseType] || caseType || '未填') + '」，'
      + '没有证据材料上传、证据链与充分度评估这一组。</p>'
      + '</div>';
  }

  /* ------------------------------------------------------------ 证据链
     纯增量：案件详情弹层里多一块「证据链」，分析报告尾部多一个「生成证据链」按钮。
     后端端点：GET/POST /cases/{case_id}/evidence-chain、GET /cases/by-task/{task_id}。 */
  function loadEvidenceChain(caseId) {
    var box = document.getElementById('evchainBox');
    if (!box) return;
    box.innerHTML = '<div class="tm muted">读取证据链…</div>';
    J('/cases/' + encodeURIComponent(caseId) + '/evidence-chain').then(function (r) {
      renderEvidenceChain(r, box);
    }).catch(function (e) {
      box.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>';
    });
  }

  function renderEvidenceChain(r, box) {
    var chain = r.chain || [];
    if (!chain.length) {
      box.innerHTML = '<p class="muted">尚未生成证据链。点右上角「生成证据链」基于卷宗材料与案件摘要'
        + '自动梳理（主张 → 支撑证据 → 缺口），几秒即可出结果。</p>';
      return;
    }
    var gen = r.generated_at
      ? '<p class="muted" style="font-size:12px;margin-bottom:8px">生成于 ' + esc(r.generated_at) + '</p>' : '';
    var html = chain.map(function (it) {
      var evs = (it.evidence || []).map(function (e) {
        var support = e.support === '间接' ? 'ac' : 'ps';
        return '<li><b>' + esc(e.material || '—') + '</b>'
          + (e.excerpt ? '：' + esc(e.excerpt) : '')
          + '　<span class="tag ' + support + '">' + esc(e.support || '直接') + '</span></li>';
      }).join('') || '<li class="muted">（无支撑证据）</li>';
      return '<div class="evc">'
        + '<div class="evc-claim">' + icon('bookmark', 'sm') + esc(it.claim || '—') + '</div>'
        + '<ul class="evc-ev">' + evs + '</ul>'
        + (it.gap ? '<div class="evc-gap">缺口：' + esc(it.gap) + '</div>' : '')
        + '</div>';
    }).join('');
    box.innerHTML = gen + html;
  }

  function generateEvidenceChain(caseId) {
    var box = document.getElementById('evchainBox');
    if (!box) return;
    box.innerHTML = '<div class="tm muted">正在基于卷宗材料与案件摘要梳理证据链（约 5~15 秒）…</div>';
    J('/cases/' + encodeURIComponent(caseId) + '/evidence-chain', { method: 'POST' })
      .then(function (r) { renderEvidenceChain(r, box); toast('证据链已生成'); })
      .catch(function (e) {
        box.innerHTML = '<p style="color:var(--dg)">生成失败：' + esc(e.message) + '</p>';
        toast('失败：' + e.message);
      });
  }

  /* ------------------------------------------------------------ 证据材料
     纯增量：案件详情弹层里多一块「证据材料」（上传 / 解析 / 列表）。
     后端端点全部现成：GET/POST /cases/{case_id}/materials、POST /materials/{id}/parse。
     上传后自动触发解析 —— 证据链的正文就是从解析产物里读的，不解析 = 链上全是缺口。 */
  var MAT_TYPE_LABEL = {
    contract: '合同', evidence: '证据', pleading: '诉状', judgment: '裁判文书',
    correspondence: '往来函件', other: '其他', unknown: '未分类',
  };
  function loadMaterials(caseId) {
    var box = document.getElementById('matListBox');
    if (!box) return;
    J('/cases/' + encodeURIComponent(caseId) + '/materials').then(function (r) {
      renderMaterials(r.items || [], box);
    }).catch(function (e) {
      box.innerHTML = '<p style="color:var(--dg)">材料读取失败：' + esc(e.message) + '</p>';
    });
  }
  function renderMaterials(items, box) {
    if (!items.length) {
      box.innerHTML = '<p class="mat-empty">还没有材料。点右上角「上传材料」把合同原件、聊天记录、'
        + '付款凭证等传上来，再生成证据链，缺口就会变成支撑证据。</p>';
      return;
    }
    box.innerHTML = items.map(function (m) {
      var tag = m.parsed ? '<span class="tag ps">已解析</span>'
        : '<button class="btn sm" data-mat-parse="' + m.id + '">解析</button>';
      // 整行可点（`data-mat-view`）+ 一枚显式的「查看」按钮。
      // 只做整行点击的话，材料长什么样、能不能点，界面上完全没有提示 ——
      // 而这条路径是"证据链里那句缺口到底依据什么"的唯一入口，必须让人看得见。
      return '<div class="mat-item" data-mat-view="' + m.id + '"'
        + ' title="点击查看材料正文">' + icon('doc', 'sm')
        + '<span class="nm">' + esc(m.filename) + '</span>'
        + '<span class="meta">' + esc(MAT_TYPE_LABEL[m.doc_type] || m.doc_type || '未分类')
        + ' · ' + esc(fmtTime(m.uploaded_at)) + '</span>'
        + '<button class="btn sm" data-mat-view="' + m.id + '">查看</button>'
        + tag + '</div>';
    }).join('');
  }

  /** 从被点的那一行里取材料名（查看器标题用）。取不到就空串，标题退化成「材料 ·」。 */
  function matNameFrom(el2) {
    var row = el2 && el2.closest && el2.closest('.mat-item');
    var nm = row ? row.querySelector('.nm') : null;
    return (nm && nm.textContent) || '';
  }

  /** 查看材料正文。
   *
   * 用弹层而不是跳页：律师是在"看证据链 / 传材料"的上下文里想核对某一份材料，
   * 跳走就把上下文丢了；弹层关掉还在原地。
   *
   * 后端会把三件事一并交代（见 ``GET /materials/{id}/content``）：
   * ``source``（解析产物 / 原文）、``truncated``（有没有被截断）、``notice``（读不到的原因）。
   * 这三样都要显示 —— 只说"这是材料正文"，律师会以为看到的就是全部。
   */
  function openMaterialView(id, name) {
    var m = openModal({
      title: '材料 · ' + (name || ''), wide: true,
      body: '<p class="muted">读取正文…</p>',
    });
    // 「解析」按钮走**事件委托**（它在正文重画时才出现，绑不上具体节点）。
    // 这颗按钮是必需的：后端的提示语一直在说"点『解析』"，而查看器里原先
    // 没有它 —— 律师只能关掉弹层、回到列表里找那颗按钮，路径长得没道理
    // （2026-10-12，从微信上传的图片材料上暴露出来：图抽不出字，
    // 提示让他点解析，而弹层里根本没有）。
    m.body.addEventListener('click', function (e) {
      var pb = e.target.closest('[data-mv-parse]');
      if (pb) parseFromViewer(m, id, name, pb);
    });
    loadMaterialView(m, id, name);
  }

  function loadMaterialView(m, id, name) {
    J('/materials/' + encodeURIComponent(id) + '/content').then(function (r) {
      renderMaterialView(m, id, name, r);
    }).catch(function (e) {
      m.body.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>';
    });
  }

  function renderMaterialView(m, id, name, r) {
    var srcLabel = r.source === 'parsed' ? '来源：解析产物'
      : (r.source === 'raw' ? '来源：原文' : '');
    // 只在**没有正文**时才摆「解析」：有正文时它多余，而重新解析会覆盖解析产物 ——
    // 律师点进来往往正是为了留下这份产物，不该给他一颗顺手就能毁掉它的按钮。
    var parseBtn = (!r.has_text && r.has_file !== false)
      ? '<button class="btn sm" style="margin-left:auto" data-mv-parse="1">'
        + icon('refresh', 'sm') + (r.parsed ? '重新解析' : '解析') + '</button>'
      : '';
    var head = '<div class="mat-view-h">'
      + tag(MAT_TYPE_LABEL[r.doc_type] || r.doc_type || '未分类', 'ne', false)
      + (srcLabel ? '<span class="muted">' + esc(srcLabel) + '</span>' : '')
      + (r.chars ? '<span class="muted">' + esc(String(r.chars)) + ' 字</span>' : '')
      + '<span class="muted">' + esc(r.filename || '') + '</span>'
      + parseBtn
      + '</div>';
    var notes = '';
    if (r.notice) notes += '<p class="mat-view-n">' + esc(r.notice) + '</p>';
    if (r.truncated) {
      notes += '<p class="mat-view-n">正文过长，这里只显示前 '
        + esc(String(r.chars)) + ' 字。</p>';
    }
    // 正文经 ``esc`` 再进 <pre>：材料里可能有任意字符，绝不裸插 innerHTML。
    var body = r.text ? '<pre class="mat-view-t">' + esc(r.text) + '</pre>' : '';
    m.body.innerHTML = head + notes + body;
  }

  /** 在查看器里解析，然后**重读正文**。
   *
   * 解析接口不回传全文（可能几十万字，见 ``api/v1/cases.py``），所以解析完必须
   * 回来重读一次 —— 否则律师看到的是"点了没反应"。``chars`` 为 0 时要把原因
   * 说出来：图片材料多半是多模态服务没起，那件事和材料本身无关。 */
  function parseFromViewer(m, id, name, btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>解析中…';
    J('/materials/' + encodeURIComponent(id) + '/parse', { method: 'POST' })
      .then(function (r) {
        var note = (r.notes && r.notes.length) ? String(r.notes[0]) : '';
        if (r.chars) toast('解析完成：抽取 ' + r.chars + ' 字');
        else toast('解析完成，但没抽出文字' + (note ? '：' + note : ''));
        loadMaterialView(m, id, name);
      })
      .catch(function (e) {
        toast('解析失败：' + e.message);
        // 失败时要把按钮**复原**：不复原它就永远停在"解析中…"且不可点，
        // 看起来像卡死了（成功路径靠重画整个正文换掉它）。
        btn.disabled = false;
        btn.innerHTML = icon('refresh', 'sm') + '重新解析';
      });
  }
  /** 上传材料。``box`` / ``onDone`` 可省：弹层里那套走默认值，证据链页那套另传。
      （证据链页的列表容器是 ``#evpMatBox``，不能写死成弹层的 ``#matListBox``。） */
  function uploadMaterials(caseId, files, box, onDone) {
    box = box || document.getElementById('matListBox');
    var names = [];
    for (var i = 0; i < files.length; i++) names.push(files[i].name);
    if (box) box.innerHTML = '<p class="muted">正在上传 ' + names.length + ' 份材料：' + esc(names.join('、')) + '…</p>';
    // 逐份上传：FormData 走 req()（不带 JSON 头），传完自动触发解析再刷列表
    var seq = Promise.resolve();
    var ok = 0, failed = [];
    Array.prototype.forEach.call(files, function (f) {
      seq = seq.then(function () {
        var fd = new FormData();
        fd.append('file', f);
        fd.append('doc_type', 'evidence');
        return req('/cases/' + encodeURIComponent(caseId) + '/materials', { method: 'POST', body: fd })
          .then(function (r) {
            if (!r.ok) {
              return r.text().then(function (t) {
                var d = '';
                try { d = JSON.parse(t).detail || ''; } catch (e) { d = ''; }
                throw new Error(d || ('HTTP ' + r.status));
              });
            }
            return r.json();
          })
          .then(function (info) {
            ok++;
            // 解析失败不阻断：列表里会显示「解析」按钮可手动重试
            return req('/materials/' + info.id + '/parse', { method: 'POST' })
              .catch(function () { /* 忽略，保留手动按钮 */ });
          })
          .catch(function (e) { failed.push(f.name + '（' + e.message + '）'); });
      });
    });
    seq.then(function () {
      if (onDone) onDone(); else loadMaterials(caseId);
      if (failed.length) toast('上传完成 ' + ok + ' 份，失败：' + failed.join('、'));
      else toast(ok + ' 份材料已上传并解析，可重新生成证据链');
    });
  }
  /** 解析单份材料。``reload`` 可省：默认刷新弹层列表；证据链页传自己的刷新函数。
      （弹层里那份靠 ``currentCaseId()`` 定位案件，页面上没有 ``#evchainBox``，
      不另给 reload 的话解析完会去刷一个空的案件。） */
  function parseOneMaterial(materialId, btn, reload) {
    reload = reload || function () { loadMaterials(currentCaseId()); };
    var old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>解析中…';
    req('/materials/' + encodeURIComponent(materialId) + '/parse', { method: 'POST' })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (t) {
            var d = '';
            try { d = JSON.parse(t).detail || ''; } catch (e) { d = ''; }
            throw new Error(d || ('HTTP ' + r.status));
          });
        }
        return r.json();
      })
      .then(function (r) {
        // 与查看器里那颗「解析」按钮同一套交代：抽不到文字时**把原因说出来**。
        // 光说"解析完成"会被当成"点了但没看到效果"——图片/扫描件正是这种情况，
        // 而原因（多模态服务不可用）与材料本身无关，不说清就会反复点。
        var note = (r && r.notes && r.notes.length) ? String(r.notes[0]) : '';
        toast(r && r.chars ? ('解析完成：抽取 ' + r.chars + ' 字')
          : ('解析完成，但没抽出文字' + (note ? '：' + note : '')));
        reload();
      })
      .catch(function (e) { toast('解析失败：' + e.message); btn.disabled = false; btn.innerHTML = old; });
  }
  /** 当前打开的案件详情对应的案件号（弹层里的 data-evchain 按钮带着它）。 */
  function currentCaseId() {
    var b = document.querySelector('#evchainBox') &&
      document.querySelector('[data-evchain]');
    return b ? b.getAttribute('data-evchain') : '';
  }

  /* ------------------------------------------------------------ 证据链页（左栏独立页）
     2026-10-11：把「证据链的生成」从案件详情弹层提到左栏，独立成一页 ——
     以前要先生成/找到案件、进案件管理、点开详情弹层才发现那两个按钮。

     端点与弹层那一组完全一样（GET/POST /cases/{id}/evidence-chain、
     GET/POST /cases/{id}/materials、POST /materials/{id}/parse），
     仍只对「诉讼」案件开放：这里按 case_type=litigation 过滤列表，
     后端 case_service.assert_evidence_chain_allowed 另有一道闸门。
     ⚠️ 抓手全部以 ``evp`` 开头，**不与弹层里的 matListBox / evchainBox 重名** ——
        重名会让 getElementById 取到另一处，两个界面互相把对方的列表写花且不报错。 */
  var EVP = {
    caseId: '', bound: false, titles: null, wxPoll: 0, wxCount: -1,
    cases: [],    // 已拉到的诉讼案件（搜索在本地过滤这一份，不再发请求）
    query: '',    // 搜索词
  };

  function renderEvchain() {
    bindEvchainOnce();
    loadEvpCases();
    loadEvpWechat();
  }

  function bindEvchainOnce() {
    var sec = $('#pg-evchain');
    if (!sec || EVP.bound) return;
    EVP.bound = true;

    // 左侧案件列表：点一行 = 选中该案件（右键/换行都不做，避免误触）
    var list = $('#evpCaseList');
    if (list) list.addEventListener('click', function (e) {
      var row = e.target.closest('[data-evp-case]');
      if (row) selectEvpCase(row.getAttribute('data-evp-case'));
    });

    // 案件搜索：只重画列表，不重拉数据（全量已在 EVP.cases 里）
    var q = $('#evpCaseQ');
    if (q) q.addEventListener('input', function () {
      EVP.query = q.value || '';
      paintEvpCases();
    });

    var up = $('#evpMatUp'), file = $('#evpFile');
    if (up && file) {
      up.addEventListener('click', function () { file.click(); });
      file.addEventListener('change', function () {
        if (file.files && file.files.length) {
          uploadMaterials(EVP.caseId, file.files, $('#evpMatBox'), function () {
            loadEvpMaterials(EVP.caseId);
          });
        }
        file.value = '';   // 清空才能重选同一个文件
      });
    }

    var gen = $('#evpGen');
    if (gen) gen.addEventListener('click', generateEvpChain);

    // 材料列表：整行/「查看」→ 看正文；「解析」→ 解析（解析完刷新本页列表）
    var mbox = $('#evpMatBox');
    if (mbox) mbox.addEventListener('click', function (e) {
      var pb = e.target.closest('[data-mat-parse]');
      if (pb) {
        parseOneMaterial(pb.getAttribute('data-mat-parse'), pb, function () {
          loadEvpMaterials(EVP.caseId);
        });
        return;
      }
      var vb = e.target.closest('[data-mat-view]');
      if (vb) openMaterialView(vb.getAttribute('data-mat-view'), matNameFrom(vb));
    });

    // 一颗「刷新」要刷新这一页的**全部**数据，不是只刷一半 ——
    // 律师点它时心里的对象是"这一页"，不是"案件列表"。
    var rf = $('#evpRefresh');
    if (rf) rf.addEventListener('click', function () {
      loadEvpCases();
      loadEvpWechat();
    });

    // 微信绑定：取码 / 撤销
    var wxBtn = $('#evpWxCode');
    if (wxBtn) wxBtn.addEventListener('click', issueEvpWxCode);
    // 复制那条 `/绑定 xxx` 命令（生成后它才出现，所以走委托）
    var wxBox = $('#evpWxCodeBox');
    if (wxBox) wxBox.addEventListener('click', function (e) {
      var c = e.target.closest('[data-evp-copy]');
      if (c) copyText(c.getAttribute('data-evp-copy'));
    });
    var wxList = $('#evpWxList');
    if (wxList) wxList.addEventListener('click', function (e) {
      var b = e.target.closest('[data-evp-wxrm]');
      if (!b) return;
      b.disabled = true;
      J('/wechat/bindings/' + encodeURIComponent(b.getAttribute('data-evp-wxrm')),
        { method: 'DELETE' })
        .then(function () { toast('已解除绑定'); loadEvpWechat(); })
        .catch(function (err) { toast('解除失败：' + err.message); b.disabled = false; });
    });
  }

  /* ------------------------------------------------------------ 微信绑定
     端点在 backend/app/api/v1/wechat.py。绑定码是**授权凭证**（拿着它可以以本人
     口径读写案件），所以只在点「生成绑定码」后临时显示，不写 localStorage、
     不预取 —— 页面刷新即消失，必须重新取。
     ⚠️ ``/wechat/status`` 在功能未开启时也返回 200（带 enabled/ready 标志），
        所以这里按标志决定按钮可用性，而不是把 200 当"能用"。 */
  function loadEvpWechat() {
    var state = $('#evpWxState'), hint = $('#evpWxHint'), btn = $('#evpWxCode');
    if (!state) return;
    J('/wechat/status').then(function (st) {
      var ready = !!st.ready;
      state.innerHTML = '<i></i>' + (ready ? '已接入'
        : (st.enabled ? '未就绪：缺桥接密钥' : '未开启'));
      state.style.color = ready ? '' : 'var(--tt)';
      // 提示按**当前状态**分三种说：没开 / 开了但没绑 / 已绑可用。
      // 一句话覆盖三种状态的话，绑好的人每次进来都要再读一遍"怎么绑定"。
      var bound = (st.bindings || []).length;
      if (btn) {
        btn.disabled = !ready;
        // 绑过之后，这枚按钮的语义从"开启功能"变成"再加一个号"。
        // 而主按钮样式（``.pr``）与「生成绑定码」这四个字都在说"你必须先做这一步" ——
        // 对已经绑好的人是误导：他会以为上一次没绑成功（这条我们踩过）。
        btn.className = 'btn sm' + (bound ? '' : ' pr');
        btn.innerHTML = icon('plus', 'sm') + (bound ? '再绑一个微信号' : '生成绑定码');
      }
      if (hint) {
        if (!ready) {
          hint.textContent = st.enabled
            ? '后端已打开微信接入，但缺少 WECHAT_BRIDGE_TOKEN，接口当前不可用。'
            : '后端未开启微信接入（WECHAT_ENABLED=false），此功能不可用。';
        } else if (!bound) {
          hint.textContent = '还没有绑定微信号。点「生成绑定码」，把那串码直接发到微信即可'
            + '（机器人能认出来，不用加任何前缀）。';
        } else {
          hint.textContent = '已就绪：在微信里直接发图片/文件就是上传证据，'
            + '会归档到左侧选中的案件并自动生成证据链。';
        }
      }
      EVP.wxCount = bound;
      renderEvpWxList(st.bindings || []);
    }).catch(function (e) {
      state.textContent = '状态读取失败';
      state.style.color = 'var(--tt)';
      if (hint) hint.textContent = '读取微信接入状态失败：' + e.message;
    });
  }

  /** 生成绑定码后短轮询绑定状态。
   *
   * 为什么值得加一个定时器：律师此刻正在微信里操作，回到页面时列表**必须**已经是
   * 新的 —— 否则他会以为又失败了，再点一次「生成绑定码」，而那会把上一张码作废
   * （``issue_bind_code`` 会失效同账号的未用码），于是真的失败。这个轮询解决的不是
   * 性能问题，是"别让他手忙脚乱再点一次"。
   *
   * 上限 2 分钟（与绑定码 TTL 同量级）、成功即停、失败静默：轮询本身不该打扰人。 */
  function startEvpWxWatch() {
    stopEvpWxWatch();
    var tries = 0;
    EVP.wxPoll = setInterval(function () {
      tries += 1;
      if (tries > 24) { stopEvpWxWatch(); return; }
      J('/wechat/status').then(function (st) {
        var items = st.bindings || [];
        if (items.length !== EVP.wxCount) {
          EVP.wxCount = items.length;
          renderEvpWxList(items);
        }
        if (items.length) {
          stopEvpWxWatch();
          toast('微信已绑定：' + (items[0].display_name || wxPeerLabel(items[0].peer_id)));
        }
      }).catch(function () { /* 静默 */ });
    }, 5000);
  }

  function stopEvpWxWatch() {
    if (EVP.wxPoll) { clearInterval(EVP.wxPoll); EVP.wxPoll = 0; }
  }

  /** 微信 peer_id → 可读的短标签。
   *
   * peer_id 形如 ``o9cq808PTPerBDGJI_C-dRoNNyvc@im.wechat``。整串摆出来律师认不出
   * 是谁（实测截图里就是一大串乱码似的字符），所以去掉 ``@im.wechat`` 后缀、
   * 中间省略；**完整值放 title**，真要核对时还点得出来。
   * 昵称（display_name）目前机器人不传 —— 传了就优先用它。
   */
  function wxPeerLabel(peer) {
    var s = String(peer || '');
    var at = s.indexOf('@');
    var base = at > 0 ? s.slice(0, at) : s;
    if (base.length > 16) base = base.slice(0, 10) + '…' + base.slice(-4);
    return base;
  }

  function renderEvpWxList(items) {
    var box = $('#evpWxList');
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<p class="mat-empty">还没有绑定微信号。点「生成绑定码」，'
        + '把那串码发到微信即可。</p>';
      return;
    }
    box.innerHTML = items.map(function (b) {
      return '<div class="mat-item">' + icon('user', 'sm')
        + '<span class="nm" title="' + esc(b.peer_id) + '">微信账号 · '
        + esc(b.display_name || wxPeerLabel(b.peer_id)) + '</span>'
        + '<span class="meta">' + esc(fmtTime(b.bound_at)) + ' 绑定</span>'
        + '<button class="btn sm" data-evp-wxrm="' + esc(b.peer_id) + '">解除绑定</button>'
        + '</div>';
    }).join('');
  }

  function issueEvpWxCode() {
    var btn = $('#evpWxCode'), box = $('#evpWxCodeBox');
    if (btn) btn.disabled = true;
    J('/wechat/bind-code', { method: 'POST' })
      .then(function (r) {
        if (box) {
          // 只给**这串码**，不给 ``/绑定 <码>``。
          // 原因是机器人里 `/绑定` 已经被另一个功能占用（用户互转的 LINUX DO 绑定），
          // 律师照着我们的话去打 `/绑定` 只会收到那个功能报的"绑定码无效或已过期"
          //（实测踩过）。裸码是不会有歧义的：那串码的形状只有本助手认。
          box.innerHTML = '<code style="font-family:var(--mono);font-size:16px;font-weight:600;'
            + 'letter-spacing:2px;background:var(--mu);border:1px solid var(--br);'
            + 'border-radius:6px;padding:3px 12px">' + esc(r.code) + '</code>'
            + '<button class="btn sm" data-evp-copy="' + esc(r.code) + '">复制</button>'
            + '<span style="font-size:12px;color:var(--tt)">把这串码直接发给机器人 · '
            + esc(r.ttl_minutes) + ' 分钟内有效</span>';
        }
        // 生成即开始盯着绑定结果：他这会儿正在微信里敲码，回来时列表该是新的
        startEvpWxWatch();
        toast('已生成绑定码：复制后直接发到微信即可（不用加前缀）');
      })
      .catch(function (e) { toast('生成失败：' + e.message); })
      .then(function () { if (btn) btn.disabled = false; });
  }

  function loadEvpCases() {
    var box = $('#evpCaseList');
    if (!box) return;
    box.innerHTML = '<p class="evp-empty">读取案件…</p>';
    J('/cases', { query: { limit: 100 } }).then(function (res) {
      EVP.cases = (res.items || []).filter(function (c) { return c.case_type === 'litigation'; });
      var sub = $('#evpSub');
      if (sub) {
        // 0 件时不要再报"0 件 · 基于卷宗材料…"：那行看着像状态，实际是**行动提示**，
        // 要让律师一眼知道"我现在缺的是案件"，而不是去猜为什么页面是空的。
        sub.textContent = EVP.cases.length
          ? '诉讼案件 ' + EVP.cases.length + ' 件 · 基于卷宗材料自动梳理「主张 → 证据 → 缺口」'
          : '还没有「诉讼」案件 · 证据链只对诉讼案件开放';
      }
      // 空态时把卡头那枚「新建案件」收起来：不然同一个动作在 100px 内摆两遍，
      // 而真正该被看见的是空态里那个（它带着说明）。
      var nb = $('#evpCaseNew');
      if (nb) nb.hidden = !EVP.cases.length;
      paintEvpCases();
      // 刷新后保持选中：上一轮选中的案件还在列表里就接着用它，重新拉材料与链路
      var still = EVP.cases.some(function (c) { return c.case_id === EVP.caseId; });
      if (EVP.caseId && still) selectEvpCase(EVP.caseId);
    }).catch(function (e) {
      box.innerHTML = '<p class="evp-empty" style="color:var(--dg)">读取案件失败：' + esc(e.message) + '</p>';
    });
  }

  /** 按搜索词画案件列表。**只重画，不重拉** —— 过滤全在本地做。 */
  function paintEvpCases() {
    var box = $('#evpCaseList');
    if (!box) return;
    var all = EVP.cases || [];
    if (!all.length) {
      // 空态给**出口**而不是一句说明：这里是他唯一的主路径，
      // 让他自己去侧栏找「案件管理」是一次没必要的绕路。
      box.innerHTML = '<p class="evp-empty">证据链只对「诉讼」案件开放。还没有诉讼案件，'
        + '先去建一件。</p>'
        + '<button class="btn sm pr" data-go="pg-case">' + icon('plus', 'sm')
        + '新建诉讼案件</button>';
      return;
    }
    var q = (EVP.query || '').trim().toLowerCase();
    var items = q
      ? all.filter(function (c) {
          return ((c.title || '') + ' ' + (c.case_id || '')).toLowerCase().indexOf(q) >= 0;
        })
      : all;
    if (!items.length) {
      box.innerHTML = '<p class="evp-empty">没有匹配「' + esc(EVP.query) + '」的案件。</p>';
      return;
    }
    // 案件名存一份，选中时写进卡片标题（见 selectEvpCase）
    EVP.titles = {};
    box.innerHTML = items.map(function (c) {
      EVP.titles[c.case_id] = c.title || '（未命名案件）';
      // 链状态是这个列表的**主要决策信息**：有链的可以直接起草带证据链的文书，
      // 没链的先去传材料。原来要点进每一件才知道，27 件就是 27 次点击。
      //
      // ⚠️ 字段不是数字就不显示这个标签 —— 老后端（还没重启）不返回 ``chain_claims``，
      // 那时``Number(undefined||0)=0`` 会把**每一件**都标成"未生成"，
      // 而"不知道"和"没有"是两件事：宁可这一列空着。
      var st = '';
      if (typeof c.chain_claims === 'number') {
        st = c.chain_claims > 0
          ? '<span class="evp-st on">已生成 ' + c.chain_claims + ' 条</span>'
          : '<span class="evp-st">未生成</span>';
      }
      return '<button class="evp-case' + (c.case_id === EVP.caseId ? ' on' : '') + '"'
        + ' data-evp-case="' + esc(c.case_id) + '">'
        + '<span class="hdr"><span class="tt">' + esc(c.title || '（未命名案件）') + '</span>'
        + st + '</span>'
        + '<span class="mm">' + esc(c.case_id) + '</span></button>';
    }).join('');
  }

  function selectEvpCase(id) {
    EVP.caseId = id;
    $$('#evpCaseList [data-evp-case]').forEach(function (r) {
      r.classList.toggle('on', r.getAttribute('data-evp-case') === id);
    });
    // 卡片标题带上案件名：这页同时要"传证据"和"看链路"，不说清是哪一件，
    // 往下传的文件到底落在哪个案卷里就得靠记忆 —— 律师不该靠记忆。
    var title = $('#evpTitle');
    if (title) title.textContent = EVP.titles && EVP.titles[id] ? '证据链 · ' + EVP.titles[id] : '证据链';
    var hint = $('#evpHint');
    if (hint) hint.hidden = true;
    ['#evpMatUp', '#evpGen', '#evpMatBox', '#evpChainBox'].forEach(function (sel) {
      var el2 = $(sel);
      if (el2) el2.hidden = false;
    });
    var file = $('#evpFile');
    if (file) file.value = '';
    loadEvpMaterials(id);
    loadEvpChain(id);
  }

  function loadEvpMaterials(caseId) {
    var box = $('#evpMatBox');
    if (!box || !caseId) return;
    box.innerHTML = '<p class="muted">读取材料…</p>';
    J('/cases/' + encodeURIComponent(caseId) + '/materials').then(function (r) {
      renderMaterials(r.items || [], box);
    }).catch(function (e) {
      box.innerHTML = '<p style="color:var(--dg)">材料读取失败：' + esc(e.message) + '</p>';
    });
  }

  function loadEvpChain(caseId) {
    var box = $('#evpChainBox');
    if (!box || !caseId) return;
    box.innerHTML = '<div class="tm muted">读取证据链…</div>';
    J('/cases/' + encodeURIComponent(caseId) + '/evidence-chain').then(function (r) {
      renderEvidenceChain(r, box);
    }).catch(function (e) {
      box.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>';
    });
  }

  function generateEvpChain() {
    if (!EVP.caseId) { toast('请先在左侧选择一件诉讼案件'); return; }
    var box = $('#evpChainBox'), btn = $('#evpGen');
    if (!box) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>生成中…'; }
    box.hidden = false;
    box.innerHTML = '<div class="tm muted">正在基于卷宗材料与案件摘要梳理证据链（约 5~15 秒）…</div>';
    J('/cases/' + encodeURIComponent(EVP.caseId) + '/evidence-chain', { method: 'POST' })
      .then(function (r) { renderEvidenceChain(r, box); toast('证据链已生成'); })
      .catch(function (e) {
        box.innerHTML = '<p style="color:var(--dg)">生成失败：' + esc(e.message) + '</p>';
        toast('失败：' + e.message);
      })
      // 无论成败都把按钮复原（放 .catch 之后等于 finally，失败路径也走得到）
      .then(function () {
        if (btn) { btn.disabled = false; btn.innerHTML = icon('scale', 'sm') + '生成证据链'; }
      });
  }

  /* ------------------------------------------------------------ 证据充分度评估
     纯增量：案件详情弹层「证据充分度评估」区，调 GET /cases/{id}/evidence-assessment。
     后端确定性出三档结论 + 致命/可补强缺口 + 补证建议 + 免责声明。 */
  function runAssessment(caseId) {
    var box = document.getElementById('assessBox');
    if (!box) return;
    var btn = document.querySelector('[data-assess]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>评估中…'; }
    box.innerHTML = '<div class="tm muted">正在基于证据链评估充分度…</div>';
    J('/cases/' + encodeURIComponent(caseId) + '/evidence-assessment')
      .then(function (r) { renderAssessment(r, box); if (btn) { btn.disabled = false; btn.innerHTML = '重新评估'; } })
      .catch(function (e) {
        box.innerHTML = '<p style="color:var(--dg)">评估失败：' + esc(e.message) + '</p>';
        if (btn) { btn.disabled = false; btn.innerHTML = '评估'; }
        toast('失败：' + e.message);
      });
  }
  function renderAssessment(r, box) {
    var m = r.metrics || {};
    var fatal = r.fatal_gaps || [];
    var fix = r.fixable_gaps || [];
    var sugg = r.suggestions || [];
    var tierTag = r.tier_tag === 'dg' ? 'dg' : (r.tier_tag === 'wn' ? 'wn' : 'ps');
    var metricHtml = '<div class="as-metrics">'
      + asMetric('主张总数', m.total_claims) + asMetric('有证据支撑', m.supported_claims)
      + asMetric('直接证据', m.direct_claims) + asMetric('带缺口', m.gap_claims)
      + asMetric('无证据', m.unsupported_claims) + '</div>';
    var fatalHtml = fatal.length
      ? '<div class="as-gap fatal"><div class="as-gap-h">致命缺口（核心主张无证据）</div>'
        + fatal.map(function (g) {
            return '<div class="as-gap-it"><b>' + esc(g.claim) + '</b>：' + esc(g.gap) + '</div>';
          }).join('') + '</div>'
      : '';
    var fixHtml = fix.length
      ? '<div class="as-gap fix"><div class="as-gap-h">可补强缺口</div>'
        + fix.map(function (g) {
            return '<div class="as-gap-it"><b>' + esc(g.claim) + '</b>：' + esc(g.gap) + '</div>';
          }).join('') + '</div>'
      : '';
    var suggHtml = sugg.length
      ? '<div class="as-sugg"><div class="as-gap-h">补证建议</div><ul>'
        + sugg.map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ul></div>'
      : '';
    box.innerHTML = '<div class="as-tier tag ' + tierTag + '">' + esc(r.tier || '—') + '</div>'
      + '<p class="as-reason">' + esc(r.tier_reason || '') + '</p>'
      + metricHtml + fatalHtml + fixHtml + suggHtml
      + '<p class="as-disc">' + esc(r.disclaimer || 'AI 辅助判断，非法律意见，最终以律师意见为准。') + '</p>';
  }
  function asMetric(label, val) {
    return '<div class="as-m"><div class="as-mv">' + esc(val === undefined ? '—' : val) + '</div>'
      + '<div class="as-ml">' + esc(label) + '</div></div>';
  }

  /* ------------------------------------------------------------ 胜诉成功率
     案件详情弹层「胜诉成功率」区，只对诉讼案件出现（它长在 evidenceBlocks 里）。

     **数字与理由分家**：百分比是后端 score_win_rate 按证据链纯确定性算的，
     AI 那半只讲举证责任/构成要件/下一步取什么证，schema 里一个数字字段都没有 ——
     所以两段永远不会互相打架。界面上也要把这一点写出来（win-cap / win-sub 的措辞），
     否则用户会以为下面那段法律分析是在解释上面的数字。 */
  function loadWinAssessment(caseId) {
    var box = document.getElementById('winBox');
    if (!box) return;
    J('/cases/' + encodeURIComponent(caseId) + '/win-assessment')
      .then(function (r) { renderWinAssessment(r, box); })
      .catch(function (e) {
        box.innerHTML = '<p class="muted">暂无法读取胜诉测算：' + esc(e.message) + '</p>';
      });
  }

  function runWinAssessment(caseId) {
    var box = document.getElementById('winBox');
    if (!box) return;
    var btn = document.querySelector('[data-win]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>测算中…'; }
    box.innerHTML = '<div class="tm muted">正在按证据链测算，并请模型做法律分析…</div>';
    J('/cases/' + encodeURIComponent(caseId) + '/win-assessment', { method: 'POST' })
      .then(function (r) {
        renderWinAssessment(r, box);
        if (btn) { btn.disabled = false; btn.innerHTML = '重新测算'; }
      })
      .catch(function (e) {
        box.innerHTML = '<p style="color:var(--dg)">测算失败：' + esc(e.message) + '</p>';
        if (btn) { btn.disabled = false; btn.innerHTML = '测算'; }
        toast('失败：' + e.message);
      });
  }

  function winLevelTag(level) {
    if (level === 'direct') return tag('直接证据', 'ps');
    if (level === 'indirect') return tag('间接证据', 'wn');
    return tag('尚无证据', 'dg');
  }

  function renderWinAssessment(r, box) {
    if (!r.supported) {
      box.innerHTML = '<p class="mat-empty">' + esc(r.message || '胜诉把握评估只对诉讼案件开放。') + '</p>';
      return;
    }
    // 「还没法算」与「算了是 0 分」是两回事，分开说 —— 混在一起用户会以为功能坏了。
    if (r.status === 'no_chain') {
      box.innerHTML = '<p class="mat-empty">' + esc(r.message || '尚未生成证据链，无法测算。')
        + '</p><p class="as-disc">' + esc(r.disclaimer || '') + '</p>';
      return;
    }

    var m = r.metrics || {};
    var a = r.analysis || {};
    var score = Number(r.score || 0);
    // 分数越低越红：用的是全站既有的三档色（ps 绿 / wn 橙 / dg 红）
    var tone = score >= 70 ? 'ps' : (score >= 40 ? 'wn' : 'dg');
    var covered = Number(m.direct || 0) + Number(m.indirect || 0);

    var head = '<div class="win-head">'
      + '<span class="win-num" style="color:var(--' + tone + ')">'
      + score + '<span class="u">%</span></span>'
      + tag(covered + ' / ' + (m.total || 0) + ' 条主张有支撑', tone)
      + '</div>'
      + '<p class="win-cap">证据覆盖率 ' + covered + ' / ' + (m.total || 0)
      + ' 条主张有支撑（直接 ' + (m.direct || 0) + ' · 间接 ' + (m.indirect || 0)
      + ' · 无证据 ' + (m.unsupported || 0) + '）'
      + '<br>' + esc(r.caliber || '') + '</p>';

    var metrics = '<div class="as-metrics">'
      + asMetric('主张总数', m.total) + asMetric('直接证据', m.direct)
      + asMetric('间接证据', m.indirect) + asMetric('无证据', m.unsupported)
      + asMetric('带缺口', m.gap) + '</div>';

    // 逐条主张：让用户看见"哪一条在拖后腿"，而不只是一个总分
    var per = (r.per_claim || []).map(function (p) {
      return '<div class="as-gap-it"><b>' + esc(p.claim) + '</b> ' + winLevelTag(p.level)
        + (p.gap ? '<div class="as-gap" style="margin:4px 0 0">' + esc(p.gap) + '</div>' : '')
        + '</div>';
    }).join('');
    var perHtml = per ? '<div class="win-sub">逐条主张</div><div class="win-model">' + per + '</div>' : '';

    var stale = r.analysis_stale
      ? '<div class="win-stale">证据链在这次分析之后又更新过，下面的分析可能已经对不上了，建议点「重新测算」。</div>'
      : '';

    var lists = [
      ['reasons', '有利因素', 'ps'],
      ['risks', '风险与对方抗辩', 'wn'],
      ['next_moves', '建议补强动作', 'ac'],
      ['decisions', '需要你拍板', 'ne'],
    ];
    var hasAny = lists.some(function (l) { return (a[l[0]] || []).length; });
    var aiBody = hasAny
      ? lists.filter(function (l) { return (a[l[0]] || []).length; }).map(function (l) {
          return '<div class="as-sugg"><div class="as-gap-h">' + esc(l[1]) + '</div><ul>'
            + a[l[0]].map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('')
            + '</ul></div>';
        }).join('')
      : '<p class="muted">还没有法律分析。点右上角「测算」生成。</p>';
    var prov = a.model
      ? '（' + esc(a.model) + (a.degraded ? '，已降级' : '') + '）'
      : '';
    var model = '<div class="win-sub">AI 法律分析' + prov + '</div>'
      + '<p class="win-cap">' + esc(r.boundary || '') + '</p>'
      + '<div class="win-model">' + aiBody + '</div>';

    box.innerHTML = head + metrics + perHtml + stale + model
      + '<p class="as-disc">' + esc(r.disclaimer || 'AI 辅助判断，非法律意见，最终以律师意见为准。') + '</p>';
  }

  /* ------------------------------------------------------------ 推进阶段
     m 从 renderCaseInto 传进来：推进成功后要就地重渲染整个弹层，
     而不是手工去改「阶段」那一行 —— 手工改会漏掉按钮态、更新时间，
     而且改了之后页面上就有一处和后端对不上了。 */
  function runStageAdvance(caseId, stage, btn, m) {
    var old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>推进中…';
    J('/cases/' + encodeURIComponent(caseId) + '/progress', {
      method: 'POST',
      json: { stage: stage, note: '人工推进：进入开庭审理阶段' },
    }).then(function () {
      toast('已推进到「' + (STAGE_LABEL[stage] || stage) + '」');
      // 1) 弹层就地重渲染：阶段行 / 推进按钮态 / 更新时间一起刷新
      if (m) reloadCaseInto(m, caseId);
      // 2) 列表页与工作台。**必须先 reloadPage 再 renderPage** ——
      //    renderPage 只在 rendered[id] 为 0 时才渲染（见它的实现），
      //    少了 reloadPage 这一步，切回去还是旧数据。
      reloadPage('pg-case'); renderPage('pg-case');
      reloadPage('pg-dash'); renderPage('pg-dash');
    }).catch(function (e) {
      btn.disabled = false; btn.innerHTML = old;
      toast('推进失败：' + e.message);
    });
  }

  /** 报告尾部的「生成证据链」：按 task 反查归属案件 → 跳案件详情并自动生成。 */
  function evchainAction(e) {
    var b = e.target.closest('[data-evchain]');
    if (b) { generateEvidenceChain(b.getAttribute('data-evchain')); return true; }
    var bt = e.target.closest('[data-evchain-task]');
    if (bt) {
      var tid = bt.getAttribute('data-evchain-task');
      var btn = bt, old = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>查找案件…';
      J('/cases/by-task/' + encodeURIComponent(tid)).then(function (c) {
        // 渲染那个按钮时还不知道归属案件的类型（要等这次反查），所以门槛在这里判：
        // AI 助手里的任务可能归到「咨询」案件上，那类案件没有证据链。
        // 合同审查页的结果按钮已经在渲染时就拿掉了（见 ops 里 noEvchain 的说明）。
        if (c.case_type !== 'litigation') {
          btn.disabled = false; btn.innerHTML = old;
          toast('该案件是「' + (TYPE_LABEL[c.case_type] || c.case_type || '未分类')
            + '」，证据链只对诉讼案件开放');
          return;
        }
        closeModal();
        switchPage('pg-case');
        openCase(c.case_id);
        generateEvidenceChain(c.case_id);
      }).catch(function () {
        btn.disabled = false; btn.innerHTML = old;
        toast('未找到该任务归属的案件，请先在案件管理中归档/新建案件，再生成证据链');
      });
      return true;
    }
    return false;
  }

  function newCaseModal() {
    var f = el('div');
    f.innerHTML = '<div class="lp-field"><label>案件编号（必填，如 2026-民初-001）</label><input name="case_id"></div>'
      + '<div class="lp-field"><label>案件名称</label><input name="title"></div>'
      + '<div class="lp-field"><label>类型</label><select name="case_type">'
      + '<option value="litigation">诉讼</option><option value="contract">非诉/合同</option>'
      + '<option value="consult">咨询</option></select></div>'
      + '<div class="lp-field"><label>案由</label><input name="cause_of_action"></div>'
      + '<div class="lp-field"><label>阶段</label><select name="stage">'
      + Object.keys(STAGE_LABEL).map(function (k) { return '<option value="' + k + '">' + STAGE_LABEL[k] + '</option>'; }).join('')
      + '</select></div>'
      + '<div class="lp-field"><label>委托人（入库前会脱敏）</label><input name="client_name"></div>';
    var m = openModal({
      title: '新建案件', body: f,
      footer: [
        btn('取消', '', closeModal),
        btn('创建', 'pr', function () {
          var val = function (n) { return (f.querySelector('[name=' + n + ']').value || '').trim(); };
          if (!val('case_id')) { toast('案件编号必填'); return; }
          J('/cases', {
            method: 'POST',
            json: {
              case_id: val('case_id'), title: val('title'), case_type: val('case_type'),
              cause_of_action: val('cause_of_action'), stage: val('stage'), client_name: val('client_name'),
            },
          }).then(function () {
            toast('案件已创建');
            closeModal();
            reloadPage('pg-case');
            renderPage('pg-case');
            reloadPage('pg-dash');
          }).catch(function (e) { toast('创建失败：' + e.message); });
        }),
      ],
    });
    return m;
  }

  /* ------------------------------------------------------------ 文书中心
     需求：**我发一段文字 → 自动识别该用哪份模板 → 套用 → 跳进编辑页**。
     模板是所里的 .docx（后端 template_service 扫描 + 渲染），本页只负责
     发起与列表；生成结果、排版、对话、下载全在 ``pg-docedit`` 那一页。

     ⚠️ ``docState`` 只留**这一页**自己的状态。某份文书的状态（正文、模板、
     fills、版本、对话记录）全在 ``docEdit`` 里 —— 两页的生命周期完全不同：
     这一页是常驻列表，那一页是"一次只开一份"。混在一起就会出"返回文书中心
     再打开另一份，标题还留着上一份的"这类问题。 */
  var docState = {
    busy: false,             // 正在套模板
    templates: [],
    // ---- 落地页（图1 那种"输入即生成"）的状态 ----
    filter: '',              // 分类胶囊：'' = 全部，否则是某个 doc_type
    pickedType: '',          // 用户点中的类型卡。**只作为"偏好类型"传给后端**，
                             // 不是 templateId —— 见 pickDocType 的注释。
    groups: {},              // 按 doc_type 分好的模板组，卡片与胶囊共用一次分组结果
    dir: '',                 // 模板目录，只在"一份模板都没有"的空状态里露出来
    parsing: false,          // 「相关材料」正在解析文件
  };

  function renderDoc() {
    loadTemplates();
    loadRecentDrafts();
    loadDocCases();

    var btn = $('#docComposeBtn');
    if (btn) btn.addEventListener('click', composeFromText);
    // 换案件要**立刻**更新那行提示：律师得在按下发送之前知道
    // "这一件能不能带证据链"，而不是等生成完了才发现没带上。
    var caseSel = $('#docCase');
    if (caseSel) caseSel.addEventListener('change', paintDocCaseHint);
    // 这里原本有个「重新扫描」按钮（`?rebuild=true`）。去掉了：模板索引的缓存戳是
    // 「文件名:mtime_ns」（template_service.py:424），改了 .docx 自动失效，
    // 那个按钮本来就是多余的；而它露在落地页上只会把人往"模板管理"上带。
    var all = $('#docAll');
    if (all) all.addEventListener('click', openDraftPicker);
    var text = $('#docText');
    if (text) {
      // Ctrl/Cmd+Enter 直接套用（与「AI 助手」一致的提交手势）
      text.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); composeFromText(); }
      });
    }

    // 分类胶囊：单选（写法照 #caseFilter）。只重画卡片网格，不重扫模板。
    var chips = $('#docChips');
    if (chips) chips.addEventListener('click', function (e) {
      var c = e.target.closest('[data-filter]');
      if (!c) return;
      docState.filter = c.getAttribute('data-filter') || '';
      var all = chips.querySelectorAll('[data-filter]');
      for (var i = 0; i < all.length; i++) all[i].classList.toggle('on', all[i] === c);
      paintDocGrid();
    });

    // 类型卡：选中哪一类就把它当"偏好类型"。
    // ⚠️ 绑定放这里（`rendered` 守卫保证 renderDoc 只跑一次），
    //    不要在 paintTemplates 里再挂一次 —— 会在每次重扫后重复叠加。
    var grid = $('#docGrid');
    if (grid) grid.addEventListener('click', function (e) {
      var dc = e.target.closest('[data-doctype]');
      if (dc) pickDocType(dc.getAttribute('data-doctype'));
    });

    // 「相关材料」：复用合同审查那条 /contract/parse 链路
    var attach = $('#docAttach'), files = $('#docFiles');
    if (attach && files) {
      attach.addEventListener('click', function () { if (!docState.parsing) files.click(); });
      files.addEventListener('change', function () {
        if (files.files && files.files.length) parseDocFiles(files.files);
        files.value = '';   // 清空才能让"同一个文件连选两次"也触发 change
      });
    }
  }

  function paintTemplates(res) {
    docState.templates = res.items || [];
    docState.dir = res.dir || '';
    // 「模板 N 份 · 劳动仲裁申请书 31 / …」原来写在这里。删掉的原因不只是"不好看"：
    // `loadRecentDrafts` 也在写同一个 `#docSub`，两个请求谁后到谁赢 ——
    // 那行字于是时隐时现。现在 `#docSub` 由 HTML 写死，谁都不覆盖。

    // 按类型分组**只做一次**，分类胶囊与卡片网格共用。
    // 只收"有模板的类型"—— 没有模板的类型放上去点了也做不出来。
    var groups = {};
    docState.templates.forEach(function (t) {
      var g = groups[t.doc_type] = groups[t.doc_type] || {
        label: t.doc_type_label, items: [], regions: [],
      };
      g.items.push(t);
      if (t.region && g.regions.indexOf(t.region) < 0) g.regions.push(t.region);
    });
    docState.groups = groups;

    // 重扫之后模板目录可能变了：原来选中的类型/筛选可能已经不存在，落回"全部"
    if (docState.filter && !groups[docState.filter]) docState.filter = '';
    if (docState.pickedType && !groups[docState.pickedType]) docState.pickedType = '';

    var chips = $('#docChips');
    if (chips) {
      chips.innerHTML = '<button class="fchip' + (docState.filter ? '' : ' on')
        + '" data-filter="">全部</button>'
        + Object.keys(groups).map(function (k) {
            return '<button class="fchip' + (docState.filter === k ? ' on' : '')
              + '" data-filter="' + esc(k) + '">' + esc(groups[k].label) + '</button>';
          }).join('');
    }
    paintDocGrid();
  }

  /** 按 `docState.filter` 画类型卡片。分类胶囊改选时**只重画这里**，不重扫模板。 */
  function paintDocGrid() {
    var grid = $('#docGrid');
    if (!grid) return;
    var groups = docState.groups || {};
    var keys = Object.keys(groups).filter(function (k) {
      return !docState.filter || k === docState.filter;
    });
    if (!keys.length) {
      grid.innerHTML = '<div class="dc"><span class="t">没有找到模板</span>'
        + '<span class="d">请把 .docx 模板放进 ' + esc(docState.dir || 'DOC_TEMPLATE_DIR')
        + ' 后重启后端</span></div>';
      return;
    }
    // 卡片只留「图标 + 类型名 + 一句说明」，跟参考图的快捷入口一致。
    // 「可用模板 31 份（上海、云南…）」和右上角那个「模板」角标都删掉了 ——
    // 那些是模板库的内部账，摆在这里只会让人以为要自己挑模板。
    grid.innerHTML = keys.map(function (k) {
      var on = docState.pickedType === k;
      return '<div class="dc' + (on ? ' on' : '') + '" data-doctype="' + esc(k) + '">'
        + '<span class="ic">' + icon(DOC_ICON[k] || 'doc', 'ic') + '</span>'
        + '<span class="t">' + esc(groups[k].label) + '</span>'
        + '<span class="d">' + esc(DOC_DESC[k] || '') + '</span>'
        + '<span class="go">' + (on ? '已选中，输入需求即可 →' : '粘贴文字开始 →')
        + '</span></div>';
    }).join('');
  }

  /** 关联案件下拉的数据源。 */
  var DOC_CASES = [];

  /** 当前选中的关联案件号（没选 = 空串）。 */
  function docCaseId() {
    var sel = $('#docCase');
    return (sel && sel.value) || '';
  }

  /** 把案件填进 `#docCase`。
   *
   * 为什么列**全部**案件、而不是只列诉讼案件：文书生成对哪种案件都成立，
   * 而证据链只对诉讼开放。把非诉案件藏起来，律师会以为"我的案子不见了"；
   * 正确做法是列出来、选中时用一行小字说清它带不了链（见 paintDocCaseHint），
   * 而**文书照常生成** —— 这正是这次需求里那句"证据链不足也可以进行文书生成"。
   */
  function loadDocCases() {
    var sel = $('#docCase');
    if (!sel) return;
    J('/cases', { query: { limit: 100 } }).then(function (res) {
      DOC_CASES = res.items || [];
      var keep = sel.value;
      sel.innerHTML = '<option value="">不关联案件（只按下面的文字起草）</option>'
        + DOC_CASES.map(function (c) {
            var label = (c.title || '（未命名案件）') + ' · ' + c.case_id
              + (TYPE_LABEL[c.case_type] ? '（' + TYPE_LABEL[c.case_type] + '）' : '');
            return '<option value="' + esc(c.case_id) + '">' + esc(label) + '</option>';
          }).join('');
      if (keep) sel.value = keep;
      paintDocCaseHint();
    }).catch(function () {
      // 案件列表拉不到**不该挡住文书生成**：下拉留空 = 不关联，照样能起草。
      sel.innerHTML = '<option value="">不关联案件（案件列表读取失败，不影响起草）</option>';
    });
  }

  function paintDocCaseHint() {
    var sel = $('#docCase'), hint = $('#docCaseHint');
    if (!sel || !hint) return;
    var id = sel.value;
    if (!id) { hint.textContent = ''; return; }
    var picked = null;
    for (var i = 0; i < DOC_CASES.length; i++) {
      if (DOC_CASES[i].case_id === id) picked = DOC_CASES[i];
    }
    hint.textContent = (picked && picked.case_type !== 'litigation')
      ? '该案不是诉讼案件：带不了证据链，文书照常生成'
      : '会带入本案证据链（主张 / 证据 / 缺口）；没有链也能生成';
  }

  function loadTemplates() {
    return J('/doc/templates').then(paintTemplates).catch(function (e) {
      var sub = $('#docSub');
      if (sub) sub.textContent = '模板接口不可用：' + e.message;
    });
  }

  /** 点类型卡：把这一类记成"偏好类型"，光标送到输入框。
   *
   * ⚠️ **这里只记类型、绝不记 templateId。** `pick_template` 的取法是
   *    「显式指定 > 类型 + 地区 > 该类型第一份空白模板」（`template_service.py:442`）——
   *    一旦把具体 templateId 传下去就**直接返回那一份、跳过地区匹配**：
   *    31 份省级仲裁申请书会永远命中默认那份，杭州的案子套上上海的模板。
   *    所以这里走 `doc_type` 这个偏好字段，地区仍由后端从案情文字里判。
   */
  function pickDocType(docType) {
    docState.pickedType = docType;
    paintDocGrid();
    var ta = $('#docText');
    if (ta) { ta.scrollIntoView({ block: 'center', behavior: 'smooth' }); setTimeout(function () { ta.focus(); }, 300); }
  }

  function composeFromText() {
    var ta = $('#docText');
    var btn = $('#docComposeBtn');
    var text = (ta && ta.value || '').trim();
    if (!text) { toast('请先把案情文字粘进来'); if (ta) ta.focus(); return; }
    if (docState.busy) return;
    docState.busy = true;
    // 发送键是个 38×38 的图标按钮，**不能把 innerHTML 换成文字**（会把图标顶掉）。
    // 忙态靠 `.send:disabled` 的降透明度 + 旁边这行状态字。
    if (btn) btn.disabled = true;
    var info = $('#docAttachInfo');
    // 这行要如实报"在干什么"：2026-10-07 起这一步**先跑一次检索 + 引用终审**
    // 再套模板，比原先多十几秒。仍然只写"正在识别模板"的话，律师会以为卡住了。
    if (info) {
      info.textContent = '正在检索法条依据、核验引用，再识别模板并填充…'
        + (docCaseId() ? '（并带入该案证据链）' : '');
    }
    J('/doc/compose', {
      method: 'POST',
      json: {
        text: text,
        // 下拉框已经收起来了，这里恒为空 = 走后端的自动识别（类型 + 地区）
        template_id: '',
        // 类型卡的偏好。留空 = 完全按文字判类型。**传的是 doc_type 不是 template_id**，
        // 理由见 pickDocType 的注释。
        doc_type: docState.pickedType || '',
        // 关联案件 → 后端据此调度该案证据链。**留空也照常起草**：
        // 链不足、没生成、案件非诉都只提示，不挡生成。
        case_id: docCaseId(),
        use_chain: true,
      },
    }).then(function (r) {
      var t = r.template || {};
      var body = String(r.content || '');
      if (!body.replace(/\s/g, '')) {
        throw new Error('模板渲染出来的正文是空的，没法起草');
      }
      // ⚠ 每次套用都要**重新算标题**：否则第二份文书会沿用上一份的标题（实测踩过）。
      var title = (t.doc_type_label || DOC_NAME[r.doc_type] || '文书') + ' · ' + nowStamp();
      var logText = composeChatText(r);
      // **先落库再跳页**：编辑页只认"已经存在的草稿 id" —— 预览、对话记录、
      // 版本历史全挂在这个 id 上，就不用再维护"还没存"的第三态。
      return J('/drafts', {
        method: 'POST',
        json: {
          content: body,
          doc_type: r.doc_type || t.doc_type || 'complaint',
          title: title,
          template_id: t.id || '',
          fills: r.fills || {},
          chat_log: [{ role: 'ai', text: logText }],
          // 记一版"首次生成"：历史里于是有一条能退回去的初始状态
          snapshot: true,
        },
      }).then(function (d) {
        toast('已套用模板：' + (t.file || '—')
          + '（填了 ' + (r.filled_count || 0) + ' 项）');
        return openDocEdit(d.draft_id, {
          from: 'pg-doc', title: title, docType: r.doc_type || '',
          templateId: t.id || '', fills: r.fills || {},
          meta: docEditMeta(r), chatLog: [{ role: 'ai', text: logText }],
        });
      });
    }).catch(function (e) {
      toast('套用失败：' + e.message);
    }).then(function () {
      docState.busy = false;
      if (btn) btn.disabled = false;
      if (info) info.textContent = '';
    });
  }

  /** 「相关材料」：把上传的 PDF / Word / 图片解析成正文，追加进输入框。
   *
   * 与合同审查页共用同一条链路：`POST /contract/parse` 受理 → worker 解析 →
   * `GET /contract/parse/{task_id}` 取正文（三步打包在 `parseUpload` 里，
   * 见 app.js 里 `ssePump` 附近那段）。**两页的差异只剩进度往哪儿写**：
   * 这一页写在 `#docAttachInfo` 那行小字上，合同页写在文件卡的状态行上。
   */
  function parseDocFiles(files) {
    if (docState.parsing) return;
    docState.parsing = true;
    var info = $('#docAttachInfo');
    var ta = $('#docText');
    var attach = $('#docAttach');
    var names = [];
    for (var i = 0; i < files.length; i++) names.push(files[i].name);
    if (info) info.textContent = '正在解析：' + names.join('、') + '…';
    if (attach) attach.disabled = true;
    // 解析已改成**后端异步任务**（受理 → worker 解析 → 取正文），所以这里写的
    // 进度是后端逐份上报的那句，不是前端自己编的"正在解析…"。
    parseUpload(files, {
      onAccepted: function (acc) {
        var bad = (acc && acc.rejected) || [];
        if (bad.length && info) {
          info.textContent = bad.map(function (x) {
            return '✗ ' + x.name + ' ' + (x.error || '未受理');
          }).join('；');
        }
      },
      onNote: function (text) { if (info) info.textContent = text; },
    })
      .then(function (res) {
        var merged = (res && res.merged) || '';
        var parts = [];
        (res.files || []).forEach(function (f) {
          // ⛔ 字数一律用后端给的 `f.chars`（不计空白），**不要退回 `f.text.length`**：
          //    同一张扫描件每次识别出的换行/空格/填空线长度都不同，text.length 每次
          //    给出的数字都不一样。这段理由与合同页 `app.js` 里那段是同一条。
          var n = f.chars || 0;
          parts.push(f.ok ? ('✓ ' + f.name + (f.text ? (' (' + n + ' 字)') : ' (无文字)'))
                          : ('✗ ' + f.name + ' ' + f.error));
        });
        if (info) info.textContent = parts.join('；');
        if (!merged) { toast('未从文件中识别出可写进文书的文本'); return; }
        if (ta) {
          var cur = ta.value.trim();
          ta.value = cur ? (cur + '\n\n' + merged) : merged;
        }
        var mn = (typeof res.merged_chars === 'number') ? res.merged_chars : merged.length;
        toast('已解析 ' + names.length + ' 份材料，合并 ' + mn + ' 字');
      })
      .catch(function (e) { toast('材料解析失败：' + e.message); })
      .then(function () {
        docState.parsing = false;
        if (attach) attach.disabled = false;
      });
  }

  /* ------------------------------------------------------------ 文书编辑页
     点「生成」不再把结果塞在文书中心下方，而是跳进这一页（``#pg-docedit``）：
     左列一条对话流，右列一张按**真模板纸张尺寸**渲染的白纸，纸上方一条富文本
     工具栏。三个口径是这一版定死的：

     1. **版式严格照模板**。正文 HTML 来自 ``GET /drafts/{id}/preview``，而后端
        那条路由与 Word 导出走的是**同一个** ``filled_template_doc()`` —— 屏幕上
        长什么样，下载下来就是什么样。刻意不走 ``render_markdown``：Markdown
        装不下表格边框、合并单元格、字体字号和页边距，26 行的两列表单会变成
        一串 ``- **栏名**``，第 3 列起在解析阶段就已经丢了。
     2. **工具栏的改动要能导出**。编辑后的 ``innerHTML`` 存进 ``content_html``，
        导出时后端走 HTML → docx/PDF 那条分支（``export_draft`` 自己判断）。
     3. **左框让 AI 改的是模板字段**（``fills``），不是自由文本 —— 所以改完
        重新渲染出来的仍然长得像那份模板。代价是**只能改模板里已有的栏目**，
        界面上必须说清楚（响应里的 ``scope: template_fields``）。

     ``docEdit.serverHtml`` 是最近一次从后端拿到的正文 HTML（浏览器归一化 + 高亮
     之后）。判断"律师有没有手动动过排版"就是拿当前 ``innerHTML`` 跟它比：动过才把
     ``content_html`` 写进库。写早了会出现"只是看了一眼、一个字没改，让 AI 改字段
     却每次都要确认覆盖手动排版"。
     ---------------------------------------------------------------------- */
  var docEdit = {
    draftId: '', title: '', docType: '', templateId: '', fills: {},
    page: null, version: 1, meta: '', loading: null,
    from: 'pg-doc',          // 返回目标：进来时 ``.pg.on`` 是哪一页
    serverHtml: '',          // 见上面那段注释
    dirty: false, busy: false,
    chatLog: [], chatRunning: false,
  };

  /** 当前显示的是哪一页。**项目没有 history / lastPage 机制**，只能读 DOM。 */
  function currentPageId() {
    var on = $('.pg.on');
    return (on && on.id) || 'pg-doc';
  }

  function setDocEditSaved(ok) {
    var t = $('#deSaved');
    if (!t) return;
    t.textContent = ok ? ('已保存 v' + (docEdit.version || 1)) : '未保存';
    t.className = 'de-sv' + (ok ? ' ok' : '');
  }

  /** 顶部那行摘要（模板 / 类型 / 地区 / 填了几项）。
   *
   *  两条来源、一套取值：刚生成时来自 ``/doc/compose`` 的返回，重新打开旧草稿时
   *  来自 ``/drafts/{id}/preview`` —— 后端的键名是**对齐过**的（见 ``_template_meta``），
   *  所以这里不用分情况。非模板型文书（两条都没有）返回空串，让顶栏留着上一次的。 */
  function docEditMeta(r) {
    if (!r) return '';
    var t = r.template || {};
    if (!r.template && !r.template_id) return '';
    var miss = r.missing_count || (r.missing || []).length || 0;
    // 引用核验只在**刚套用那次**报得出来：``evidence`` 是 ``/doc/compose`` 现算的，
    // 没跟着草稿落库（草稿表里没有这一列）。从「全部草稿」打开时这行自然消失，
    // 而不是显示一个空值 —— 那一条的依据在对话记录里翻得到。
    var ev = r.evidence || null;
    return ['模板：' + (t.file || r.template_id || '—'),
      (t.doc_type_label || r.doc_type) ? '类型：' + (t.doc_type_label || r.doc_type) : '',
      t.region ? '地区：' + t.region : '',
      '已填 ' + (r.filled_count || 0) + '/' + (r.field_count || 0) + ' 项',
      miss ? '待填 ' + miss + ' 项' : '',
      ev ? '引用核验：' + CS_CHECK_LABEL[ev.citation_check || 'pending'] : '',
      r.provider ? '模型：' + r.provider : ''].filter(Boolean).join('　·　');
  }

  /** 进入编辑页。``info`` 是已知的草稿信息（刚生成时由 compose 结果直接给，
   *  省一次请求）；打开旧草稿时为空，全部从 preview 里取。 */
  function openDocEdit(draftId, info) {
    info = info || {};
    if (!draftId) { toast('还没有文书可以编辑'); return Promise.resolve(null); }
    var from = info.from || currentPageId();
    docEdit.draftId = draftId;
    docEdit.from = (from && from !== 'pg-docedit') ? from : 'pg-doc';
    docEdit.title = info.title || docEdit.title || '未命名文书';
    docEdit.docType = info.docType || docEdit.docType || '';
    docEdit.templateId = info.templateId || '';
    docEdit.fills = info.fills || {};
    docEdit.meta = info.meta || '';
    // 换一份草稿就要换一套版本号：留着上一份的会让顶栏先显示"已保存 v7"，
    // 而这份其实才 v1，直到 preview 回来才纠正。
    docEdit.version = 1;
    docEdit.serverHtml = '';
    docEdit.dirty = false;
    docEdit.chatRunning = false;
    // 换草稿就要换一份对话。``info.chatLog`` 只有刚生成那条路径给（正文与
    // 对话一起在手上）；从「全部草稿」进来时留空，等 preview 把它带回来 ——
    // **不清空的话，上一份草稿的对话会挂在这一份下面**。
    docEdit.chatLog = info.chatLog || [];
    // ``rendered`` 是"每页只跑一次"，返回后再进不会重跑 → 先清标志再切页
    rendered['pg-docedit'] = 0;
    switchPage('pg-docedit');
    return docEdit.loading;
  }

  /** 返回。**先存再走** —— 这页没有「保存」按钮（顶栏照参考图只有返回/历史/下载），
   *  所以退出路径必须自己兜住没保存的改动。 */
  function closeDocEdit() {
    var back = docEdit.from || 'pg-doc';
    var step = function () {
      docEdit.draftId = '';
      switchPage(back);
      // ⚠️ **不能**重置 ``rendered['pg-doc']`` 来刷新列表：``renderDoc`` 里的
      // 监听器没有 bound 守卫，重跑一次就是"点一下生成发两次请求"。
      if (back === 'pg-doc') loadRecentDrafts();
    };
    if (!docEdit.dirty) { step(); return; }
    saveDocEdit().then(step, step);
  }

  /** 每次进入这一页都重跑：正文、对话、顶部摘要都要按当前草稿重画。
   *  监听器不在这里挂（见 ``bindDocEditOnce``）。 */
  function renderDocEdit() {
    paintDocEditHead();
    renderDocEditChat();
    docEdit.loading = loadDocEditBody();
  }

  /** 拉正文并渲染。``docEdit.draftId`` 必须已经设好。 */
  function loadDocEditBody() {
    var id = docEdit.draftId;
    if (!id) return Promise.resolve(null);
    return J('/drafts/' + encodeURIComponent(id) + '/preview').then(function (r) {
      docEdit.page = r.page || {};
      docEdit.templateId = r.template_id || docEdit.templateId;
      if (r.version) docEdit.version = r.version;
      if (r.title) docEdit.title = r.title;
      // 从「全部草稿」进来时没有 compose 那份信息，顶栏摘要只能靠 preview 给
      var meta = docEditMeta(r);
      if (meta) docEdit.meta = meta;
      // 对话也一并带上来了。**手上已有就听手上的**：刚跟 AI 改完一轮时，
      // 本地这份比库里的新（AI 那条回复还走在补存请求上），采信服务端会把
      // 刚出现的那句话从面板上抹掉。
      if (!docEdit.chatLog.length && (r.chat_log || []).length) {
        docEdit.chatLog = r.chat_log;
        renderDocEditChat();
      }
      docEdit.dirty = false;
      setDocEditHtml(r.html || '');
      paintDocEditHead();
      setDocEditSaved(true);
      return r;
    }).catch(function (e) {
      toast('文书加载失败：' + e.message);
      setDocEditHtml('<p>' + esc('正文加载失败：' + e.message) + '</p>');
      return null;
    });
  }

  function paintDocEditHead() {
    var t = $('#deTitle'), m = $('#deMeta');
    if (t) t.textContent = docEdit.title || '未命名文书';
    if (m) m.textContent = docEdit.meta || '';
    setDocEditSaved(!docEdit.dirty);
  }

  /** 把后端给的 HTML 放进编辑器。
   *
   *  ⚠️ 这里是全站唯一一处**用 innerHTML 直插后端 HTML** 的地方 —— app.js 开头
   *  那条「绝不用 innerHTML 直插后端文本」的规矩在这里必须让路：这一页的正文
   *  本来就是富文本，转义了台上就只剩一堆标签字符。所以门槛放到**服务端**：
   *  ``content_html`` 存库前过白名单净化器，读出来再净化一次
   *  （见 ``html_sanitize`` 的模块注释）。前端可以是旧的、可以被绕过，服务端不会。
   *  除这一处之外，本页所有文本一律走 esc() / textContent。
   */
  function setDocEditHtml(html) {
    var doc = $('#deDoc');
    if (!doc) return;
    doc.innerHTML = html || '';
    highlightPlaceholders(doc);
    // 基准取**归一化 + 高亮之后**的结果：直接拿传进来的字符串当基准的话，
    // 第一次 input 事件就会因为浏览器的归一化差异被判成"改过了"。
    docEdit.serverHtml = doc.innerHTML;
    applyDocEditPaper();
  }

  /** 白纸：宽高与四边留白按模板 ``sectPr`` 的英寸值来。
   *  不照做的话，A4 的起诉状和 Letter 的仲裁申请书在屏幕上一样大，
   *  跟下载到的 Word 对不上。 */
  function applyDocEditPaper() {
    var paper = $('#dePaper');
    if (!paper) return;
    var p = docEdit.page || {};
    var num = function (v, d) { var n = Number(v); return (isFinite(n) && n > 0) ? n : d; };
    paper.style.width = num(p.width, 8.27) + 'in';
    paper.style.minHeight = num(p.height, 11.69) + 'in';
    paper.style.padding = num(p.margin_top, 1) + 'in ' + num(p.margin_right, 1) + 'in '
      + num(p.margin_bottom, 1) + 'in ' + num(p.margin_left, 1) + 'in';
  }

  /** 把还没填的占位（＿＿ / __）高亮成淡黄底。
   *
   *  这件事之前只以底部一行「待填 6 项：请求事项、事实与理由…」的文字列表存在，
   *  律师得自己一行行去正文里找。高亮是**视图层**的：``class`` 不在净化白名单里，
   *  存库时会被摘掉，所以不怕把标记写进正文。
   */
  function highlightPlaceholders(root) {
    var RE = /[＿_]{2,}/;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var hits = [], n;
    while ((n = walker.nextNode())) {
      var par = n.parentNode;
      if (par && par.classList && par.classList.contains('de-miss')) continue;
      if (RE.test(n.nodeValue || '')) hits.push(n);
    }
    hits.forEach(function (node) {
      var frag = document.createDocumentFragment();
      String(node.nodeValue || '').split(/([＿_]{2,})/).forEach(function (part) {
        if (!part) return;
        if (RE.test(part)) {
          var s = document.createElement('span');
          s.className = 'de-miss';
          s.textContent = part;
          frag.appendChild(s);
        } else {
          frag.appendChild(document.createTextNode(part));
        }
      });
      if (node.parentNode) node.parentNode.replaceChild(frag, node);
    });
  }

  /* ---- 左列：对话流（最初那条起草需求 + 每次 AI 改动，都存进草稿的 chat_log） ---- */
  function pushDocEditChat(role, text, quiet) {
    docEdit.chatLog.push({ role: role, text: String(text || '') });
    // 封顶：chat_log 是**一整列 JSON**，不封顶的话聊得越多每存一次越慢
    if (docEdit.chatLog.length > 60) docEdit.chatLog = docEdit.chatLog.slice(-60);
    if (!quiet) renderDocEditChat();
  }

  function renderDocEditChat() {
    var box = $('#deChat');
    if (!box) return;
    box.innerHTML = '';
    var items = docEdit.chatLog || [];
    if (!items.length) {
      box.innerHTML = '<div style="color:var(--tt);font-size:12.5px;line-height:1.7">'
        + '还没有对话。生成文书后这里会显示模板命中情况；也可以直接说一句'
        + '「把请求事项第 2 条改成 80000 元」让 AI 改栏目。</div>';
      return;
    }
    items.forEach(function (it) {
      var mine = it.role === 'user';
      // 用户说的话一律纯文本；AI 那句走 mdLite（它自己先转义再拼标记），
      // 与 AI 助手页同一个渲染口径，不引入第二条渲染路径。
      bubble(mine ? String(it.text || '') : mdLite(String(it.text || '')), mine, box);
    });
    stick(box, true);
  }

  /** 起草完成那条摘要。**每个数字都来自服务端**，不让模型自己总结"我写好了" ——
   *  它没数过字段；而"以为填好了、其实缺三栏"是这份文书最容易出的事。 */
  function composeChatText(r) {
    var t = (r && r.template) || {};
    var missing = (r && r.missing) || [];
    var lines = ['已按模板起草完成：' + (t.file || '—'), ''];
    lines.push('- 类型：' + (t.doc_type_label || r.doc_type || '—')
      + (t.region ? '（' + t.region + '）' : ''));
    lines.push('- 已填 ' + (r.filled_count || 0) + '/' + (r.field_count || 0) + ' 项'
      + (missing.length ? '，待填 ' + missing.length + ' 项' : ''));
    if (missing.length) {
      lines.push('- 待补栏目：' + missing.slice(0, 10).join('、')
        + (missing.length > 10 ? ' 等' : ''));
    }
    if (r.note) lines.push('- ' + r.note);

    // ---- 案件证据链（2026-10-12）----
    // 关联了案件时，起草会顺带调度该案证据链。**两种结果都要如实说**：
    // 带上了就说带上了几条主张、几处缺口；没带上就说清原因，并明确**文书已照常生成** ——
    // 只写"没带上"的话，律师会以为这次生成是失败的、要重来一遍。
    var ch = (r && r.chain) || null;
    if (ch && ch.case_id) {
      if (ch.available) {
        lines.push('- 证据链：本案 ' + ch.claims + ' 条主张'
          + (ch.gap_count ? '，其中 ' + ch.gap_count + ' 条有缺口' : '，无缺口')
          + (ch.generated ? '（本次起草时生成）' : ''));
        (ch.gaps || []).slice(0, 3).forEach(function (g) { lines.push('  - 缺口：' + g); });
      } else {
        lines.push('- 证据链：未带入（' + (ch.reason || '未关联案件') + '）—— 文书已照常生成，'
          + '事实与理由请人工核对');
      }
    }

    // ---- 依据与缺口（2026-10-07 起）----
    // 套模板之前先跑了一段 Agent 编排（检索 + 引用终审），产出在这里交代给律师。
    // **放在这条对话里而不是新开一块面板**：它跟着草稿的 chat_log 一起落库，
    // 下次从「全部草稿」打开这份文书，依据还在对话记录里翻得到 ——
    // 而新开的那个块只能活在"刚生成完"那一次，重开就没了。
    var ev = (r && r.evidence) || null;
    if (ev) {
      var nS = (ev.statutes || []).length;
      var ck = CS_CHECK_LABEL[ev.citation_check || 'pending'];
      lines.push('- 法条依据：' + (nS ? nS + ' 条' : '本次未检索到') + '（引用核验：' + ck + '）');
      var laws = (ev.statutes || []).slice(0, 6).map(shortLaw).filter(Boolean);
      if (laws.length) lines.push('  - ' + laws.join('、') + (nS > 6 ? ' 等' : ''));
      (ev.evidence_gap || []).slice(0, 3).forEach(function (g) {
        var text = typeof g === 'string' ? g
          : [(g.kind ? GAP_LABEL[g.kind] || g.kind : ''), g.reason || g.note || g.detail || '',
            g.impact ? '（影响：' + g.impact + '）' : ''].filter(Boolean).join('：');
        if (text) lines.push('- 证据缺口：' + text);
      });
    }

    // ---- 执行摘要与关键决策（2026-10-07 起）----
    // 这一页是**四个入口里唯一不走 SSE 的**（``/doc/compose`` 一次性返回 JSON），
    // 所以它没有"过程中"这个时刻 —— 别的页面靠 ``decision`` 帧实时推，
    // 它只能把决策随最终 body 一起带回来（``RunOutcome.decisions``）。
    // 落到这条对话记录里而不是新开一块：它跟着草稿的 chat_log 一起落库，
    // 下次从「全部草稿」打开还翻得到，与上面「依据与缺口」同理。
    // ``summary`` 里的步数取 ``used_nodes`` —— 实际跑过几个节点，
    // 与 ``steps``（回环会让它变大）不是一回事。
    if (r.summary) lines.push('- ' + r.summary);
    (r.decisions || []).slice(0, 3).forEach(function (d) {
      var t = (d && d.text) || '';
      if (t) lines.push('  - ' + t);
    });

    lines.push('');
    lines.push('正文里高亮的「＿＿」就是还没填的栏目。要改内容直接说一句就行，例如'
      + '「把请求事项第 2 条改成 80000 元」——**只能改模板里已有的栏目**，'
      + '往正文中间插一整段新论述是做不到的。');
    return lines.join('\n');
  }

  /* ---- 右列：富文本工具栏 ---- */
  /** 选区快照。
   *
   *  ``execCommand`` 作用于**当前选区**，而点工具栏上的 ``<select>`` /
   *  ``<input type=color>`` 会把焦点移出编辑器、选区随之丢掉 —— 于是"选了一段
   *  字再点颜色"什么都不会发生。所以边改选区边记一份 Range，执行命令前还原。
   *  （按钮类可以靠 mousedown 阻止默认行为保住焦点，原生取色器不行。）
   */
  var deSel = null;
  function saveDocEditSel() {
    var doc = $('#deDoc');
    if (!doc) return;
    var s = window.getSelection();
    if (!s || !s.rangeCount) return;
    var r = s.getRangeAt(0);
    if (doc.contains(r.commonAncestorContainer)) deSel = r.cloneRange();
  }
  function restoreDocEditSel() {
    var doc = $('#deDoc');
    if (!doc) return null;
    doc.focus();
    if (deSel) {
      var s = window.getSelection();
      if (s) { s.removeAllRanges(); s.addRange(deSel); }
    }
    return doc;
  }

  /** 执行一条排版命令，返回浏览器是否认这条命令。
   *
   *  ⚠️ ``document.execCommand`` 已被标为废弃。取舍：当前主流浏览器仍完整支持，
   *  而本项目是零依赖零构建的前端（引 Quill / Tiptap 要连带打包产物与构建步骤）。
   *  将来真要换，**换掉这一个函数就够了** —— 存库、净化、导出都只认 HTML 字符串。
   *
   *  ``css=true`` 时开 ``styleWithCSS``：颜色与字体必须产出 ``<span style="…">``，
   *  否则 ``foreColor`` 会吐 ``<font color=…>``，而 ``font`` 不在净化白名单里，
   *  存库时标签被摘掉、颜色**静默消失**。字号那条例外，见 ``deFontSize``。
   */
  function deCmd(cmd, val, css) {
    var doc = restoreDocEditSel();
    if (!doc) return false;
    var ok = false;
    try { document.execCommand('styleWithCSS', false, !!css); } catch (e) { /* 个别实现不认，忽略 */ }
    try { ok = document.execCommand(cmd, false, val === undefined ? null : val); }
    catch (e) { ok = false; }
    if (!ok) { toast('这条排版命令当前浏览器不支持：' + cmd); return false; }
    markDocEditDirty();
    return true;
  }

  /** 字号。``execCommand('fontSize')`` 只能选 1~7 档，产出的 ``<font size=7>``
   *  折算不出磅值。老办法：按最大档插入，再把这些 ``<font>`` 换成带 pt 的
   *  ``<span>`` —— 不换的话标签会被净化器摘掉，字号静默消失。 */
  function deFontSize(pt) {
    var doc = restoreDocEditSel();
    if (!doc) return;
    try { document.execCommand('styleWithCSS', false, false); } catch (e) { /* 忽略 */ }
    document.execCommand('fontSize', false, '7');
    var fs = doc.querySelectorAll('font[size="7"]');
    for (var i = 0; i < fs.length; i++) {
      var sp = document.createElement('span');
      sp.style.fontSize = pt + 'pt';
      while (fs[i].firstChild) sp.appendChild(fs[i].firstChild);
      fs[i].parentNode.replaceChild(sp, fs[i]);
    }
    markDocEditDirty();
  }

  function markDocEditDirty() {
    docEdit.dirty = true;
    setDocEditSaved(false);
    scheduleDocEditSave();
  }

  /** 自动保存（防抖 1.2s）。这页没有保存按钮，改动必须自己落库；返回与下载
   *  两条路径还会各自强制存一次。 */
  var deSaveTimer = null;
  function scheduleDocEditSave() {
    clearTimeout(deSaveTimer);
    deSaveTimer = setTimeout(function () {
      saveDocEdit().catch(function () { /* 已经提示过了 */ });
    }, 1200);
  }

  /** 律师是否手动动过排版（当前 HTML 与从后端拿到的那份不一致）。 */
  function docEditEdited() {
    var doc = $('#deDoc');
    return !!doc && doc.innerHTML !== docEdit.serverHtml;
  }

  function saveDocEdit() {
    var id = docEdit.draftId;
    if (!id) return Promise.resolve('');
    clearTimeout(deSaveTimer);
    var doc = $('#deDoc');
    if (!doc) return Promise.resolve(id);
    // innerText 是"去掉排版标记的正文"，正好是 content 该有的样子
    var text = String(doc.innerText || '');
    if (!text.replace(/\s/g, '')) { toast('正文是空的，这次没有保存'); return Promise.resolve(id); }
    var body = { content: text, title: docEdit.title || '未命名文书', chat_log: docEdit.chatLog };
    if (docEdit.templateId) body.template_id = docEdit.templateId;
    // 只有真动过排版才写 content_html。写早了会让预览一直优先给这份"手动版"，
    // 紧接着 AI 改字段每次都要弹覆盖确认 —— 明明一个字都没改。
    if (docEditEdited()) body.content_html = doc.innerHTML;
    return J('/drafts/' + encodeURIComponent(id), { method: 'PUT', json: body })
      .then(function (row) {
        docEdit.dirty = false;
        if (row.version) docEdit.version = row.version;
        setDocEditSaved(true);
        return id;
      }).catch(function (e) {
        toast('保存失败：' + e.message);
        throw e;
      });
  }

  function downloadDocEdit(fmt) {
    if (docEdit.busy) return;
    if (!docEdit.draftId) { toast('文书还没保存，无法导出'); return; }
    docEdit.busy = true;
    toast(fmt === 'pdf' ? '正在生成 PDF…' : '正在生成 Word…');
    var doc = $('#deDoc');
    // 存之前先把判断与 HTML 取下来：存成功后 html 与 serverHtml 可能就不再"不同"了，
    // 那时再判断会误判成"没编辑过"，把律师的排版丢掉。
    var edited = docEditEdited();
    var html = doc ? doc.innerHTML : '';
    saveDocEdit().then(function (id) {
      if (!id) throw new Error('保存没成功');
      var meta = {};
      // 带了就以这份为准（"导我屏幕上这一份"）；没手动编辑过就不带，
      // 让后端走模板回填那条保真度最高的分支。
      if (edited) meta.content_html = html;
      if (docEdit.templateId) {
        meta.template_id = docEdit.templateId;
        meta.fills = docEdit.fills || {};
      }
      return J('/drafts/' + encodeURIComponent(id) + '/exports?fmt=' + fmt,
        { method: 'POST', json: meta });
    }).then(function () {
      return req('/drafts/' + encodeURIComponent(docEdit.draftId) + '/download');
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.blob().then(function (blob) {
        var name = (docEdit.title || '文书') + '.' + (fmt === 'pdf' ? 'pdf' : fmt);
        downloadBlob(blob, name);
        toast('已开始下载 ' + name);
      });
    }).catch(function (e) { toast('下载失败：' + e.message); })
      .then(function () { docEdit.busy = false; });
  }

  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 2000);
  }

  /* ---- 左框发一句话让 AI 改栏目 ---- */
  function sendDocEditInstruction() {
    var ta = $('#deChatText');
    var text = (ta && ta.value || '').trim();
    if (!text) return;
    if (docEdit.chatRunning) return;
    if (!docEdit.draftId) { toast('这份文书还没保存，无法按栏目修改'); return; }
    if (!docEdit.templateId) {
      toast('这份文书不是套模板生成的，没有可按栏目修改的字段；请直接在右边改正文');
      return;
    }
    ta.value = '';
    docEdit.chatRunning = true;
    var btn = $('#deChatSend');
    if (btn) btn.disabled = true;
    pushDocEditChat('user', text);
    // 先落库再改：手动排版过的内容必须先存下来，服务端才能在"这次改动会覆盖排版"时
    // 如实回 409，而不是拿库里那份旧的去比、默默把屏幕上的改动冲掉。
    Promise.resolve()
      .then(function () { return docEdit.dirty ? saveDocEdit() : docEdit.draftId; })
      .then(function () { return reviseDocEdit(text); })
      .catch(function (e) { toast('修改失败：' + e.message); })
      .then(function () { docEdit.chatRunning = false; if (btn) btn.disabled = false; });
  }

  /** 调 ``/drafts/{id}/revise``。``force`` 只在用户确认过"覆盖手动排版"之后才为真。
   *
   *  这里没有用 ``J()``，因为要单独认出 **409**：它不是失败，是服务端在问
   *  "这份文书有手动排版，确定要按栏目重排吗" —— 该弹确认框而不是弹错误提示。 */
  function reviseDocEdit(instruction, force) {
    var box = $('#deChat');
    var typing = box ? el('div', 'msg') : null;
    if (typing && box) {
      typing.innerHTML = '<span class="bav">' + icon('scale', 'sm') + '</span>'
        + '<div class="bb"><div class="tp3"><i></i><i></i><i></i></div></div>';
      box.appendChild(typing);
      stick(box, true);
    }
    var drop = function () { if (typing && typing.parentNode) typing.parentNode.removeChild(typing); };
    return req('/drafts/' + encodeURIComponent(docEdit.draftId) + '/revise', {
      method: 'POST',
      json: { instruction: instruction, force: !!force, chat_log: docEdit.chatLog },
    }).then(function (r) {
      if (r.status === 409) {
        drop();
        return confirmBox('会覆盖手动排版',
          '这份文书的正文被手动排版过。按栏目重排会覆盖那些改动：金额、日期这类'
          + '字段会更新，而你手动调的字体与对齐会回到模板原本的样子。',
          '仍要修改').then(function (yes) {
            if (!yes) { pushDocEditChat('ai', '已取消，文书没有改动。'); return null; }
            return reviseDocEdit(instruction, true);
          });
      }
      if (!r.ok) {
        return r.text().then(function (t) {
          var d = '';
          try { d = JSON.parse(t).detail || ''; } catch (e) { d = ''; }
          throw new Error(d || ('HTTP ' + r.status));
        });
      }
      return r.json().then(function (out) {
        drop();
        // ``reply`` 由服务端按**实际改动的栏目**生成，不采信模型自述 ——
        // 模型说"已修改"而一处没对上，是最容易让人以为改成了的情况。
        pushDocEditChat('ai', out.reply + (out.degraded
          ? '\n\n（模型这次不可用，是规则通道处理的，结果可能不完整。）' : ''));
        if (out.changed_count) {
          docEdit.fills = out.fills || {};
          docEdit.dirty = false;
          if (out.version) docEdit.version = out.version;
          // 服务端重渲染时已经把手动 HTML 清掉了，屏幕上这份就是新的模板版式
          if (out.html) { setDocEditHtml(out.html); setDocEditSaved(true); }
          else loadDocEditBody();
          persistDocEditChatLog();
        }
        return out;
      });
    }).catch(function (e) { drop(); throw e; });
  }

  /** 把**含 AI 那条回复**的完整对话记录补存一次。
   *
   *  为什么还要单独一次：revise 接口存的是**它收到的那份** chat_log（只有用户那句），
   *  回复是它返回后才产生的。不补这一步，刷新页面 AI 的话就没了。
   *  内容没变时后端不会涨版本（``unchanged``），所以不会污染历史。 */
  function persistDocEditChatLog() {
    var doc = $('#deDoc');
    if (!docEdit.draftId || !doc) return;
    J('/drafts/' + encodeURIComponent(docEdit.draftId), {
      method: 'PUT',
      json: {
        content: String(doc.innerText || ''),
        chat_log: docEdit.chatLog,
      },
    }).catch(function () { /* 只是对话记录没补上，不值得打断用户 */ });
  }

  /* ---- 历史版本 ---- */
  var DE_VSOURCE = { compose: '首次生成', ai_revise: 'AI 修改', manual: '手动编辑' };
  var DE_VKIND = { compose: 'ac', ai_revise: 'wn', manual: 'ne' };

  function openDocEditVersions() {
    if (!docEdit.draftId) { toast('文书还没保存，还没有历史版本'); return; }
    // ⚠️ 状态行写**行内样式**，不用 ``class="muted"`` —— 那个类在 index.html 里
    // 从未定义过（见 ``#docCard`` 上方那段注释），项目里 30 多处写着它但一直是
    // 个空招式。老代码不动，新代码不再往上加。
    var m = openModal({
      title: '历史版本', wide: true,
      body: '<p style="color:var(--tt);font-size:12.5px;line-height:1.7">正在读取…</p>',
    });
    J('/drafts/' + encodeURIComponent(docEdit.draftId) + '/versions').then(function (res) {
      var items = res.items || [];
      m.body.innerHTML = '';
      if (!items.length) {
        m.body.innerHTML = '<p style="color:var(--tt);font-size:12.5px;line-height:1.7">'
          + '还没有历史版本。首次生成、每次 AI 改动、每次恢复都会各记一条；'
          + '纯打字自动保存不记 —— 否则历史会被逐字稿刷满，想找「上一版」反而找不到。</p>';
        return;
      }
      var wrap = el('div');
      items.forEach(function (v) {
        var row = el('div', 'drow');
        row.innerHTML = '<span class="ic">' + icon('clock', 'sm') + '</span>'
          + '<span class="tx"><div class="tt">v' + esc(String(v.version))
          + '　' + tag(DE_VSOURCE[v.source] || v.source || '保存', DE_VKIND[v.source] || 'ne')
          + (v.version === docEdit.version ? '　' + tag('当前', 'ps') : '')
          + '</div><div class="mm">'
          + esc([v.note || '（无备注）', (v.content_chars || 0) + ' 字',
            fmtTime(v.created_at)].join(' · ')) + '</div></span>';
        var btns = el('span', 'ops');
        var restore = el('button', 'btn sm', '恢复');
        restore.addEventListener('click', function () {
          confirmBox('恢复到 v' + v.version + '？',
            '当前内容会被覆盖。恢复本身也会记一版，所以恢复错了还能再退回来。', '恢复')
            .then(function (yes) {
              if (!yes) return;
              J('/drafts/' + encodeURIComponent(docEdit.draftId)
                + '/versions/' + encodeURIComponent(v.id) + '/restore', { method: 'POST' })
                .then(function () {
                  closeModal();
                  docEdit.dirty = false;
                  toast('已恢复到 v' + v.version + '（这一版本身也记进了历史）');
                  return loadDocEditBody();
                }).catch(function (e) { toast('恢复失败：' + e.message); });
            });
        });
        btns.appendChild(restore);
        row.appendChild(btns);
        wrap.appendChild(row);
      });
      m.body.appendChild(wrap);
    }).catch(function (e) {
      m.body.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>';
    });
  }

  /** 编辑页的**一次性**绑定。``renderDocEdit`` 每次进入都会跑（要重新拉正文），
   *  但监听器只能挂一次 —— 挂两次的后果是"点一下撤销退两步"。 */
  function bindDocEditOnce() {
    var back = $('#deBack');
    if (back) back.addEventListener('click', closeDocEdit);
    var hist = $('#deHist');
    if (hist) hist.addEventListener('click', openDocEditVersions);
    var w = $('#deWord');
    if (w) w.addEventListener('click', function () { downloadDocEdit('docx'); });
    var p = $('#dePdf');
    if (p) p.addEventListener('click', function () { downloadDocEdit('pdf'); });

    var tools = $('#deTools');
    if (tools) {
      // 按钮：按下时阻止默认行为 → 编辑器不失焦、选区不丢
      tools.addEventListener('mousedown', function (e) {
        if (e.target.closest('.de-tb')) e.preventDefault();
      });
      tools.addEventListener('click', function (e) {
        var b = e.target.closest('.de-tb');
        if (!b || b.disabled) return;
        var cmd = b.getAttribute('data-cmd');
        if (!cmd) return;
        // 撤销/重做不涉及颜色字体，``css`` 传 false 保持默认产出
        deCmd(cmd, null, cmd !== 'undo' && cmd !== 'redo');
      });
    }
    // 三个下拉都是"**应用一次**的动作"，不是当前格式的指示器：``execCommand``
    // 那一套读不回光标处的真实状态（queryCommandValue 各浏览器返回值不一致），
    // 与其显示一个可能是错的当前值，不如应用完就归位。
    var block = $('#deBlock');
    if (block) block.addEventListener('change', function () {
      if (block.value) deCmd('formatBlock', '<' + block.value + '>', false);
      block.selectedIndex = 0;
    });
    var font = $('#deFont');
    if (font) font.addEventListener('change', function () {
      if (font.value) deCmd('fontName', font.value, true);
      font.selectedIndex = 0;
    });
    var size = $('#deSize');
    if (size) size.addEventListener('change', function () {
      if (size.value) deFontSize(size.value);
      size.selectedIndex = 0;
    });
    var col = $('#deColor');
    if (col) col.addEventListener('input', function () { deCmd('foreColor', col.value, true); });
    var hi = $('#deHilite');
    if (hi) hi.addEventListener('input', function () {
      // Firefox 只认 hiliteColor、部分老实现只认 backColor；两个都试
      if (!deCmd('hiliteColor', hi.value, true)) deCmd('backColor', hi.value, true);
    });

    var doc = $('#deDoc');
    if (doc) {
      doc.addEventListener('input', markDocEditDirty);
      // 粘贴只收**纯文本**。默认行为会把来源网页的整套标签与样式一起带进来：
      // 服务端净化时大部分会被剥掉，于是"粘贴后台上看到的样子"与"存下来/导出
      // 的样子"当场分叉，而这份文书是要按模板保真的。分行按段落插入。
      doc.addEventListener('paste', function (e) {
        if (!e.clipboardData) return;
        var t = e.clipboardData.getData('text/plain');
        if (t === null || t === undefined) return;
        e.preventDefault();
        deCmd('insertHTML', String(t).replace(/\r\n/g, '\n').split('\n').map(function (ln) {
          return ln ? '<p>' + esc(ln) + '</p>' : '<p><br/></p>';
        }).join(''), false);
      });
    }

    var cta = $('#deChatText');
    if (cta) cta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendDocEditInstruction(); }
    });
    var cs = $('#deChatSend');
    if (cs) cs.addEventListener('click', sendDocEditInstruction);

    document.addEventListener('selectionchange', saveDocEditSel);
    // 关页面/切到后台时兜一下没保存的改动。**这只是第二道保险** ——
    // 卸载过程中发出去的 fetch 不保证送达，主路径是自动保存与「返回」时保存。
    window.addEventListener('beforeunload', function () {
      if (docEdit.dirty && docEdit.draftId) saveDocEdit();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden' && docEdit.dirty && docEdit.draftId) saveDocEdit();
    });
  }

  function confirmBox(title, message, okText) {
    return new Promise(function (resolve) {
      var m = openModal({
        title: title,
        body: '<p>' + esc(message) + '</p>',
        footer: [
          btn('取消', '', function () { m.close(); resolve(false); }),
          btn(okText || '确定', 'pr', function () { m.close(); resolve(true); }),
        ],
      });
    });
  }

  function loadRecentDrafts() {
    var card = $('#docCard');
    if (!card) return Promise.resolve();
    return J('/drafts', { query: { limit: 8 } }).then(function (res) {
      var drafts = res.items || [];
      var all = $('#docAll');
      if (all) all.textContent = '全部 ' + (drafts.length || 0) + ' 份（最近）';
      $$('#docCard > *').forEach(function (n) { if (!n.classList.contains('ch')) n.remove(); });
      var holder = el('div');
      holder.innerHTML = drafts.length ? drafts.map(function (d) {
        var meta = [(DOC_NAME[d.doc_type] || d.doc_type || ''),
          'v' + (d.version || 1), (d.content_chars || 0) + ' 字'].filter(Boolean).join(' · ');
        return '<div class="drow" style="cursor:pointer" data-draft="' + esc(d.draft_id) + '">'
          + '<span class="ic">' + icon(DOC_ICON[d.doc_type] || 'doc', 'sm') + '</span>'
          + '<span class="tx"><div class="tt">' + esc(truncate(d.title || '（无标题）', 40)) + '</div>'
          + '<div class="mm">' + esc(meta) + '</div></span>'
          + (d.exported ? tag('已导出', 'ps') : tag('未导出', 'ne'))
          + '<span class="up">' + esc(fmtTime(d.updated_at)) + '</span>'
          + '<span class="ops">'
          + '<button class="ibtn" data-draft-open="' + esc(d.draft_id) + '" title="打开">' + icon('edit', 'sm') + '</button>'
          + '<button class="ibtn" data-draft-dl="' + esc(d.draft_id) + '" title="下载 Word">' + icon('dl', 'sm') + '</button>'
          + '<button class="ibtn" data-draft-del="' + esc(d.draft_id) + '" title="删除">' + icon('cross', 'sm') + '</button>'
          + '</span></div>';
      }).join('') : '<div class="tm" style="padding:12px 0;color:var(--tt)">还没有文书。'
        + '把案情写进上面的框里，点发送即可生成第一份。</div>';
      while (holder.firstChild) card.appendChild(holder.firstChild);
    }).catch(function (e) {
      var sub = $('#docSub');
      if (sub) sub.textContent = '草稿接口不可用：' + e.message;
    });
  }

  function bindDraftRowActions() {
    var card = $('#docCard');
    if (!card || card.dataset.bound) return;
    card.dataset.bound = '1';
    card.addEventListener('click', function (e) {
      var del = e.target.closest('[data-draft-del]');
      if (del) {
        e.stopPropagation();
        var id = del.getAttribute('data-draft-del');
        confirmBox('删除这份文书？', '删除后不可恢复。', '删除').then(function (yes) {
          if (!yes) return;
          J('/drafts/' + encodeURIComponent(id), { method: 'DELETE' }).then(function () {
            toast('已删除');
            // 删的正好是编辑页打开着的那份 → 清掉它的 id，否则返回后那一页
            // 还在按一个已经不存在的草稿拉预览，只会弹"文书加载失败"。
            if (docEdit.draftId === id) docEdit.draftId = '';
            loadRecentDrafts();
          }).catch(function (err) { toast('删除失败：' + err.message); });
        });
        return;
      }
      var dl = e.target.closest('[data-draft-dl]');
      if (dl) {
        e.stopPropagation();
        var did = dl.getAttribute('data-draft-dl');
        J('/drafts/' + encodeURIComponent(did) + '/exports?fmt=docx', { method: 'POST' })
          .then(function () { return req('/drafts/' + encodeURIComponent(did) + '/download'); })
          .then(function (res) { return res.blob(); })
          .then(function (b) { downloadBlob(b, '文书_' + did + '.docx'); toast('已开始下载'); })
          .catch(function (err) { toast('下载失败：' + err.message); });
        return;
      }
      var op = e.target.closest('[data-draft-open]') || e.target.closest('[data-draft]');
      if (op) openDraft(op.getAttribute('data-draft-open') || op.getAttribute('data-draft'));
    });
  }

  /** 打开一份已保存的草稿 → **直接进编辑页**，不再往文书中心下方塞结果卡。
   *  模板 / fills / 版本 / 对话记录全由那一页自己按 draft_id 拉，这里不预填 ——
   *  预填一份、页面再拉一份，两处迟早对不上。 */
  function openDraft(id) {
    return openDocEdit(id, { from: 'pg-doc' });
  }

  /** 「全部草稿」：本设计没有独立草稿页，用弹层给完整列表 + 打开/下载/删除。 */
  function openDraftPicker() {
    var m = openModal({ title: '我的文书草稿', body: '<p class="muted">正在读取…</p>', wide: true });
    J('/drafts', { query: { limit: 50 } }).then(function (res) {
      var drafts = res.items || [];
      m.body.innerHTML = '';
      var wrap = el('div');
      if (!drafts.length) {
        wrap.innerHTML = '<p class="muted">还没有草稿。</p>';
      } else {
        drafts.forEach(function (d) {
          var row = el('div', 'drow');
          row.innerHTML = '<span class="ic">' + icon(DOC_ICON[d.doc_type] || 'doc', 'sm') + '</span>'
            + '<span class="tx"><div class="tt">' + esc(d.title || '（无标题）') + '</div>'
            + '<div class="mm">' + esc([DOC_NAME[d.doc_type] || d.doc_type, 'v' + (d.version || 1),
              (d.content_chars || 0) + ' 字', fmtTime(d.updated_at)].join(' · ')) + '</div></span>'
            + (d.exported ? tag('已导出', 'ps') : tag('未导出', 'ne'));
          var openBtn = el('button', 'btn sm', '打开');
          openBtn.addEventListener('click', function () { closeModal(); openDraft(d.draft_id); });
          var delBtn = el('button', 'btn sm', '删除');
          delBtn.style.color = 'var(--dg)';
          delBtn.addEventListener('click', function () {
            confirmBox('删除这份文书？', '删除后不可恢复。', '删除').then(function (yes) {
              if (!yes) return;
              J('/drafts/' + encodeURIComponent(d.draft_id), { method: 'DELETE' }).then(function () {
                toast('已删除');
                closeModal();
                loadRecentDrafts();
                if (docEdit.draftId === d.draft_id) docEdit.draftId = '';
              });
            });
          });
          var ops = el('span', 'ops');
          ops.appendChild(openBtn);
          ops.appendChild(delBtn);
          row.appendChild(ops);
          wrap.appendChild(row);
        });
      }
      m.body.appendChild(wrap);
    }).catch(function (e) { m.body.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>'; });
  }

  var DOC_ICON = {
    complaint: 'doc', defense: 'edit', arbitration_application: 'gavel',
    legal_opinion: 'scale', appeal: 'doc', revision: 'edit', opinion: 'gavel',
  };
  var DOC_DESC = {
    complaint: '法院表单式：当事人信息 / 诉讼请求 / 事实与理由 / 证据清单',
    defense: '针对对方诉请逐条抗辩，含答辩意见与依据',
    arbitration_application: '仲裁委表单式：申请人 / 被申请人 / 仲裁请求 / 事实理由',
    legal_opinion: '结构化论证并给出结论，附法律依据清单',
    appeal: '针对一审裁判理由归纳上诉请求与理由',
    revision: '逐条给出合同修改建议与对照',
    opinion: '围绕争议焦点组织代理意见论证',
  };
  var DOC_NAME = {
    complaint: '民事起诉状', defense: '民事答辩状', arbitration_application: '劳动仲裁申请书',
    legal_opinion: '法律意见书', appeal: '民事上诉状', revision: '合同修改稿', opinion: '代理意见书',
  };

  /* ------------------------------------------------------------ 类案检索 */

  /** 落地页那三张场景卡。
   *
   *  ⛔ 内容**只写本项目语料覆盖得住的**（劳动法 + 合同）。参考图里那三条
   *  （火车 Z00 次、强制拆迁、黄金珠宝店账号）是另一家的语料，照搬过来点一下
   *  大概率一条都命中不了 —— 而律师试一次就不会再点第二下，比不摆还糟。
   *
   *  ``q`` 是发出去的检索式：**写全称、带争点词**。别图省事写"劳动争议"这种
   *  泛词 —— 后端是语义 + 关键词混合检索，泛词会召回一堆不相干的案子。
   *  ``focus`` / ``pick`` 是卡里那两步的说明文案，形状照参考图。 */
  var CS_SCENES = [
    {
      brief: '2023 年 7 月，李某某入职某科技公司任工程师。试用期届满前，公司以'
        + '「不符合录用条件」解除劳动合同，未提前通知，也未支付任何赔偿。',
      q: '违法解除劳动合同 赔偿金 2N 试用期内以不符合录用条件解除是否合法',
      focus: '争议焦点：试用期解除的合法性认定与赔偿金计算基数',
      cause: '劳动争议',
      pick: '各级人民法院审理并在中国裁判文书网发布的案例'
    },
    {
      brief: '王某某离职后入职竞品公司，原单位以违反竞业限制为由，要求其返还已付'
        + '补偿金并支付违约金；而双方劳动合同里并未约定竞业限制条款。',
      q: '竞业限制 未约定补偿金 违约金 条款效力认定 补偿标准',
      focus: '争议焦点：未约定补偿的竞业限制条款是否有效',
      cause: '劳动争议',
      pick: '各级人民法院审理并在中国裁判文书网发布的案例'
    },
    {
      brief: '某建筑公司逾期竣工 90 天，发包方按合同约定主张日万分之五的违约金，'
        + '承包方主张该约定明显过高，请求法院予以酌减。',
      q: '建设工程施工合同 逾期竣工 违约金过高 酌减 实际损失',
      focus: '争议焦点：违约金约定过高的判断标准与酌减幅度',
      cause: '建设工程施工合同纠纷',
      pick: '各级人民法院审理并在中国裁判文书网发布的案例'
    }
  ];

  // 检索范围。**后端没有 filter 参数可传**：入参只有 query / top_k / case_id，
  // 所以范围词是**折进检索式**做关键词补强，不是硬过滤 ——
  // 面板里那句说明就是照这个事实写的，别改成"筛选"。
  // （``top_k`` 是真参数，但它是"要几条"，不是"要哪一类"。
  //   2026-10-07 之前连它都没有通路，CaseNode 里写死 5，面板选了不生效。）
  /** 引用终审的三种结果。**三态都要有名字**：漏了 ``pending`` 会让
   *  "本次没跑终审"在页面上显示成 ``undefined``，读起来像接口坏了。 */
  var CS_CHECK_LABEL = { passed: '通过', failed: '有拦截', pending: '未执行' };
  var CS_CAUSES = ['劳动争议', '买卖合同纠纷', '建设工程施工合同纠纷', '民间借贷纠纷'];
  var CS_COURTS = ['最高人民法院', '高级人民法院', '中级人民法院'];
  var CS_TOPKS = [6, 10, 20];
  var csScope = { causes: [], courts: [], topk: 6 };

  /** 范围词（案由在前、法院层级在后）。**顺序固定**：后端有查询缓存，
   *  同一套选择每次拼出来的检索式必须一模一样，否则等于每次都是新查询。 */
  function csScopeWords() { return csScope.causes.concat(csScope.courts); }

  function csQuery(base) {
    return [String(base || '').trim()].concat(csScopeWords()).filter(Boolean).join(' ');
  }

  function csPaintScenes() {
    var box = $('#sScenes');
    if (!box) return;
    box.innerHTML = CS_SCENES.map(function (s, i) {
      return '<div class="cs-case" data-i="' + i + '">'
        + '<p class="brief">' + esc(s.brief) + '</p>'
        + '<div class="steps">'
        + '<div class="st"><i class="dot"></i>理解问题</div>'
        + '<div class="sd"><span>' + esc(s.focus) + '</span></div>'
        + '<div class="st"><i class="dot"></i>选取高相关案例</div>'
        + '<div class="sd">' + tag('案由 · ' + s.cause, 'ac')
        + '<span>' + esc(s.pick) + '</span></div>'
        + '</div></div>';
    }).join('');
  }

  function csPaintScope() {
    var chips = function (box, list, key) {
      if (!box) return;
      box.innerHTML = list.map(function (w) {
        var on = csScope[key].indexOf(w) >= 0;
        return '<button class="fchip' + (on ? ' on' : '') + '" data-scope="' + key
          + '" data-w="' + esc(w) + '">' + esc(w) + '</button>';
      }).join('');
    };
    chips($('#sCause'), CS_CAUSES, 'causes');
    chips($('#sCourt'), CS_COURTS, 'courts');
    var tk = $('#sTopk');
    if (tk) tk.innerHTML = CS_TOPKS.map(function (n) {
      return '<button class="fchip' + (csScope.topk === n ? ' on' : '')
        + '" data-scope="topk" data-w="' + n + '">' + n + ' 条</button>';
    }).join('');
    // 已选范围**摆在外面**（面板收起来也看得见），每一项都能摘掉 ——
    // 只能加不能减的面板会把人锁死在一个检索式里。
    var pk = $('#sPicked');
    if (pk) pk.innerHTML = csScopeWords().map(function (w) {
      return '<span class="cs-pick">' + esc(w)
        + '<button data-drop="' + esc(w) + '" title="从检索式里去掉">×</button></span>';
    }).join('');
    var b = $('#sScopeBtn');
    if (b) b.classList.toggle('on', csScopeWords().length > 0);
  }

  /** 落地态 ↔ 结果态。只切类名，**不碰输入框里的字** —— 点「新检索」的人
   *  多半是想改两个字再搜一次，清空等于把他的话删了。 */
  function csSetRun(on) {
    var sec = $('#pg-search');
    if (sec) sec.classList.toggle('run', !!on);
  }

  /** 「新检索」= 把结果与检索式一起收掉、回到落地页。**必须连 ``#searchHits``
   *  一起清**：进页面时会按"结果区有没有东西"恢复结果态（见 ``renderSearch``），
   *  只切类名的话，走开再回来又会被弹回结果页，按钮看着像没生效。 */
  function csReset() {
    var hits = $('#searchHits'), foot = $('#searchFoot'), sub = $('#searchSub');
    if (hits) hits.innerHTML = '';
    if (foot) foot.textContent = '';
    if (sub) sub.textContent = '语义匹配 · 裁判要旨抽取 · 相似度排序';
    csSetRun(false);
  }

  /** 附件 → 案情描述。走合同页那条链路（PDF / Word / 图片都能认），
   *  取回文本**填进输入框让人自己删改**，不直接开跑：一份合同里真正要检索的往往
   *  只有其中一段，拿全文去搜召回质量反而更差。
   *
   *  与另外两处上传入口共用 `parseUpload`（受理 → 订阅任务进度 → 取正文）——
   *  这里的等待可能长达几分钟（扫描件要整页转写），所以进度直接写在 toast 上：
   *  这一页没有文件卡那种状态行，而"什么都没发生"是最容易让人重复点上传的界面。 */
  function csAttach(files) {
    var f = files && files[0];
    if (!f) return;
    if (f.size > CT_MAX_MB * 1024 * 1024) {
      toast('文件超过 ' + CT_MAX_MB + 'M 上限，请压缩或拆分后再上传');
      return;
    }
    toast('正在识别「' + f.name + '」…');
    parseUpload([f], {
      onNote: function (text) { toast(text); },
    }).then(function (res) {
      var good = (res.files || []).filter(function (x) { return x.ok && x.text; });
      if (!good.length) throw new Error(((res.files || [])[0] || {}).error || '没从文件里认出文字');
      var text = String(res.merged || '').trim();
      if (!text) throw new Error('文件里没有可用正文');
      var box = $('#sInput');
      if (box) box.value = text;
      toast('已从「' + f.name + '」提取 ' + (res.merged_chars || text.length) + ' 字，可删改成要检索的那一段');
    }).catch(function (e) { toast('提取失败：' + e.message); });
  }

  // ``RENDER`` 表里的函数一辈子只跑一次（``renderPage`` 的 rendered 守卫），
  // 但 ``reloadPage`` 会重跑 —— 所以画场景卡与范围面板不设防（幂等），
  // 挂监听器必须设防，否则是"点一次跑两遍检索"。
  var csBound = false;
  function renderSearch() {
    csPaintScenes();
    csPaintScope();
    // 进页面时按"有没有结果"恢复形态：跑过一次再切回来，结果还在那儿，
    // 不该把人送回落地页（那会读成"刚才那次白跑了"）。
    var h0 = $('#searchHits');
    csSetRun(!!(h0 && h0.innerHTML.trim()));
    if (csBound) return;
    csBound = true;

    var input = $('#sInput'), btn = $('#sBtn');
    if (btn) btn.addEventListener('click', function () { doSearch(input ? input.value.trim() : ''); });
    // Enter 检索、Shift+Enter 换行 —— 与 AI 助手那个 composer 同一套手感。
    if (input) input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSearch(input.value.trim()); }
    });

    var sb = $('#sScopeBtn');
    if (sb) sb.addEventListener('click', function () {
      var p = $('#sPanel');
      if (!p) return;
      p.hidden = !p.hidden;
      sb.setAttribute('aria-expanded', p.hidden ? 'false' : 'true');
    });

    // 范围面板与已选范围共用一个委托：两者都是"改一下 csScope 再重画"。
    var comp = $('#sComp');
    if (comp) comp.addEventListener('click', function (e) {
      var d = e.target.closest('[data-drop]');
      if (d) {
        var w = d.getAttribute('data-drop');
        ['causes', 'courts'].forEach(function (k) {
          var i = csScope[k].indexOf(w);
          if (i >= 0) csScope[k].splice(i, 1);
        });
        csPaintScope();
        return;
      }
      var c = e.target.closest('[data-scope]');
      if (!c) return;
      var key = c.getAttribute('data-scope'), w2 = c.getAttribute('data-w');
      if (key === 'topk') { csScope.topk = Number(w2) || 6; csPaintScope(); return; }
      var arr = csScope[key], i2 = arr.indexOf(w2);
      if (i2 >= 0) arr.splice(i2, 1); else arr.push(w2);
      csPaintScope();
    });

    var at = $('#sAttach'), file = $('#sFile');
    if (at && file) {
      at.addEventListener('click', function () { file.click(); });
      file.addEventListener('change', function () {
        csAttach(file.files);
        file.value = '';   // 清空才能让"同一个文件连选两次"也触发 change
      });
    }

    var scenes = $('#sScenes');
    if (scenes) scenes.addEventListener('click', function (e) {
      var card = e.target.closest('.cs-case');
      if (!card) return;
      var s = CS_SCENES[Number(card.getAttribute('data-i'))];
      if (!s) return;
      if (input) input.value = s.q;
      doSearch(s.q);
    });

    var back = $('#sBack');
    if (back) back.addEventListener('click', function () {
      csReset();
      if (input) input.focus();
    });
  }

  function doSearch(base) {
    var hits = $('#searchHits'), foot = $('#searchFoot'), sub = $('#searchSub');
    // **发出去的是拼过范围词的检索式**，界面上显示的也是它 —— 律师得看得出
    // "它到底拿什么去搜的"，否则命中了不相干的案例会以为是模型的问题。
    var q = csQuery(base);
    if (!q) { toast('请输入检索式'); return; }
    csSetRun(true);
    if (sub) sub.textContent = '检索式：' + truncate(q, 90);
    if (hits) hits.innerHTML = '<div class="tm" style="padding:10px 0;color:var(--tt)">检索中…</div>';
    var t0 = Date.now();
    // **走 Agent 编排**（2026-10-07）。这条原先打的是 ``/search/case`` ——
    // 那条只调一次 ``case_search`` 工具就返回原始命中，页面上既没有支撑法条、
    // 也没有案号核验结论（本项目 93 个判例**全部没有案号**，而工具通道的
    // 返回值里没有任何一处会说"这条不能作为正式引用"）。
    // 老端点保留没删，给脚本与回归测试用。
    stream('/agents/case-search/stream', {
      query: q,
      top_k: csScope.topk,
    }, {
      onFrame: function (ev, data) {
        // 关键决策一句话（约束 4 的「过程中」层）。这页**没有气泡**
        // （结果是整块替换的命中列表），所以它落在最下面那行状态栏上 ——
        // 那正是 ``检索失败：…`` 用的位置，跑起来之后会被 ``paintResults`` 覆盖。
        if (ev === 'decision') { if (foot) foot.textContent = data.text; return; }
        if (ev === 'done') { paintResults(data); return; }
        if (ev === 'error') {
          if (hits) hits.innerHTML = '';
          if (foot) foot.textContent = '检索失败：' + (data.error || '未知错误');
        }
      },
      onError: function (e) {
        if (hits) hits.innerHTML = '';
        if (foot) foot.textContent = '检索失败：' + (e.message || e);
      },
    });

    function paintResults(r) {
      var items = r.cases || [];
      var gaps = r.evidence_gap || [];
      var support = (r.statutes || []).length;
      if (foot) {
        // 底部那行如实报四件事。``citation_check`` 是新增的 —— 页面上第一次
        // 能看出"这些案子的案号有没有人核过"；未通过的角标本来就在每张卡上，
        // 这行是它的汇总。缺口只报个数，明细在任务详情里（这行放不下）。
        // ``步数`` 取 ``used_nodes`` 的条数 = 实际跑过哪几个节点（去重保序），
        // 与 ``r.steps`` 不是一回事：回环会把同一节点跑第二遍，那时步数涨、节点数不涨。
        // 这里**不再重复耗时**（``r.summary`` 里也有）—— 这行已经有 ``耗时`` 了，
        // 把整句摘要搬过来会让同一行报两遍时间。
        var nSteps = (r.used_nodes || []).length;
        foot.textContent = '命中 ' + items.length + ' 件 · 支撑法条 ' + support + ' 条 · 耗时 '
          + fmtMs(r.elapsed_ms || (Date.now() - t0))
          + (nSteps ? ' · ' + nSteps + ' 步' : '')
          + ' · 引用核验 ' + CS_CHECK_LABEL[r.citation_check || 'pending']
          + (gaps.length ? ' · 证据缺口 ' + gaps.length + ' 条' : '');
      }
      if (!hits) return;
      hits.innerHTML = items.length ? items.map(function (c) {
        var sim = Math.round((Number(c.score) || 0) * 100);
        return '<div class="hit">'
          + '<div class="sim"><b>' + sim + '%</b><span>相似度</span></div>'
          + '<div class="bd"><div class="ht">' + (c.case_no ? '<span class="no">' + esc(c.case_no) + '</span>' : '')
          + (c.court ? tag(c.court, 'ac') : '') + (c.judge_date ? tag(c.judge_date, 'ne', false) : '')
          + (c.verified === false ? tag('未通过核验', 'wn') : '') + '</div>'
          + '<p class="hold"><b>裁判要旨：</b>' + esc(c.gist || '（该案未抽取要旨）') + '</p>'
          + '<div class="hm">' + esc([c.cause_of_action, c.result ? '结果：' + truncate(c.result, 40) : '']
            .filter(Boolean).join(' · ')) + '</div></div>'
          + '<button class="btn sm" data-case-full="' + esc(c.case_no || c.case_name || '') + '">查看全文</button>'
          + '</div>';
      }).join('') : '<div class="tm" style="padding:10px 0;color:var(--tt)">没有命中。可以换成更通用的关键词，或确认案例库是否已建索引。</div>';
      // 详情缓存在内存里，点「查看全文」直接用
      SEARCH_CACHE = {};
      items.forEach(function (c) { SEARCH_CACHE[c.case_no || c.case_name || ''] = c; });
    }
  }
  var SEARCH_CACHE = {};

  function bindSearchHits() {
    var hits = $('#searchHits');
    if (!hits) return;
    hits.addEventListener('click', function (e) {
      var b = e.target.closest('[data-case-full]');
      if (!b) return;
      var c = SEARCH_CACHE[b.getAttribute('data-case-full')] || {};
      openCaseModal(c);
    });
  }
  function goSearch(q) {
    switchPage('pg-search');
    if (q) { var i = $('#sInput'); if (i) i.value = q; doSearch(q); }
  }

  /* ------------------------------------------------------------ 日程任务 */
  function renderCal() {
    var box = $('#calBox');
    if (box) box.innerHTML = '<div class="tm" style="color:var(--tt)">正在读取任务…</div>';
    J('/tasks', { query: { limit: 50 } }).then(function (res) {
      var items = res.items || [];
      var sub = $('#calSub');
      var open = items.filter(function (t) { return t.status === 'pending' || t.status === 'running'; }).length;
      if (sub) sub.textContent = '近 ' + items.length + ' 条执行记录 · 进行中 ' + open + ' 条';
      if (!box) return;
      if (!items.length) {
        box.innerHTML = '<div class="tm" style="color:var(--tt)">还没有任务。到「AI 助手」发起一次分析后会出现在这里。</div>';
        return;
      }
      // 按日期分组（后端只有 created_at，就按它分）
      var groups = {};
      items.forEach(function (t) {
        var day = (t.created_at || '').slice(0, 10) || '未知日期';
        (groups[day] = groups[day] || []).push(t);
      });
      var today = new Date();
      var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
      var todayStr = today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate());
      var yest = new Date(today.getTime() - 86400000);
      var yestStr = yest.getFullYear() + '-' + pad(yest.getMonth() + 1) + '-' + pad(yest.getDate());

      box.innerHTML = Object.keys(groups).sort().reverse().map(function (day) {
        var label = day === todayStr ? '今天 ' + day : (day === yestStr ? '昨天 ' + day : day);
        return '<div class="day"><div class="dayh"><span class="dd">' + esc(label) + '</span>'
          + '<span class="tl">' + groups[day].length + ' 条</span><span class="ln"></span></div>'
          + groups[day].map(function (t) {
            var running = t.status === 'pending' || t.status === 'running';
            var st = STATUS_TAG(t.status);
            return '<div class="ev' + (running ? ' hl' : '') + '" data-task="' + esc(t.task_id) + '" style="cursor:pointer">'
              + '<span class="tm">' + esc((t.created_at || '').slice(11, 16) || '—') + '</span>'
              + '<span class="bar2"></span>'
              + '<span class="tx"><div class="tt">' + esc(truncate(t.query || '（无请求）', 46)) + '</div>'
              + '<div class="ds">' + esc([INTENT_LABEL[t.intent] || t.intent || '自动判定',
                fmtMs(t.elapsed_ms), '置信度 ' + pct(t.confidence), t.task_id].filter(Boolean).join(' · ')) + '</div>'
              + '<div class="meta">' + st
              + (t.citation_check === 'failed' ? tag('引用未通过', 'dg') : '')
              + '</div></span></div>';
          }).join('') + '</div>';
      }).join('');
    }).catch(function (e) { fail(box, e, '读取任务失败'); });

    var rf = $('#calRefresh');
    if (rf) rf.addEventListener('click', function () { reloadPage('pg-cal'); renderPage('pg-cal'); });
  }

  function openTask(id) {
    var m = openModal({ title: '任务详情', body: '<p class="muted">正在读取…</p>', wide: true });
    J('/tasks/' + encodeURIComponent(id), { query: { with_state: 'false' } }).then(function (t) {
      var rep = (t.result && (t.result.report || t.result.draft)) || '';
      m.body.innerHTML = '<dl class="kv">'
        + kvRow('任务号', t.task_id) + kvRow('意图', INTENT_LABEL[t.intent] || t.intent)
        + kvRow('状态', t.status) + kvRow('置信度', pct(t.confidence))
        + kvRow('引用校验', t.citation_check) + kvRow('耗时', fmtMs(t.elapsed_ms))
        + kvRow('创建时间', fmtTime(t.created_at)) + kvRow('请求', t.query)
        + '</dl>'
        + ((t.evidence_gap || []).length ? '<p class="muted">证据缺口：' + esc(t.evidence_gap.join('；')) + '</p>' : '')
        + (t.error ? '<p style="color:var(--dg)">错误：' + esc(t.error) + '</p>' : '')
        + (rep ? '<p><b>结果正文</b></p><pre class="lp-pre">' + esc(rep) + '</pre>' : '<p class="muted">该任务没有正文产物。</p>');
    }).catch(function (e) { m.body.innerHTML = '<p style="color:var(--dg)">读取失败：' + esc(e.message) + '</p>'; });
  }
  function bindTaskClicks() {
    document.addEventListener('click', function (e) {
      // 证据链按钮（案件详情内的 data-evchain，报告尾部的 data-evchain-task）
      // 不套 .lp-modal 排除：它本就位于弹层内，需要被正常触发。
      if (evchainAction(e)) return;
      // 卡片 / 列表行上的 data-case
      var c = e.target.closest('[data-case]');
      if (c && !e.target.closest('.lp-modal')) { openCase(c.getAttribute('data-case')); return; }
      // 任务行（工作台待办 / 日程任务列表 / 会话里的按钮）
      var t = e.target.closest('[data-task]');
      if (t && !e.target.closest('.lp-modal')) { openTask(t.getAttribute('data-task')); }
    });
  }

  /* ------------------------------------------------------------ 假按钮接真
     设计稿上还留着几处指向"后端并没有的能力"的按钮（快速立案、通知未读、
     庭审提纲生成）。这些不能留着当摆设：要么接上真实动作，要么换成真实存在的
     同类能力。处理原则同上 —— 宁可换标签，也不留假承诺。 */
  function bindActions() {
    // 1) 「快速立案」→ 跳到案件管理并直接弹出「新建案件」表单。
    //    立案的落点本来就是案件库（原先是跳文书中心套起诉状模板，
    //    律师实际反馈：想立案时第一件事是建案卷，不是写文书）。
    var launch = $('#pg-dash .hello .btn.pr');
    if (launch) {
      launch.removeAttribute('data-toast');
      launch.addEventListener('click', function () {
        switchPage('pg-case');
        newCaseModal();
      });
    }

    // 2) 顶栏「通知」→ 最近动态；「新建」→ 真实的三件事
    $$('.tr .ibtn').forEach(function (b) {
      var t = b.getAttribute('title') || '';
      if (t === '通知') {
        b.removeAttribute('data-toast');
        var dot = b.querySelector('.dot');
        if (dot) dot.style.display = 'none';   // 默认不亮，只有真有任务在执行才亮
        b.addEventListener('click', recentModal);
        J('/tasks/stats').catch(function () { return null; }).then(function (st) {
          if (!dot || !st) return;
          var n = Number((st.by_status || {}).pending || 0) + Number((st.by_status || {}).running || 0);
          if (n > 0) { dot.style.display = ''; dot.title = n + ' 个任务正在执行'; }
        });
      }
      if (t === '新建') {
        b.removeAttribute('data-toast');
        b.addEventListener('click', newMenuModal);
      }
    });

    // 3) 快捷操作后两项：设计稿的「庭审提纲生成」后端没有这个能力，换成真实能力
    var qs = $$('#pg-dash .quick .q');
    if (qs[2]) {
      qs[2].removeAttribute('data-go');
      $('.t', qs[2]).innerHTML = icon('shield', 'sm') + '合同风险审查';
      $('.d', qs[2]).textContent = '上传合同文件，自动提取正文并逐条给风险与修改建议';
      qs[2].addEventListener('click', function () {
        switchPage('pg-contract');
      });
    }
    if (qs[3]) {
      qs[3].removeAttribute('data-go');
      $('.t', qs[3]).innerHTML = icon('gavel', 'sm') + '案件分析';
      $('.d', qs[3]).textContent = '归纳争议焦点、攻防要点与诉讼策略';
      qs[3].addEventListener('click', function () {
        switchPage('pg-ai');
      });
    }
  }

  /** 顶栏「通知」：最近的任务执行记录（真实数据，不是假未读）。 */
  function recentModal() {
    var m = openModal({ title: '最近动态', body: '<p class="muted">正在读取…</p>', wide: true });
    J('/tasks', { query: { limit: 10 } }).catch(function () { return null; }).then(function (res) {
      var items = (res && res.items) || [];
      if (!items.length) { m.body.innerHTML = '<p class="muted">还没有执行记录。</p>'; return; }
      var wrap = el('div');
      items.forEach(function (t) {
        var row = el('div', 'drow');
        row.style.cursor = 'pointer';
        row.setAttribute('data-task', t.task_id);
        row.innerHTML = '<span class="ic">' + icon('clock', 'sm') + '</span>'
          + '<span class="tx"><div class="tt">' + esc(truncate(t.query || '（无请求）', 40)) + '</div>'
          + '<div class="mm">' + esc([INTENT_LABEL[t.intent] || t.intent || '自动判定', fmtTime(t.created_at)].join(' · ')) + '</div></span>'
          + STATUS_TAG(t.status);
        row.addEventListener('click', function () { openTask(t.task_id); });
        wrap.appendChild(row);
      });
      m.body.innerHTML = '';
      m.body.appendChild(wrap);
    });
  }

  /** 顶栏「新建」：本项目真实能新建的三件事。 */
  function newMenuModal() {
    var wrap = el('div');
    [['folder', '新建案件', '登记编号、当事人、案由与阶段', function () { closeModal(); newCaseModal(); }],
      // 'pen' 不是 sprite 里的图标名（一直是渲染成空白的），改用已存在的 'edit'
      ['edit', '起草文书', '按场景生成初稿，引用可核验', function () { closeModal(); switchPage('pg-doc'); }],
      // 合同审查**只有一个入口**：合同审查页的上传落地页。
      // 这里原来弹一个"粘贴材料正文"的模态框，再跳到 AI 助手执行 ——
      // 那条路绕开了上传/解析/范本比对，走出来的结果和页面上跑的不是一回事。
      ['shield', '合同审查', '上传合同文件，自动提取正文并逐条给风险与修改建议',
        function () { closeModal(); switchPage('pg-contract'); }],
    ].forEach(function (x) {
      var b = el('div', 'drow');
      b.style.cursor = 'pointer';
      b.innerHTML = '<span class="ic">' + icon(x[0], 'sm') + '</span>'
        + '<span class="tx"><div class="tt">' + x[1] + '</div><div class="mm">' + x[2] + '</div></span>'
        + '<span class="up">' + icon('arrow', 'sm') + '</span>';
      b.addEventListener('click', x[3]);
      wrap.appendChild(b);
    });
    openModal({ title: '新建', body: wrap });
  }

  /* ------------------------------------------------------------ 技能页
   * 卡片文案用 index.html 里面向律师的精炼版本：后端 /agents 的
   * name/description 是注册表原文（"router Agent""intake Agent"、内部工具名），
   * 那是给开发者对账用的，原样端给律师就是一张调试视图（见 agents.py
   * "两份名单"的设计说明）。这里只做**存在性校验**，正常情况下不动界面。 */

  /** 技能页：后端目录为空时提示一句，其余情况保持 curated 文案不变。 */
  /* ------------------------------------------------------------ 技能页（受控渲染）
     对接后端 /api/v1/skills：顶部 = 搜索技能 + 我安装的 N（动态计数）+ 添加技能（查找/上传/创建菜单）。
     主列表默认展示已安装技能（GET /skills/mine）；查找技能切到市场视图（未启用技能，点击安装即启用）。
     搜索框过滤当前视图；启用/关闭/安装/上传/创建后自动刷新列表与计数。 */
  var skState = { view: 'mine', keyword: '', category: '', cats: [], curPage: 1, minePage: 1, marketTotal: 0, mineItems: [], marketItems: [], bound: false };

  function renderSkill() {
    skState.minePage = 1;
    skState.category = '';
    var cats = $('#skCats');
    if (cats) cats.hidden = true;
    loadSkillMine();
    bindSkillsOnce();
  }

  function bindSkillsOnce() {
    if (skState.bound) return;
    skState.bound = true;
    var search = $('#skSearch');
    if (search) {
      var timer = null;
      search.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          skState.keyword = search.value.trim();
          if (skState.view === 'market') { skState.curPage = 1; loadMarket(1); }
          else { skState.minePage = 1; renderGrid(); }
        }, 200);
      });
    }
    var count = $('#skCount');
    if (count) count.addEventListener('click', function () { enterSkillDetail(); });
    var backBtn = $('#skBack');
    if (backBtn) backBtn.addEventListener('click', function () { backFromSkillDetail(); });
    var addBtn = $('#skAdd');
    var menu = $('#skMenu');
    if (addBtn && menu) {
      addBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        menu.hidden = !menu.hidden;
      });
      menu.addEventListener('click', function (e) {
        var item = e.target.closest('.sk-menu-item');
        if (!item) return;
        var act = item.getAttribute('data-act');
        menu.hidden = true;
        if (act === 'lookup') showMarket();
        else if (act === 'upload') openSkillImport();
        else if (act === 'create') openSkillForm();
      });
      document.addEventListener('click', function () { menu.hidden = true; });
    }
    var file = $('#skFile');
    if (file) file.addEventListener('change', function () {
      var f = file.files && file.files[0];
      if (!f) return;
      handleSkillFile(f);
      file.value = '';
    });
    var grid = $('#skGrid');
    var catsBox = $('#skCats');
    if (catsBox) catsBox.addEventListener('click', function (e) {
      var chip = e.target.closest('.cat');
      if (!chip || !skState.cats.length) return;
      var v = chip.getAttribute('data-cat') || '';
      if (v === skState.category) return;
      skState.category = v;
      skState.curPage = 1;
      renderCats();
      loadMarket(1);
    });
    if (grid) grid.addEventListener('click', function (e) {
      var card = e.target.closest('.ex');
      if (!card) return;
      var id = card.getAttribute('data-id');
      if (!id) return;
      var popItem = e.target.closest('.sk-pop-item');
      if (popItem) { e.stopPropagation(); handleSkillPop(id, popItem.getAttribute('data-act'), card); return; }
      var more = e.target.closest('.sk-more');
      if (more) {
        e.stopPropagation();
        closeAllSkillPops();
        var pop = card.querySelector('.sk-pop');
        if (pop) pop.classList.toggle('on');
        return;
      }
      var pinBtn = e.target.closest('.sk-pin');
      if (pinBtn) { e.stopPropagation(); pinSkill(id, pinBtn); return; }
      var sw = e.target.closest('.sk-sw');
      if (sw) { toggleSkill(id, sw, loadSkillMine); return; }
      var tryBtn = e.target.closest('.sk-try');
      if (tryBtn) { trySkillCard(card, id); return; }
      var inst = e.target.closest('.sk-install');
      if (inst) { installSkill(id, inst); return; }
      if (skState.view === 'market') return;
      if (e.target.closest('.sk-pop')) return;
      enterSkillCard(card, id);
    });
    // 点击任意非卡内菜单区域时关闭所有技能卡菜单
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.sk-pop')) closeAllSkillPops();
    });
  }

  function closeAllSkillPops() {
    var grid = $('#skGrid');
    if (!grid) return;
    var pops = grid.querySelectorAll('.sk-pop.on');
    for (var i = 0; i < pops.length; i++) pops[i].classList.remove('on');
  }

  function pinSkill(id, btn) {
    var pinned = btn.classList.contains('on');
    btn.classList.add('busy');
    J('/skills/' + encodeURIComponent(id) + '/pin', { method: 'PUT', json: { pinned: !pinned } })
      .catch(function (e) { btn.classList.remove('busy'); toast('置顶失败：' + e.message); })
      .then(function (res) {
        if (!res) return;
        btn.classList.remove('busy');
        toast(res.pinned ? '已置顶，置顶技能排在最前' : '已取消置顶');
        loadSkillMine();
      });
  }

  function handleSkillPop(id, act, card) {
    if (act === 'chat') {
      switchPage('pg-ai');
      toast('已进入 AI 助手对话页');
    } else if (act === 'edit') {
      openSkillEditForm(id, card);
    } else if (act === 'open') {
      enterSkillCard(card, id);
    } else if (act === 'uninstall') {
      confirmUninstallSkill(id, card);
    }
  }

  function confirmUninstallSkill(id, card) {
    var nm = (($('.ex-nm', card) || {}).textContent || '未命名技能');
    var body = el('div');
    var p = el('p', null, '确定要卸载「' + nm + '」吗？卸载后将从「我安装的技能」移除，市场恢复为「安装」，可随时重新安装。');
    p.style.margin = '0';
    body.appendChild(p);
    var ok = btn('卸载', 'dng', function () {
      J('/skills/' + encodeURIComponent(id), { method: 'DELETE' })
        .catch(function (e) { toast('卸载失败：' + e.message); })
        .then(function (res) {
          if (!res) return;
          closeModal();
          toast('已卸载「' + nm + '」');
          loadSkillMine();
          if (skState.view === 'market') loadMarket(skState.curPage);
        });
    });
    var cancel = btn('取消', '', closeModal);
    openModal({ title: '卸载技能', body: body, footer: [cancel, ok] });
  }

  function openSkillEditForm(id, card) {
    var item = null;
    (skState.view === 'mine' ? skState.mineItems : []).forEach(function (x) { if (x.id === id) item = x; });
    if (!item) { toast('未找到技能数据，请刷新后重试'); return; }
    var cats = ['检索', '文书', '合同', '案件', '咨询', '知识库'];
    var curCat = String(item.category || '');
    var isOther = cats.indexOf(curCat) < 0;
    var body = el('div');
    body.innerHTML =
      '<div class="lp-field"><label>技能名 *</label><input id="skEFName" maxlength="64" value="' + esc(item.name || '') + '"></div>'
      + '<div class="lp-field"><label>分类 *</label><select id="skEFCat">'
      + cats.map(function (c) { return '<option value="' + esc(c) + '"' + (c === curCat ? ' selected' : '') + '>' + esc(c) + '</option>'; }).join('')
      + '<option value="__other__"' + (isOther ? ' selected' : '') + '>自定义分类…</option></select></div>'
      + '<div class="lp-field" id="skEFOtherWrap" style="display:' + (isOther ? '' : 'none') + '"><label>自定义分类 *</label><input id="skEFOther" maxlength="32" value="' + esc(isOther ? curCat : '') + '" placeholder="如：合规"></div>'
      + '<div class="lp-field"><label>技能描述 *</label><textarea id="skEFDesc" maxlength="2000">' + esc(item.description || '') + '</textarea></div>'
      + '<div class="lp-field"><label>标签（英文逗号分隔，可留空）</label><input id="skEFTags" maxlength="200" value="' + esc((item.tags || []).join(', ')) + '"></div>'
      + '<div class="lp-field"><label>图标 symbol id（可留空，如 i-zap）</label><input id="skEFIcon" maxlength="32" value="' + esc(item.icon || 'i-zap') + '"></div>'
      + '<div class="lp-field"><label>功能页 id（可留空，打开文件夹 / 点击卡片跳转）</label><input id="skEFPage" maxlength="32" value="' + esc(item.page || '') + '"></div>';
    var catSel = body.querySelector('#skEFCat');
    var otherWrap = body.querySelector('#skEFOtherWrap');
    catSel.addEventListener('change', function () {
      otherWrap.style.display = catSel.value === '__other__' ? '' : 'none';
    });
    var save = btn('保存', 'pr', function () {
      var name = ($('#skEFName').value || '').trim();
      var cat = catSel.value === '__other__' ? ($('#skEFOther').value || '').trim() : catSel.value;
      var desc = ($('#skEFDesc').value || '').trim();
      if (!name || !cat || !desc) { toast('请填写技能名、分类与描述'); return; }
      var tags = ($('#skEFTags').value || '').split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean);
      var payload = {
        name: name, description: desc, tags: tags, category: cat,
        icon: ($('#skEFIcon').value || 'i-zap').trim(),
        page: ($('#skEFPage').value || '').trim()
      };
      save.disabled = true;
      J('/skills/' + encodeURIComponent(id) + '/edit', { method: 'PUT', json: payload })
        .catch(function (e) { save.disabled = false; toast('保存失败：' + e.message); })
        .then(function (res) {
          if (!res) return;
          closeModal();
          toast('已保存「' + name + '」的修改');
          loadSkillMine();
          if (skState.view === 'market') loadMarket(skState.curPage);
        });
    });
    var cancel = btn('取消', '', closeModal);
    openModal({ title: '编辑技能', body: body, footer: [cancel, save] });
  }

  function enterSkillDetail() {
    skState.view = 'mine';
    skState.minePage = 1;
    skState.keyword = '';
    skState.category = '';
    var cats = $('#skCats');
    if (cats) cats.hidden = true;
    var search = $('#skSearch');
    if (search) search.value = '';
    var bar = $('#skBar');
    if (bar) bar.hidden = true;
    var detail = $('#skDetail');
    if (detail) detail.hidden = false;
    var title = $('#skViewTitle');
    if (title) title.textContent = '我安装的技能（详情）';
    var hint = $('#skHint');
    if (hint) hint.textContent = '';
    loadSkillMine();
  }

  function backFromSkillDetail() {
    var bar = $('#skBar');
    if (bar) bar.hidden = false;
    var detail = $('#skDetail');
    if (detail) detail.hidden = true;
    loadSkillMine();
  }

  function loadSkillMine() {
    J('/skills/mine').catch(function (e) {
      renderGridFail('加载失败：' + e.message);
    }).then(function (res) {
      if (!res) return;
      skState.mineItems = res.items || [];
      updateMineCount(skState.mineItems.length);
      if (skState.view === 'mine') renderGrid();
    });
  }

  function updateMineCount(n) {
    var count = $('#skCount');
    if (count) count.textContent = '我安装的 ' + n;
    var hint = $('#skHint');
    if (hint) hint.textContent = n ? '共 ' + n + ' 个已安装技能，点击卡片可使用' : '还没有已安装的技能，点击右上角“添加技能”安装或创建';
  }

  function showMarket() {
    skState.view = 'market';
    skState.keyword = '';
    skState.category = '';
    var search = $('#skSearch');
    if (search) search.value = '';
    var grid = $('#skGrid');
    if (grid) grid.innerHTML = exEmpty('正在加载技能市场…');
    var title = $('#skViewTitle');
    if (title) title.textContent = '技能市场 · 查找技能';
    var hint = $('#skHint');
    if (hint) hint.textContent = '每页 30 条；未安装的点击“安装”启用，已安装的点击“试一试”直接使用';
    loadCategories();
    loadMarket(1);
  }

  function loadCategories() {
    if (!skState.cats.length) {
      J('/skills/categories').catch(function () {}).then(function (res) {
        if (!res) return;
        skState.cats = res.categories || [];
        renderCats();
      });
    } else {
      renderCats();
    }
  }

  function renderCats() {
    var box = $('#skCats');
    if (!box) return;
    var chips = ['<button type="button" class="cat' + (skState.category ? '' : ' on') + '" data-cat="">全部</button>'];
    skState.cats.forEach(function (c) {
      chips.push('<button type="button" class="cat' + (skState.category === c ? ' on' : '') + '" data-cat="' + esc(c) + '">' + esc(c) + '</button>');
    });
    box.innerHTML = chips.join('');
    box.hidden = false;
  }

  function loadMarket(page) {
    skState.curPage = page;
    var q = { limit: 30, offset: (page - 1) * 30 };
    if (skState.keyword) q.keyword = skState.keyword;
    if (skState.category) q.category = skState.category;
    J('/skills', { query: q }).catch(function (e) {
      renderGridFail('加载失败：' + e.message);
    }).then(function (res) {
      if (!res) return;
      skState.marketItems = res.items || [];
      skState.marketTotal = res.total != null ? res.total : skState.marketItems.length;
      renderGrid();
    });
  }

  function backToMine() {
    skState.view = 'mine';
    skState.minePage = 1;
    skState.keyword = '';
    skState.category = '';
    var cats = $('#skCats');
    if (cats) cats.hidden = true;
    var search = $('#skSearch');
    if (search) search.value = '';
    var title = $('#skViewTitle');
    if (title) title.textContent = '我安装的技能';
    var hint = $('#skHint');
    if (hint) hint.textContent = '';
    loadSkillMine();
  }

  function installSkill(id, btn) {
    btn.classList.add('busy');
    J('/skills/' + encodeURIComponent(id), { method: 'PUT', json: { enabled: true } })
      .catch(function (e) { btn.classList.remove('busy'); toast('安装失败：' + e.message); })
      .then(function (res) {
        if (!res) return;
        btn.classList.remove('busy');
        var nm = '';
        var page = '';
        for (var i = 0; i < skState.marketItems.length; i++) {
          if (skState.marketItems[i].id === id) {
            nm = skState.marketItems[i].name || '';
            page = skState.marketItems[i].page || '';
            break;
          }
        }
        // 停留技能市场页，就地显示图2样式的安装成功提示；「去试试」为可点击链接
        toast('「' + esc(nm || '未命名技能') + '」技能已安装，<a href="javascript:void(0)" id="toastTryLink">去试试</a>', true);
        var tl = $('#toastTryLink');
        if (tl) {
          tl.addEventListener('click', function () {
            if (page) {
              switchPage(page);
              toast('已跳转至「' + nm + '」对应功能页');
            } else {
              toast('「' + nm + '」暂无独立功能页，可在 AI 助手中直接使用该技能');
            }
          });
        }
        // 刷新市场当前页使该卡片按钮变为「试一试」；同时同步已安装数据与计数
        loadMarket(skState.curPage);
        loadSkillMine();
      });
  }

  function renderGrid() {
    var grid = $('#skGrid');
    if (!grid) return;
    var pager = $('#skPager');
    var items = skState.view === 'mine' ? skState.mineItems : skState.marketItems;
    var kw = skState.keyword.toLowerCase();
    var list = (skState.view === 'market') ? items : (kw ? items.filter(function (it) {
      return (String(it.name || '').toLowerCase().indexOf(kw) >= 0)
        || (String(it.description || '').toLowerCase().indexOf(kw) >= 0)
        || (it.tags || []).some(function (t) { return String(t).toLowerCase().indexOf(kw) >= 0; });
    }) : items);
    if (!list.length) {
      grid.innerHTML = exEmpty(skState.view === 'mine' ? '没有匹配的已安装技能，换个关键词试试' : '没有匹配的市场技能，换个关键词试试');
      if (pager) { pager.innerHTML = ''; pager.style.display = 'none'; }
      return;
    }
    var pageItems;
    var total;
    if (skState.view === 'market') {
      pageItems = list;
      total = skState.marketTotal;
    } else {
      total = list.length;
      var pages = Math.max(1, Math.ceil(total / 15));
      if (skState.minePage > pages) skState.minePage = pages;
      pageItems = list.slice((skState.minePage - 1) * 15, skState.minePage * 15);
    }
    grid.innerHTML = pageItems.map(function (it) { return skillCardHTML(it, skState.view === 'market'); }).join('');
    if (!pager) return;
    if (skState.view === 'market') {
      renderPager(pager, { n: skState.curPage, size: 30, total: total }, function (p) { loadMarket(p); });
    } else {
      renderPager(pager, { n: skState.minePage, size: 15, total: total }, function (p) { skState.minePage = p; renderGrid(); });
    }
  }

  function renderGridFail(msg) {
    var grid = $('#skGrid');
    if (grid) grid.innerHTML = exEmpty(msg);
  }

  function toggleSkill(id, sw, refresh) {
    var on = sw.classList.contains('on');
    sw.classList.add('busy');
    J('/skills/' + encodeURIComponent(id), { method: 'PUT', json: { enabled: !on } })
      .catch(function (e) { sw.classList.remove('busy'); toast('切换失败：' + e.message); })
      .then(function (res) {
        if (!res) return;
        sw.classList.remove('busy');
        var card = sw.closest('.ex');
        var nm = card ? (($('.ex-nm', card) || {}).textContent || '') : '';
        sw.classList.toggle('on', !!res.enabled);
        sw.setAttribute('data-on', res.enabled ? '1' : '0');
        sw.title = res.enabled ? '点击关闭' : '点击启用';
        toast(res.enabled ? '已开启自动触发' : '已关闭自动触发');
        if (refresh) refresh();
      });
  }

  function trySkillCard(card, id) {
    // 已安装但已关闭：点「试一试」= 重新启用并进入，无需重新安装
    var item = null;
    (skState.view === 'market' ? skState.marketItems : skState.mineItems).forEach(function (x) {
      if (x.id === id) item = x;
    });
    if (item && item.installed && !item.enabled) {
      J('/skills/' + encodeURIComponent(id), { method: 'PUT', json: { enabled: true } })
        .catch(function () {})
        .then(function () { enterSkillCard(card, id); });
      return;
    }
    enterSkillCard(card, id);
  }

  function enterSkillCard(card, id) {
    var page = card.getAttribute('data-page') || '';
    var nm = (($('.ex-nm', card) || {}).textContent || '');
    if (page) {
      switchPage(page);
      toast('已跳转至「' + nm + '」对应功能页');
      return;
    }
    toast('「' + nm + '」暂无独立功能页，可在 AI 助手中直接使用该技能');
  }

  var SK_ICON_MORE = '<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>';
  var SK_ICON_PIN = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M8 6V4a2 2 0 0 1 4 0v2l4 4 2-1v6l-6 3-6-3v-6l2 1z"/></svg>';
  var SK_ICON_CHAT = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  var SK_ICON_EDIT = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
  var SK_ICON_FOLDER = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
  var SK_ICON_TRASH = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

  function skillCardHTML(it, market) {
    var icon = String(it.icon || 'i-zap');
    var hasIcon = !!(icon && document.getElementById(icon));
    var av = hasIcon
      ? '<svg class="ic sm"><use href="#' + esc(icon) + '"/></svg>'
      : '<svg class="ic sm"><use href="#i-zap"/></svg>';
    var grads = ['linear-gradient(135deg,#4f6ef7,#7a5cf0)', 'linear-gradient(135deg,#0ea5a4,#2563eb)',
      'linear-gradient(135deg,#f0730e,#e0447c)', 'linear-gradient(135deg,#16a34a,#0d9488)',
      'linear-gradient(135deg,#7c3aed,#db2777)', 'linear-gradient(135deg,#d97706,#dc2626)',
      'linear-gradient(135deg,#334155,#0f172a)', 'linear-gradient(135deg,#0891b2,#4f46e5)'];
    var seed = 0;
    var nm = String(it.name || '').trim();
    for (var i = 0; i < nm.length; i++) seed = (seed * 31 + nm.charCodeAt(i)) >>> 0;
    var bg = grads[seed % grads.length];
    var on = !!it.enabled;
    var inst = !!it.installed;
    var rl = (on ? '已启用' : '已关闭');
    if (inst) rl += ' · 已安装';
    if (it.author) rl += ' · ' + esc(it.author);
    var tags = (it.tags || []).map(function (t) { return '<span class="tg">' + esc(t) + '</span>'; }).join('');
    var ctrl = market
      ? (inst
          ? '<button class="sk-install sk-try" title="' + (on ? '已安装，点击直接使用' : '已安装但已关闭，点击重新启用') + '">试一试</button>'
          : '<button class="sk-install" title="安装并启用该技能">安装</button>')
      : '<div class="sk-ops">'
          + '<button type="button" class="sk-more" title="更多操作" aria-label="更多操作">' + SK_ICON_MORE + '</button>'
          + '<button type="button" class="sk-pin' + (it.pinned ? ' on' : '') + '" title="' + (it.pinned ? '取消置顶' : '置顶') + '">' + SK_ICON_PIN + '</button>'
          + '<div class="sk-sw' + (on ? ' on' : '') + '" data-on="' + (on ? '1' : '0') + '" title="' + (on ? '点击关闭' : '点击启用') + '"></div>'
        + '</div>';
    var popHtml = '';
    if (!market) {
      var popItems = ['<button type="button" class="sk-pop-item" data-act="chat">' + SK_ICON_CHAT + '去对话</button>'];
      if (it.can_edit) popItems.push('<button type="button" class="sk-pop-item" data-act="edit">' + SK_ICON_EDIT + '编辑</button>');
      popItems.push('<button type="button" class="sk-pop-item" data-act="open">' + SK_ICON_FOLDER + '打开文件夹</button>');
      popItems.push('<button type="button" class="sk-pop-item danger" data-act="uninstall">' + SK_ICON_TRASH + '卸载</button>');
      popHtml = '<div class="sk-pop">' + popItems.join('') + '</div>';
    }
    return '<div class="ex' + (market ? '' : ' ex-mine-card') + '" data-id="' + esc(it.id) + '" data-page="' + esc(it.page || '') + '">'
      + '<div class="ex-h">'
      + '<span class="ex-av" style="background:' + bg + '">' + av + '</span>'
      + '<span><div class="ex-nm">' + esc(nm || '未命名技能') + '</div><div class="ex-rl">' + rl + '</div></span>'
      + ctrl
      + '</div>'
      + '<div class="ex-ds">' + esc(it.description || '') + '</div>'
      + '<div class="ex-tags">' + tags + '<span class="tg hot">热度 ' + Number(it.hot || 0) + '</span></div>'
      + popHtml
      + '</div>';
  }

  function openSkillForm() {
    var cats = ['检索', '文书', '合同', '案件', '咨询', '知识库'];
    var body = el('div');
    body.innerHTML =
      '<div class="lp-field"><label>技能名 *</label><input id="skFName" maxlength="64" placeholder="如：证据三性审查"></div>'
      + '<div class="lp-field"><label>分类 *</label><select id="skFCat">'
      + cats.map(function (c) { return '<option value="' + esc(c) + '">' + esc(c) + '</option>'; }).join('')
      + '<option value="__other__">自定义分类…</option></select></div>'
      + '<div class="lp-field" id="skFOtherWrap" style="display:none"><label>自定义分类 *</label><input id="skFOther" maxlength="32" placeholder="如：合规"></div>'
      + '<div class="lp-field"><label>技能描述 *</label><textarea id="skFDesc" maxlength="2000" placeholder="技能能做什么、在哪些场景使用"></textarea></div>'
      + '<div class="lp-field"><label>标签（英文逗号分隔，可留空）</label><input id="skFTags" maxlength="200" placeholder="如：审查, 证据"></div>';
    var catSel = body.querySelector('#skFCat');
    var otherWrap = body.querySelector('#skFOtherWrap');
    catSel.addEventListener('change', function () { otherWrap.style.display = catSel.value === '__other__' ? '' : 'none'; });
    var save = btn('创建', 'pr', function () {
      var name = ($('#skFName').value || '').trim();
      var cat = catSel.value === '__other__' ? ($('#skFOther').value || '').trim() : catSel.value;
      var desc = ($('#skFDesc').value || '').trim();
      if (!name || !cat || !desc) { toast('请填写技能名、分类与描述'); return; }
      var tags = ($('#skFTags').value || '').split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean);
      save.disabled = true;
      J('/skills', { method: 'POST', json: { name: name, description: desc, tags: tags, category: cat, icon: 'i-zap', page: '' } })
        .catch(function (e) { save.disabled = false; toast('创建失败：' + e.message); })
        .then(function (res) {
          if (!res) return;
          closeModal();
          toast('已创建技能「' + name + '」');
          backToMine();
        });
    });
    var cancel = btn('取消', '', closeModal);
    openModal({ title: '新建技能', body: body, footer: [cancel, save] });
  }

  /* ------------------------------------------------------------ 导入技能（截图样式）
     菜单「上传技能」→ 打开「导入技能」模态：虚线拖拽上传区（点击/拖放选文件）、
     「非高风险自动安装」复选框、文件要求说明、「选择 ZIP 文件」快捷链接。
     支持的技能包：本地 .json（现有格式）或 .zip（内含 SKILL.md + YAML 元数据，
     前端解析后复用同一 POST /skills 创建启用，后端接口不变）。 */
  function openSkillImport() {
    var body = el('div');
    body.innerHTML =
      '<div class="sk-import-drop" id="skImportDrop">'
      + '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
      + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="M7 9l5-5 5 5"/>'
      + '<path d="M4 20h16"/></svg>'
      + '<div class="tx">拖拽文件或点击上传</div></div>'
      + '<label class="sk-import-auto"><input type="checkbox" id="skImportAuto">非高风险自动安装</label>'
      + '<div class="sk-import-req">文件要求</div>'
      + '<ul class="sk-import-req-list">'
      + '<li>文件夹或者.zip需要包含SKILL.md文件</li>'
      + '<li>.md 文件需包含 YAML 格式的技能名称和描述</li></ul>'
      + '<a class="sk-import-zip" id="skImportZip">选择 ZIP 文件</a>';
    var drop = body.querySelector('#skImportDrop');
    var zipLink = body.querySelector('#skImportZip');
    function pick(accept) {
      var f = $('#skFile');
      if (f) { f.accept = accept; f.click(); }
    }
    drop.addEventListener('click', function () { pick('.json,.zip,application/json'); });
    drop.addEventListener('dragover', function (e) {
      e.preventDefault();
      e.stopPropagation();
      drop.classList.add('over');
    });
    drop.addEventListener('dragleave', function () { drop.classList.remove('over'); });
    drop.addEventListener('drop', function (e) {
      e.preventDefault();
      e.stopPropagation();
      drop.classList.remove('over');
      var f0 = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f0) handleSkillFile(f0);
    });
    zipLink.addEventListener('click', function () { pick('.zip'); });
    openModal({ title: '导入技能', closeText: '×', body: body, wide: false });
  }

  function handleSkillFile(file) {
    var autoEl = $('#skImportAuto');
    var autoInstall = !!(autoEl && autoEl.checked);
    if (/\.zip$/i.test(String(file.name || ''))) importZipSkill(file, autoInstall);
    else uploadSkill(file, autoInstall);
  }

  function submitSkillPayload(payload, autoInstall, label, done) {
    J('/skills', { method: 'POST', json: payload })
      .catch(function (e) { toast('导入失败：' + e.message); })
      .then(function (res) {
        if (!res) return;
        var sid = res.skill && res.skill.id;
        if (autoInstall === false && sid) {
          J('/skills/' + encodeURIComponent(sid), { method: 'PUT', json: { enabled: false } })
            .catch(function () {})
            .then(function () {
              toast('已导入技能「' + label + '」（未自动安装，可在市场手动启用）');
              if (done) done();
            });
          return;
        }
        toast('已导入并自动启用技能「' + label + '」');
        if (done) done();
      });
  }

  function uploadSkill(file, autoInstall) {
    var rd = new FileReader();
    rd.onload = function () {
      var data = null;
      try { data = JSON.parse(rd.result); }
      catch (e) { toast('技能包解析失败：不是合法的 JSON 文件'); return; }
      var name = String(data.name || '').trim();
      var desc = String(data.description || '').trim();
      if (!name || !desc) { toast('技能包缺少 name / description 字段'); return; }
      var payload = {
        name: name,
        description: desc,
        tags: Array.isArray(data.tags) ? data.tags.map(function (t) { return String(t); }) : [],
        category: String(data.category || '咨询').trim() || '咨询',
        icon: String(data.icon || 'i-zap').trim() || 'i-zap',
        page: String(data.page || '').trim(),
        enabled: true,
      };
      submitSkillPayload(payload, autoInstall, name, function () { backToMine(); });
    };
    rd.readAsText(file, 'utf-8');
  }

  /** 读取 .zip 中根目录/任意层级的 SKILL.md，解压并返回文本 */
  function readZipSkillMd(buf) {
    var u8 = new Uint8Array(buf);
    var dv = new DataView(buf);
    if (u8.length < 22) throw new Error('不是有效的 ZIP 文件');
    var eocd = -1;
    for (var i = u8.length - 22; i >= 0; i--) {
      if (u8[i] === 0x50 && u8[i + 1] === 0x4b && u8[i + 2] === 0x05 && u8[i + 3] === 0x06) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('不是有效的 ZIP 文件');
    var count = dv.getUint16(eocd + 10, true);
    var cdOff = dv.getUint32(eocd + 16, true);
    var entries = [];
    var p = cdOff;
    for (var n = 0; n < count; n++) {
      if (dv.getUint32(p, true) !== 0x02014b50) break;
      var method = dv.getUint16(p + 10, true);
      var csize = dv.getUint32(p + 20, true);
      var nlen = dv.getUint16(p + 28, true);
      var elen = dv.getUint16(p + 30, true);
      var clen = dv.getUint16(p + 32, true);
      var lho = dv.getUint32(p + 42, true);
      var nm = new TextDecoder().decode(u8.subarray(p + 46, p + 46 + nlen));
      entries.push({ name: nm, method: method, csize: csize, lho: lho });
      p += 46 + nlen + elen + clen;
    }
    var md = null;
    for (var j = 0; j < entries.length; j++) {
      if (/^[^\/]*SKILL\.md$/i.test(entries[j].name)) { md = entries[j]; break; }
    }
    if (!md) throw new Error('ZIP 中未找到 SKILL.md');
    var lp = md.lho;
    if (dv.getUint32(lp, true) !== 0x04034b50) throw new Error('ZIP 结构损坏');
    var lnlen = dv.getUint16(lp + 26, true);
    var lelen = dv.getUint16(lp + 28, true);
    var dataStart = lp + 30 + lnlen + lelen;
    var comp = u8.subarray(dataStart, dataStart + md.csize);
    if (md.method === 0) return Promise.resolve(new TextDecoder().decode(comp));
    if (md.method === 8 && typeof DecompressionStream !== 'undefined') {
      return new Response(new Blob([comp]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).text();
    }
    return Promise.reject(new Error('不支持的压缩方式：' + md.method));
  }

  /** 解析 SKILL.md 的 YAML 头部（--- ... ---），提取 name/description/tags/category */
  function parseSkillMd(text) {
    var m = text.match(/^\s*---\s*\r?\n([\s\S]*?)\r?\n\s*---\s*/);
    if (!m) throw new Error('SKILL.md 缺少 YAML 头部（---）');
    var y = m[1];
    function lineVal(key) {
      var re = new RegExp('^\\s*' + key + '\\s*:\\s*(.*)$', 'm');
      var mm = y.match(re);
      if (!mm) return '';
      return String(mm[1]).trim().replace(/^["']|["']$/g, '');
    }
    var name = lineVal('name');
    var desc = lineVal('description');
    var category = lineVal('category');
    var tags = [];
    var tm = y.match(/^\s*tags\s*:\s*\[(.*)\]$/m);
    if (tm && tm[1].trim()) {
      tags = tm[1].split(',').map(function (s) { return s.trim().replace(/^["']|["']$/g, ''); }).filter(Boolean);
    } else {
      var started = false;
      y.split(/\r?\n/).forEach(function (ln) {
        if (/^\s*tags\s*:/.test(ln) && ln.indexOf('-') === -1) started = true;
        else if (started && /^\s*- /.test(ln)) tags.push(ln.trim().slice(2).trim().replace(/^["']|["']$/g, ''));
        else if (started && ln.trim() && !/^\s*- /.test(ln)) started = false;
      });
    }
    if (!name || !desc) throw new Error('SKILL.md 缺少 name / description 字段');
    return { name: name, description: desc, tags: tags, category: category || '咨询' };
  }

  function importZipSkill(file, autoInstall) {
    var rd = new FileReader();
    rd.onload = function () {
      var meta = null;
      readZipSkillMd(rd.result)
        .then(function (text) { return parseSkillMd(text); })
        .then(function (mm) {
          meta = mm;
          var payload = {
            name: meta.name,
            description: meta.description,
            tags: meta.tags,
            category: meta.category,
            icon: 'i-zap',
            page: '',
            enabled: true,
          };
          submitSkillPayload(payload, autoInstall, meta.name, function () { backToMine(); });
        })
        .catch(function (e) { toast('导入失败：' + (e && e.message ? e.message : 'ZIP 解析失败')); });
    };
    rd.readAsArrayBuffer(file);
  }

  /* ------------------------------------------------------------ 专家页（受控渲染）
     对接后端 /api/v1/experts：搜索 / 分类 / 排序 / 分页 / 我的专家 / 新建 / 收藏。
     页面骨架在 index.html #pg-expert，卡片复用 .ex-grid/.ex 样式。
     点击卡片：写入 window.__expert 并 switchPage('pg-ai')，AI 助手 ask() 请求体
     自动携带 chat.expertId（见 ask() 的 expert_id 字段）。 */
  var exState = { n: 1, size: 12, total: 0, keyword: '', cat: 'all', sort: 'default', cats: [], bound: false };

  function exCardHTML(ex, fav) {
    var nm = String(ex.name || '专').slice(0, 1);
    var grads = ['linear-gradient(135deg,#4f6ef7,#7a5cf0)', 'linear-gradient(135deg,#0ea5a4,#2563eb)',
      'linear-gradient(135deg,#f0730e,#e0447c)', 'linear-gradient(135deg,#16a34a,#0d9488)',
      'linear-gradient(135deg,#7c3aed,#db2777)', 'linear-gradient(135deg,#d97706,#dc2626)',
      'linear-gradient(135deg,#334155,#0f172a)', 'linear-gradient(135deg,#0891b2,#4f46e5)'];
    var seed = 0;
    for (var i = 0; i < nm.length; i++) seed = (seed * 31 + nm.charCodeAt(i)) >>> 0;
    var bg = grads[seed % grads.length];
    var tags = (ex.tags || []).map(function (t) { return '<span class="tg">' + esc(t) + '</span>'; }).join('');
    return '<div class="ex" data-id="' + esc(ex.id) + '" data-cat="' + esc(ex.category || '') + '">'
      + '<div class="ex-h">'
      + '<span class="ex-av" style="background:' + bg + '">' + esc(nm) + '</span>'
      + '<span><div class="ex-nm">' + esc(ex.name || '') + '</div><div class="ex-rl">' + esc(ex.author || '') + '</div></span>'
      + '<button class="ex-fav' + (fav ? ' on' : '') + '" data-fav="' + (fav ? '1' : '0') + '" title="' + (fav ? '取消收藏' : '收藏') + '"><svg class="ic sm"><use href="#i-star"/></svg></button>'
      + '</div>'
      + '<div class="ex-ds">' + esc(ex.description || '') + '</div>'
      + '<div class="ex-tags">' + tags + '<span class="tg hot">热度 ' + Number(ex.hot || 0) + '</span></div>'
      + '</div>';
  }

  function exEmpty(msg) {
    return '<div class="tm" style="padding:18px;color:var(--tt)">' + esc(msg) + '</div>';
  }

  function renderExperts() {
    loadExpertCats();
    loadExperts();
    loadMine();
    bindExpertsOnce();
  }

  function bindExpertsOnce() {
    if (exState.bound) return;
    exState.bound = true;
    var search = $('#exSearch');
    if (search) {
      var timer = null;
      search.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          exState.keyword = search.value.trim();
          exState.n = 1;
          loadExperts();
        }, 300);
      });
    }
    var sort = $('#exSort');
    if (sort) sort.addEventListener('change', function () {
      exState.sort = sort.value;
      exState.n = 1;
      loadExperts();
    });
    var filter = $('#exFilter');
    if (filter) filter.addEventListener('click', function (e) {
      var c = e.target.closest('.fchip');
      if (!c) return;
      $$('.fchip', filter).forEach(function (x) { x.classList.toggle('on', x === c); });
      exState.cat = c.getAttribute('data-cat') || 'all';
      exState.n = 1;
      loadExperts();
    });
    var grid = $('#exGrid');
    if (grid) grid.addEventListener('click', function (e) {
      var fav = e.target.closest('.ex-fav');
      var card = e.target.closest('.ex');
      if (!card) return;
      var id = card.getAttribute('data-id');
      if (!id) return;
      if (fav) { toggleFav(id, fav, null, loadExperts); return; }
      enterExpertCard(card, id);
    });
    var mine = $('#exMine');
    if (mine) mine.addEventListener('click', function (e) {
      var fav = e.target.closest('.ex-fav');
      var card = e.target.closest('.ex');
      if (!card) return;
      var id = card.getAttribute('data-id');
      if (!id) return;
      if (fav) { toggleFav(id, fav, null, loadMine); return; }
      enterExpertCard(card, id);
    });
    var newBtn = $('#exNew');
    if (newBtn) newBtn.addEventListener('click', openExpertForm);
    var reloadMine = $('#exMineReload');
    if (reloadMine) reloadMine.addEventListener('click', loadMine);
  }

  function loadExpertCats() {
    J('/experts/categories').catch(function () { return null; }).then(function (res) {
      exState.cats = (res && res.categories) || [];
      var box = $('#exFilter');
      if (!box) return;
      var html = '<button class="fchip' + (exState.cat === 'all' ? ' on' : '') + '" data-cat="all">全部</button>'
        + exState.cats.map(function (c) {
          return '<button class="fchip' + (exState.cat === c ? ' on' : '') + '" data-cat="' + esc(c) + '">' + esc(c) + '</button>';
        }).join('');
      box.innerHTML = html;
    });
  }

  function loadExperts() {
    var grid = $('#exGrid');
    if (!grid) return;
    grid.innerHTML = exEmpty('正在加载专家…');
    var q = {
      keyword: exState.keyword,
      category: exState.cat === 'all' ? '' : exState.cat,
      sort: exState.sort === 'default' ? '' : exState.sort,
      offset: (exState.n - 1) * exState.size,
      limit: exState.size,
    };
    J('/experts', { query: q }).catch(function (e) {
      grid.innerHTML = exEmpty('加载失败：' + e.message);
    }).then(function (res) {
      if (!res) return;
      exState.total = res.total || 0;
      var items = res.items || [];
      var count = $('#exCount');
      if (count) count.textContent = '共 ' + exState.total + ' 位';
      if (!items.length) { grid.innerHTML = exEmpty('没有匹配的专家，试试换个关键词或分类'); return; }
      grid.innerHTML = items.map(function (it) { return exCardHTML(it, !!it.favorited); }).join('');
      var pager = $('#exPager');
      if (pager) renderPager(pager, { total: exState.total, size: exState.size, n: exState.n }, function (p) {
        exState.n = p;
        loadExperts();
      });
    });
  }

  function loadMine() {
    var box = $('#exMine');
    if (!box) return;
    box.innerHTML = '<div class="tm" style="padding:8px 0;color:var(--tt)">正在读取…</div>';
    J('/experts/mine').catch(function (e) {
      box.innerHTML = '<div class="tm" style="padding:8px 0;color:var(--tt)">读取失败：' + esc(e.message) + '</div>';
    }).then(function (res) {
      if (!res) return;
      var created = res.created || [];
      var favs = res.favorites || [];
      if (!created.length && !favs.length) {
        box.innerHTML = '<div class="tm" style="padding:8px 0;color:var(--tt)">还没有自建或收藏的专家。收藏感兴趣的专家或点击「新建专家」创建一个。</div>';
        return;
      }
      var html = '';
      if (created.length) html += '<div class="ex-mine-sec">我创建的（' + created.length + '）</div>'
        + '<div class="ex-grid">' + created.map(function (it) { return exCardHTML(it, !!it.favorited); }).join('') + '</div>';
      if (favs.length) html += '<div class="ex-mine-sec">我收藏的（' + favs.length + '）</div>'
        + '<div class="ex-grid">' + favs.map(function (it) { return exCardHTML(it, true); }).join('') + '</div>';
      box.innerHTML = html;
    });
  }

  function toggleFav(id, favBtn, grid, refresh) {
    favBtn.disabled = true;
    J('/experts/' + encodeURIComponent(id) + '/favorite', { method: 'POST' }).catch(function (e) {
      favBtn.disabled = false;
      toast('收藏失败：' + e.message);
    }).then(function (res) {
      if (!res) return;
      favBtn.disabled = false;
      var on = !!res.favorited;
      favBtn.classList.toggle('on', on);
      favBtn.setAttribute('data-fav', on ? '1' : '0');
      favBtn.title = on ? '取消收藏' : '收藏';
      toast(on ? '已收藏' : '已取消收藏');
      if (refresh) refresh();
    });
  }

  function enterExpertCard(card, id) {
    // 列表与「我的专家」卡片字段已够，不需要再请求 GET /experts/{id}。
    var nm = $('.ex-nm', card) ? $('.ex-nm', card).textContent : '';
    var rl = $('.ex-rl', card) ? $('.ex-rl', card).textContent : '';
    window.__expert = { id: id, name: nm, role: rl };
    applyExpertContext();
    switchPage('pg-ai');
    toast('已进入「' + nm + '」身份，提问将带上该专家人设');
  }

  function openExpertForm() {
    var cats = exState.cats.length ? exState.cats : ['诉讼', '合同', '劳动', '知产', '合规'];
    var body = el('div');
    body.innerHTML =
      '<div class="lp-field"><label>专家职称 *</label><input id="exFName" maxlength="64" placeholder="如：劳动争议调解专家"></div>'
      + '<div class="lp-field"><label>分类 *</label><select id="exFCat">'
      + cats.map(function (c) { return '<option value="' + esc(c) + '">' + esc(c) + '</option>'; }).join('')
      + '<option value="__other__">自定义分类…</option></select></div>'
      + '<div class="lp-field" id="exFOtherWrap" style="display:none"><label>自定义分类 *</label><input id="exFOther" maxlength="32" placeholder="如：房地产"></div>'
      + '<div class="lp-field"><label>人设描述 *</label><textarea id="exFDesc" maxlength="2000" placeholder="该专家的人设、擅长领域与应答风格（进入对话后作为 system 人设注入）"></textarea></div>'
      + '<div class="lp-field"><label>标签（英文逗号分隔，可留空）</label><input id="exFTags" maxlength="200" placeholder="如：劳动仲裁, 裁员合规"></div>';
    var catSel = body.querySelector('#exFCat');
    var otherWrap = body.querySelector('#exFOtherWrap');
    catSel.addEventListener('change', function () { otherWrap.style.display = catSel.value === '__other__' ? '' : 'none'; });
    var save = btn('创建', 'pr', function () {
      var name = ($('#exFName').value || '').trim();
      var cat = catSel.value === '__other__' ? ($('#exFOther').value || '').trim() : catSel.value;
      var desc = ($('#exFDesc').value || '').trim();
      if (!name || !cat || !desc) { toast('请填写职称、分类与人设描述'); return; }
      var tags = ($('#exFTags').value || '').split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean);
      save.disabled = true;
      J('/experts', { method: 'POST', json: { name: name, category: cat, description: desc, tags: tags } })
        .catch(function (e) { save.disabled = false; toast('创建失败：' + e.message); })
        .then(function (res) {
          if (!res) return;
          closeModal();
          toast('已创建专家');
          loadExperts();
          loadMine();
          loadExpertCats();
        });
    });
    var cancel = btn('取消', '', closeModal);
    openModal({ title: '新建专家', body: body, footer: [cancel, save] });
  }

  function applyExpertContext() {
    var ex = window.__expert || null;
    var aih = $('#pg-ai .aih');
    var aihTt = aih ? $('.tt', aih) : null;
    var aihDs = aih ? $('.ds', aih) : null;
    var input = $('#input');
    if (ex && ex.id) {
      chat.expertId = ex.id;
      chat.expertName = ex.name || '';
      if (aihTt) aihTt.textContent = ex.name || '专家';
      if (aihDs) aihDs.textContent = '正在以「' + (ex.name || '专家') + '」身份对话 · ' + (ex.role || '该专家人设已接入本次对话');
      if (input) input.placeholder = '以「' + (ex.name || '专家') + '」身份提问，Enter 发送';
      ensureExpertBanner(ex);
    } else {
      chat.expertId = '';
      chat.expertName = '';
      if (aihTt) aihTt.textContent = '衡律 AI';
      if (aihDs) aihDs.textContent = '引用可溯源 · 回答仅供参考，请结合执业判断使用';
      if (input) input.placeholder = '描述案情或提出问题，Enter 发送，Shift+Enter 换行';
      removeExpertBanner();
    }
  }

  function ensureExpertBanner(ex) {
    var box = $('#chatbox');
    if (!box) return;
    var old = box.querySelector('.ex-banner');
    if (old) {
      var nm = old.querySelector('.ex-banner-nm');
      if (nm) nm.textContent = ex.name || '专家';
      return;
    }
    var b = el('div', 'ex-banner');
    var inner = el('span');
    inner.innerHTML = '当前以 <b class="ex-banner-nm">' + esc(ex.name || '专家') + '</b> 的身份回答 · 已带入该专家人设';
    var cmp = el('button', null, '对照普通版');
    cmp.addEventListener('click', function () { askComparePlain(); });
    var out = el('button', null, '退出专家');
    out.addEventListener('click', exitExpert);
    b.appendChild(inner);
    b.appendChild(cmp);
    b.appendChild(out);
    box.insertBefore(b, box.firstChild);
  }

  /** 专家会话的「对照普通版」：对同一问题发起一次不带 expert_id 的独立咨询，
   *  以对照卡渲染到对话流末尾，直观展示专家版与普通版回答的差异。
   *  独立请求不携带 conversation_id / history，不写会话、不影响后续追问链路。 */
  var _comparing = false;
  function askComparePlain() {
    if (_comparing) { toast('对照请求正在生成，请稍候'); return; }
    var input = $('#input');
    var text = (input ? input.value : '').trim();
    if (!text) { toast('请先在输入框填写要对照的问题'); return; }
    _comparing = true;
    var box = $('#chatbox');
    var bubbleBox = bubble('<div class="cmp-tag">普通助手（对照）</div><div class="tp3"><i></i><i></i><i></i></div>', false);
    var bb = $('.bb', bubbleBox);
    var started = Date.now();
    stream('/agents/assistant/stream', {
      query: text,
      case_id: '',
      history: [],
      expert_id: '',
      conversation_id: '',
    }, {
      onFrame: function (ev, data) {
        if (ev === 'done') {
          var host = dropTyping(bb);
          paintChunks(host, formatRagAnswer(answerShape(data), Date.now() - started), function () { stick(box); });
          stick(box, true);
          _comparing = false;
          return;
        }
        if (ev === 'error') {
          dropSpin(bb);
          var h = $('.ans', bb) || bb;
          h.innerHTML = '<p style="color:#c0392b">对照回答失败：' + esc(data.error || '未知错误') + '</p>';
          _comparing = false;
        }
      },
      onError: function (e) {
        dropSpin(bb);
        var h = $('.ans', bb) || bb;
        h.innerHTML = '<p style="color:#c0392b">连接中断：' + esc(e.message || e) + '（对照请求可能仍在执行）</p>';
        _comparing = false;
      },
      onEnd: function () { _comparing = false; },
    });
  }

  function removeExpertBanner() {
    var box = $('#chatbox');
    if (!box) return;
    var old = box.querySelector('.ex-banner');
    if (old) old.remove();
  }

  function exitExpert() {
    delete window.__expert;
    applyExpertContext();
    toast('已退出专家身份，恢复普通 AI 助手');
  }

  /* ------------------------------------------------------------ 监控中心
     数据源：/admin/monitor/expert-usage、/admin/monitor/skill-usage、
             /admin/monitor/usage-logs（明细：概览指标 + 健康诊断）。
     图表统一走 ECharts（web/vendor/echarts.min.js 本地引用），实例放
     monCharts 表里，重复渲染前先 dispose，避免重复 init。 */
  var monCtl = {
    skill: '', focus: '', admin: false, busy: false, interval: 10, ticks: 0, timer: 0, taskId: '',
    data: {}, err: {}, etag: {},
  };

  function monTone(kind) {
    if (kind === 'ok') return 'var(--ps)';
    if (kind === 'bad') return 'var(--dg)';
    if (kind === 'warn') return 'var(--wn)';
    if (kind === 'mute') return 'var(--tq)';
    return 'var(--ac)';
  }
  var MON_VD = [
    { key: 'pass', label: '通过', tone: 'ok' },
    { key: 'fail', label: '失败', tone: 'bad' },
    { key: 'degraded', label: '降级', tone: 'warn' },
    { key: 'unknown', label: '未知', tone: 'mute' },
  ];
  var MON_TASK_LABEL = {
    succeeded: '成功', failed: '失败', degraded: '降级', cancelled: '已取消',
    dead: '终止', pending: '待处理', running: '运行中',
  };
  function monT(v) { return esc(v === null || v === undefined || v === '' ? '—' : v); }
  /* null 必须是「—」而不是 0：Number(null) 是 0，直接转会把"还没算过收益"
     显示成"收益 0.0%"，而这在监控页上正好是两句相反的话。 */
  function monEmpty(v) { return v === null || v === undefined || v === ''; }
  function monPct(v, digits) {
    if (monEmpty(v)) return '—';
    var n = Number(v);
    return isFinite(n) ? pct(n, digits) : '—';
  }
  function monN(v, digits) {
    if (monEmpty(v)) return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return digits === undefined ? String(n) : n.toFixed(digits);
  }
  function monDur(sec) {
    if (monEmpty(sec)) return '—';
    var v = Number(sec);
    if (!isFinite(v)) return '—';
    if (v < 90) return v + ' 秒';
    if (v < 5400) return (v / 60).toFixed(1) + ' 分';
    if (v < 172800) return (v / 3600).toFixed(1) + ' 小时';
    return (v / 86400).toFixed(1) + ' 天';
  }
  function monMs(v) { return monEmpty(v) ? '—' : fmtMs(v); }
  function monHour(bucket) {
    var s = String(bucket || '');
    return s.length >= 13 ? s.slice(5, 10) + ' ' + s.slice(11, 13) + ':00' : s;
  }
  function monSet(id, html) { var n = document.getElementById(id); if (n) n.innerHTML = html; }
  function monTx(id, text) { var n = document.getElementById(id); if (n) n.textContent = text; }
  function monDot(kind) { return '<i class="mon-dt" style="background:' + monTone(kind) + '"></i>'; }
  function monKv(k, v) {
    return '<div class="mon-kv"><span class="k">' + monT(k) + '</span><span class="v">'
      + (v === undefined || v === null || v === '' ? '—' : v) + '</span></div>';
  }
  function monMm(text) { return '<p class="mon-mm">' + monT(text) + '</p>'; }

  /* 卡片级容错：所有依赖都挂了才在卡里报错，挂一半就渲染能渲染的那半 ——
     一个接口慢或挂，不该让这张卡变成一句报错。 */
  function monGuard(id, keys) {
    var bad = keys.filter(function (k) { return monCtl.err[k]; });
    if (bad.length && bad.length === keys.length) {
      monSet(id, '<p class="mon-err">读取失败：' + esc(monCtl.err[bad[0]]) + '</p>');
      return false;
    }
    return true;
  }

  /* 水平条走 CSS 而不是 SVG：SVG 的 rx 在非等比缩放下会被拉成椭圆，
     这里要的是恒定的 4px 圆角（与「2px 间隙、条 ≤24px」同一套标记规范）。 */
  function monBars(items, opts) {
    opts = opts || {};
    var rows = (items || []).filter(function (x) { return x; });
    if (!rows.length) return monMm(opts.empty || '暂无数据');
    var max = 1;
    rows.forEach(function (x) { var v = Number(x.value) || 0; if (v > max) max = v; });
    return rows.map(function (x) {
      var v = Number(x.value) || 0;
      var w = v > 0 ? Math.max(2, Math.round(v / max * 100)) : 0;
      return '<div class="mon-row"><span class="nm" title="' + monT(x.name) + '">' + monT(x.name)
        + '</span><span class="mon-bar"><i style="width:' + w + '%;background:' + monTone(x.tone)
        + '"></i></span><span class="num">' + monT(x.label === undefined ? v : x.label) + '</span></div>';
    }).join('');
  }

  /* 判定分布：色块只是第三种冗余编码 —— 红绿在红绿色盲下几乎不可分，
     所以每个分段都同时给文字和计数，颜色读不出来也能照常读表。 */
  function monStack(rows, opts) {
    opts = opts || {};
    var live = (rows || []).filter(function (x) { return (Number(x.value) || 0) > 0; });
    var total = live.reduce(function (s, x) { return s + Number(x.value); }, 0);
    if (!total) return monMm(opts.empty || '窗口内没有观测样本。');
    var segs = live.map(function (x) {
      return '<i style="flex:' + Number(x.value) + ' 0 0;background:' + monTone(x.tone)
        + '" title="' + monT(x.label + ' ' + x.value + ' 条（' + Math.round(Number(x.value) / total * 100) + '%）')
        + '"></i>';
    }).join('');
    var legend = live.map(function (x) {
      return '<span>' + monDot(x.tone) + monT(x.label) + ' ' + Number(x.value)
        + ' <span class="pc">' + Math.round(Number(x.value) / total * 100) + '%</span></span>';
    }).join('');
    return '<div class="mon-lg">' + legend + '</div><div class="mon-stk">' + segs + '</div>';
  }

  function monCols(points, opts) {
    opts = opts || {};
    if (!points || !points.length) return monMm(opts.empty || '窗口内没有吞吐样本。');
    var max = 1;
    points.forEach(function (p) { var v = Number(p.count) || 0; if (v > max) max = v; });
    var bars = points.map(function (p) {
      var v = Number(p.count) || 0;
      return '<i style="height:' + Math.max(3, Math.round(v / max * 100)) + '%" title="'
        + monT(monHour(p.bucket) + ' · ' + v + ' 条') + '"></i>';
    }).join('');
    return '<div class="mon-colax">' + bars + '</div><div class="mon-xlab"><span>'
      + monT(monHour(points[0].bucket)) + '</span><span>' + points.length + ' 个桶</span><span>'
      + monT(monHour(points[points.length - 1].bucket)) + '</span></div>';
  }

  /* 单元格一律转义；行级 tone 只用来给整行上状态色，不作为唯一编码。 */
  function monTable(head, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return monMm(opts.empty || '暂无数据');
    return '<table class="mon-tb"><thead><tr>' + head.map(function (h) {
      return '<th>' + monT(h) + '</th>';
    }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
      var cls = r.tone ? ' class="' + (r.tone === 'bad' ? 'bad' : 'warn') + '"' : '';
      return '<tr' + cls + '>' + r.cells.map(function (c) {
        return '<td>' + monT(c) + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody></table>';
  }

  /* 带 If-None-Match 的取数：后端对 overview / north-star 回了 ETag，内容没变
     就是 304，此时直接复用上一次的 JSON（连解析都省）。**不走浏览器 HTTP 缓存**
     —— 全局 req 是 no-store，缓存里留的会是某个账号的私有数据，同浏览器换账号
     会被错误复用；这里用显式条件请求，ETag 只存在 monCtl 里，随会话一起消失。 */
  function monFetch(key, path, opt) {
    var o = {};
    if (opt) {
      for (var k in opt) { if (Object.prototype.hasOwnProperty.call(opt, k)) o[k] = opt[k]; }
    }
    if (monCtl.etag[key]) {
      o.headers = Object.assign({}, o.headers, { 'If-None-Match': monCtl.etag[key] });
    }
    return req(path, o).then(function (r) {
      if (r.status === 304) return monCtl.data[key];
      if (!r.ok) {
        return r.text().then(function (txt) {
          var d = '';
          try { d = JSON.parse(txt).detail || ''; } catch (e) { d = ''; }
          throw new Error(d || ('HTTP ' + r.status));
        });
      }
      var tag = r.headers.get('ETag');
      if (tag) monCtl.etag[key] = tag; else delete monCtl.etag[key];
      return r.json();
    }).then(function (d) {
      monCtl.data[key] = d;
      delete monCtl.err[key];
      return d;
    }, function (e) {
      // 出错时丢掉 ETag：否则下次会带着一个"上一版内容"的指纹去问，
      // 服务器若判 304，就会把一个早已失效的旧快照当成最新值贴回来。
      delete monCtl.etag[key];
      monCtl.err[key] = (e && e.message) || String(e);
      monCtl.data[key] = null;
      return null;
    });
  }

  /* 只打真源接口。overview 已是全链路汇总（内嵌 north_star），不再单独打
     /evolution/north-star —— 那在后端是又一遍全量健康诊断，纯属重复。
     ledger 也没有任何卡片渲染，取来即弃，一并去掉。 */
  var MON_GLOBAL = [
    ['overview', '/evolution/overview'],
    ['meta', '/evolution/meta'],
    ['tasks', '/evolution/tasks', { query: { limit: 30 } }],
    ['audit', '/evolution/audit', { query: { limit: 40 } }],
    ['reviews', '/evolution/reviews', { query: { limit: 20 } }],
    ['taskStats', '/tasks/stats'],
    ['usageLogs', '/usage/logs', { query: { limit: 50 } }],
    ['sys', '/health'],
  ];

  function monWorst() {
    var list = ((monCtl.data.overview || {}).skills || []).slice();
    if (!list.length) return '';
    var bad = list.filter(function (x) { return x.status !== 'healthy' || (x.signals || []).length; });
    bad.sort(function (a, b) { return (b.signals || []).length - (a.signals || []).length; });
    return (bad[0] || list[0] || {}).skill_name || '';
  }
  function monFocus() { return monCtl.skill || monWorst(); }

  function monLoadSkill() {
    var skill = monFocus();
    monCtl.focus = skill;
    var jobs = [monFetch('obs', '/evolution/observations/stats',
      { query: { skill_name: monCtl.skill, hours: 24 } })];
    if (skill) {
      var enc = encodeURIComponent(skill);
      jobs.push(monFetch('health', '/evolution/health/' + enc));
      jobs.push(monFetch('cand', '/evolution/candidates/' + enc));
      // 显式 semantic=false：语义归并首次调用要加载向量模型，看板不该有这种副作用
      jobs.push(monFetch('clusters', '/evolution/clusters/' + enc + '?semantic=false'));
      jobs.push(monFetch('preflight', '/evolution/preflight/' + enc));
    }
    return Promise.all(jobs);
  }

  function monStale(on) {
    $$('#pg-monitor .card').forEach(function (c) { c.classList.toggle('mon-stale', !!on); });
  }

  function monFinish(promise) {
    return promise.then(function () {
      monCtl.busy = false;
      monStale(false);
      monPaint();
    }, function (e) {
      monCtl.busy = false;
      monStale(false);
      toast('监控中心读取失败：' + ((e && e.message) || e));
    });
  }

  /* 轻量 tick：只重取「会变的计数」类接口（MON_GLOBAL 那批 + 观测读数），
     不碰 skill 级诊断（health / candidates / clusters / preflight）与工具统计 ——
     那几条才是真贵（聚类要读 trace store）。它们交给整轮与人工刷新。 */
  function monLoadCore() {
    if (monCtl.busy) return Promise.resolve();
    monCtl.busy = true;
    monStale(true);
    var jobs = MON_GLOBAL.map(function (j) { return monFetch(j[0], j[1], j[2]); });
    jobs.push(monFetch('obs', '/evolution/observations/stats',
      { query: { skill_name: monCtl.skill, hours: 24 } }));
    if (monCtl.taskId && monTaskById(monCtl.taskId)) {
      jobs.push(monFetch('replay', '/evolution/tasks/' + encodeURIComponent(monCtl.taskId) + '/replay'));
    }
    return monFinish(Promise.all(jobs));
  }

  /* 整轮刷新：轻量 tick 的全部内容 + 权限位 + 工具统计 + skill 级诊断。
     进入页面、人工点「刷新」、以及每 6 次自动 tick 走这里。 */
  function monLoad() {
    if (monCtl.busy) return Promise.resolve();
    monCtl.busy = true;
    monStale(true);
    return monFinish(monFetch('perms', '/auth/permissions').then(function (p) {
      monCtl.admin = !!(p && p.is_admin) || !!me().is_admin;
      var jobs = MON_GLOBAL.map(function (j) { return monFetch(j[0], j[1], j[2]); });
      // 逐个工具调用统计是管理员接口：非管理员这里换成公开的工具目录，
      // 顺带把一个必然 403 的请求省掉（那会在律师页面上留一条红色报错）。
      jobs.push(monCtl.admin
        ? monFetch('tools', '/admin/stats/tools', { query: { days: 7 } })
        : monFetch('catalog', '/meta/tools'));
      return Promise.all(jobs);
    }).then(function () {
      // 已选中某个进化任务时，replay（A/B 对照 + 门禁实测）要跟着一起重取：
      // 否则自动刷新后验证守门域显示的仍是上一次切换任务时的那份对照数据。
      if (monCtl.taskId && monTaskById(monCtl.taskId)) {
        return monFetch('replay', '/evolution/tasks/' + encodeURIComponent(monCtl.taskId) + '/replay');
      }
    }).then(function () {
      return monLoadSkill();
    }));
  }

  /* 闭环回流条：把「L5 → L0」这句话落到真实记录上。后端每个周期收口时都会写
     cycle.reflow（results.tsv / 基线重建 / bandit 后验衰减）并挂进
     overview.last_cycle，这里如实转述 —— 没跑过完整周期就降级成一句说明，不编数。 */
  function monPaintRecycle() {
    var box = document.getElementById('monRecycle');
    if (!box) return;
    var meta = monCtl.data.meta || {};
    var last = ((monCtl.data.overview || {}).last_cycle) || null;
    var cycles = Number((meta.counts || {}).cycles) || 0;
    var rf = (last || {}).reflow || null;
    if (!rf) {
      box.textContent = '闭环回流：还没有跑过完整周期 —— 触发一轮后，results.tsv、新基线'
        + '与 bandit 后验会在这里回流（当前已记录 ' + cycles + ' 轮）。';
      box.removeAttribute('title');
      return;
    }
    var dec = last.decisions || {};
    var keep = (last.keep_ratio === null || last.keep_ratio === undefined)
      ? '上一轮没有产生决策'
      : '上一轮 keep ' + monPct(last.keep_ratio, 1)
        + '（keep ' + monN(dec.keep) + ' / revert ' + monN(dec.revert) + '）';
    box.textContent = ['闭环回流', keep, 'results.tsv 已落盘',
      '基线重建 ' + monN(rf.baseline_rebuilt) + ' 个',
      'bandit 后验衰减 ' + monN(rf.bandit_decayed) + ' 条',
      '下次窗口 ' + (rf.next_window || '—'),
      '共 ' + cycles + ' 轮 · 上轮收口 ' + fmtTime(last.started_at)].join(' · ');
    box.title = rf.statement || '';
  }

  function monPaint() {
    monPaintKpis();
    monPaintChips();
    monPaintSkillUse();
    monPaintUsage();
    monPaintTools();
    monPaintL0();
    monPaintL1();
    monPaintL2();
    monPaintL3();
    monPaintL4();
    monPaintL5();
    monPaintNorth();
    monPaintHitl();
    monPaintAlerts();
    monPaintRecycle();
    monTx('monUpdated', '更新于 ' + nowStamp());
  }

  function monTaskOdds() {
    var s = ((monCtl.data.taskStats || {}).by_status) || {};
    var total = 0, ok = 0, bad = 0;
    ['succeeded', 'failed', 'degraded', 'dead'].forEach(function (k) {
      var v = Number(s[k]) || 0;
      total += v;
      if (k === 'succeeded') ok += v; else bad += v;
    });
    return { total: total, ok: ok, bad: bad };
  }

  function monPaintKpis() {
    var d = monTaskOdds();
    // /tasks/stats 对非管理员只统计本人任务（返回 scope:"本人"），但页面其余卡片的
    // 计数（如告警里的门禁失败数）是全局口径。这里把范围写出来，免得律师把个人
    // 通过率当成全所通过率读。
    var scope = (monCtl.data.taskStats || {}).scope;
    var sc = scope ? '（仅统计' + scope + '任务）' : '';
    monTx('monKpiPass', d.total ? pct(d.ok / d.total, 1) : '—');
    monTx('monKpiPassSub', d.total
      ? '成功 ' + d.ok + ' / 已结 ' + d.total + ' 条（不含取消、待处理）' + sc
      : '暂无已结任务' + sc);
    monTx('monKpiFail', d.total ? pct(d.bad / d.total, 1) : '—');
    monTx('monKpiFailSub', (d.total ? '失败 + 降级 + 终止 ' + d.bad + ' 条' : '暂无已结任务') + sc);
    var skills = ((monCtl.data.overview || {}).skills) || [];
    var degraded = Number((monCtl.data.overview || {}).degraded_count) || 0;
    monTx('monKpiHealth', skills.length ? (skills.length - degraded) + ' / ' + skills.length : '—');
    monTx('monKpiHealthSub', skills.length ? degraded + ' 个原子 Skill 已退化' : '暂无 Skill 数据');
    var obs = monCtl.data.obs;
    monTx('monKpiLag', obs ? monDur(obs.data_lag_seconds) : '—');
    monTx('monKpiLagSub', '距最新观测' + (monCtl.skill ? '（' + monCtl.skill + '）' : '（全部技能）'));
  }

  function monPaintSkillUse() {
    if (!monGuard('monSkillUseBody', ['overview'])) return;
    var list = ((monCtl.data.overview || {}).skills || []).slice();
    if (!list.length) { monSet('monSkillUseBody', monMm('还没有任何原子 Skill 的观测。')); return; }
    var called = list.filter(function (x) { return Number(x.sample_count) > 0; });
    called.sort(function (a, b) { return Number(b.sample_count) - Number(a.sample_count); });
    var idle = list.length - called.length;
    var bars = monBars(called.slice(0, 10).map(function (x) {
      return {
        name: x.skill_name, value: Number(x.sample_count) || 0,
        tone: x.status === 'healthy' ? 'ok' : 'bad',
        label: (Number(x.sample_count) || 0) + ' 次 · 通过 ' + monPct(x.success_rate, 0),
      };
    }), { empty: '当前还没有 Skill 调用记录。' });
    var more = called.length > 10 ? monMm('另有 ' + (called.length - 10) + ' 个 Skill 有调用，见下表。') : '';
    var rows = list.slice().sort(function (a, b) {
      return (Number(b.sample_count) || 0) - (Number(a.sample_count) || 0);
    }).map(function (x) {
      return {
        tone: x.status === 'healthy' ? '' : 'bad',
        cells: [
          x.skill_name, x.status === 'healthy' ? '健康' : '退化',
          monN(x.sample_count), monPct(x.success_rate, 0),
          monMs(x.p95_latency_ms),
          monPct(x.coverage, 0), (x.signals || []).length,
        ],
      };
    });
    monSet('monSkillUseBody', bars + more
      + '<div class="mon-cap">' + called.length + ' 个 Skill 有样本' + (idle ? '，' + idle + ' 个零调用' : '')
      + '；通过率是置信度加权并剔除 unknown 的健康口径，与调用量不是同一分母。</div>'
      + monTable(['Skill', '状态', '调用', '通过率', 'P95', '覆盖', '信号'], rows));
  }

  /* 对话使用记录：读 /usage/logs（la_usage_log）。这表与任务表解耦 ——
     普通 AI 助手问答不写 la_task，但也会在这里留痕；未命中技能的行
     skill_id / intent 为空串，照实展示为「—」，不编造技能名。 */
  function monPaintUsage() {
    if (!monGuard('monUsageBody', ['usageLogs'])) return;
    var d = monCtl.data.usageLogs || {};
    var items = d.items || [];
    var total = Number(d.total) || 0;
    var hit = Number(d.skill_hit) || 0;
    monTx('monUsageSub', total + ' 条 · 命中技能 ' + hit + ' 条' + (d.scope ? '（' + d.scope + '）' : ''));
    if (!items.length) {
      monSet('monUsageBody', monMm('还没有对话使用记录 —— 发起一次专家对话或 AI 助手问答后，'
        + '这里会列出留痕（la_usage_log）。'));
      return;
    }
    var rows = items.map(function (x) {
      var t = String(x.created_at || '');
      var time = t.length >= 16 ? t.slice(5, 16) : (t || '—');
      return {
        cells: [
          time,
          x.user_id === null || x.user_id === undefined ? '—' : (d.scope === '本人' ? '我' : String(x.user_id)),
          x.expert_id || '—',
          x.skill_id || '—',
          x.intent || '—',
          monPct(x.confidence, 1),
          monMs(x.elapsed_ms),
        ],
      };
    });
    monSet('monUsageBody',
      '<div class="mon-cap">每次专家对话 / AI 助手问答落一条 la_usage_log：命中技能的写'
      + ' skill_id + intent，普通提问两个字段为空串 —— "技能使用率 = 命中技能行数 / 全部行数"'
      + ' 可直接在库里算。</div>'
      + monTable(['时间', '用户', '专家', '技能', '意图', '置信度', '耗时'], rows));
  }

  function monPaintTools() {
    if (!monGuard('monToolsBody', monCtl.admin ? ['tools', 'sys'] : ['catalog', 'sys'])) return;
    var sys = (monCtl.data.sys || {}).subsystems || {};
    var mcp = sys.mcp;
    var head;
    if (mcp && mcp.error) {
      monTx('monToolsSub', 'MCP 状态读取失败');
      head = '<p class="mon-err">MCP：' + esc(mcp.error) + '</p>';
    } else if (mcp) {
      monTx('monToolsSub', mcp.available ? 'MCP 服务端在线' : 'MCP 服务端不可用');
      head = '<div class="mon-cap">MCP 服务端（本项目对外暴露的工具面）</div>'
        + monKv('服务名', monT(mcp.server))
        + monKv('可用性', mcp.available
          ? '<b style="color:var(--ps)">可用</b>'
          : '<b style="color:var(--wn)">不可用</b>')
        + (mcp.available ? '' : monKv('原因', monT(mcp.reason)))
        // 不可用时把「后端实际用的解释器」和「它看到的 mcp 版本」一并摆出来：
        // "装了却仍不可用"绝大多数是包装到了另一个环境，这两行能直接定位，
        // 不必再去终端 pip show 对比。
        + (mcp.available || !mcp.python ? '' : monKv('后端解释器',
          '<span title="' + monT(mcp.python) + '">' + monT(monShortPath(mcp.python)) + '</span>'))
        + (mcp.mcp_version ? monKv('mcp 版本', monT(mcp.mcp_version)) : '')
        + monKv('传输', (mcp.transport || []).length ? monT((mcp.transport || []).join(' / ')) : '未启用')
        + monKv('对外工具', (mcp.tool_count || 0) + ' 个 · 元工具 '
          + ((mcp.meta_tools || []).length) + ' 个（' + monT((mcp.meta_tools || []).join('、')) + '）')
        + monKv('暴露 Skill', monT((mcp.skills || []).join('、')));
    } else {
      monTx('monToolsSub', '无 MCP 段');
      head = monMm('本次健康检查没有返回 MCP 段。');
    }

    var body;
    if (monCtl.admin && monCtl.data.tools) {
      var items = (monCtl.data.tools.tools || []).slice();
      items.sort(function (a, b) { return (Number(b.calls) || 0) - (Number(a.calls) || 0); });
      var sum = function (k) {
        return items.reduce(function (s, x) { return s + (Number(x[k]) || 0); }, 0);
      };
      body = '<div class="mon-cap">工具调用分布（管理员口径）</div>'
        + monBars(items.map(function (x) {
          var fails = Number(x.fails) || 0;
          return {
            name: x.tool, value: Number(x.calls) || 0, tone: fails ? 'warn' : 'ok',
            label: (Number(x.calls) || 0) + ' 次'
              + (fails ? ' · 失败 ' + monPct(x.fail_rate, 0) + '（' + fails + '）' : ' · 无失败'),
          };
        }), { empty: '近 7 天没有工具调用记录。' })
        + monTable(['工具', '调用', '失败', '失败率', '缓存', '重试', '均值', 'Agent'], items.map(function (x) {
          return {
            tone: Number(x.fails) ? 'warn' : '',
            cells: [x.tool, monN(x.calls), monN(x.fails), monPct(x.fail_rate, 1), monN(x.cached),
              monN(x.retries), monMs(x.avg_ms), (x.agents || []).join('、')],
          };
        }))
        + '<div class="mon-cap">共 ' + monN((monCtl.data.tools || {}).total_calls) + ' 次调用 · 缓存 '
          + sum('cached') + ' 次 · 重试 ' + sum('retries') + ' 次。'
          + '口径：最近 7 天内、最新 500 条任务的 tool_calls 记录（不进时间窗过滤的计数请以数据观测域为准）。</div>'
        + monMm('MCP 外部调用与内部调用走的是同一条 call_tool 路径，数据上无法区分'
          + '——所以上面是"按工具维度合并"的统计，不是"MCP 调用数"。');
    } else if (monCtl.data.catalog) {
      var cat = monCtl.data.catalog.categories || {};
      var grouped = monCtl.data.catalog.grouped || {};
      var names = Object.keys(cat);
      body = '<div class="mon-cap">工具目录（调用统计仅管理员可见）</div>'
        + monBars(names.map(function (k) {
          var arr = grouped[k] || [];
          return { name: cat[k], value: arr.length, label: arr.length + ' 个', tone: 'ac' };
        }), { empty: '工具注册表为空。' })
        + monKv('工具总数', monN(((monCtl.data.catalog || {}).items || []).length))
        + monMm('条目数按类别聚合；每个工具的调用量、失败率需要管理员权限。');
    } else {
      body = monMm('工具信息读取失败。');
    }
    monSet('monToolsBody', head + body);
  }

  function monCap(text) { return '<div class="mon-cap">' + monT(text) + '</div>'; }

  function monPaintChips() {
    var box = document.getElementById('monChips');
    if (!box) return;
    var list = ((monCtl.data.overview || {}).skills) || [];
    if (!list.length) { box.innerHTML = monMm('没有拿到技能列表，无法按技能过滤（见下方各卡的错误提示）。'); return; }
    var chips = [{ name: '', label: '全部技能', status: '', count: 0 }].concat(list.map(function (x) {
      return {
        name: x.skill_name, label: x.skill_name, status: x.status,
        count: Number(x.sample_count) || 0,
      };
    }));
    box.innerHTML = chips.map(function (c) {
      var on = (monCtl.skill || '') === c.name;
      var dot = c.status === 'healthy' ? '' : ' bad';
      return '<button class="mon-chip' + (on ? ' on' : '') + '" data-mon-skill="' + esc(c.name) + '">'
        + (c.name ? '<i class="dd' + dot + '"></i>' : '') + esc(c.label)
        + (c.name ? '<span class="pc">' + c.count + '</span>' : '') + '</button>';
    }).join('');
  }

  function monPaintL0() {
    if (!monGuard('monL0Body', ['obs'])) return;
    var d = monCtl.data.obs || {};
    var meta = monCtl.data.meta || {};
    var hours = Number(d.hours) || 24;
    monTx('monL0Sub', (monCtl.skill || '全部技能') + ' · 近 ' + hours + ' 小时 · ' + monN(d.total) + ' 条');
    var v = d.verdicts || {};
    var known = (Number(v.pass) || 0) + (Number(v.fail) || 0) + (Number(v.degraded) || 0);
    var lat = d.latency || {};
    var rows = MON_VD.map(function (x) {
      return { label: x.label, value: Number(v[x.key]) || 0, tone: x.tone };
    });
    var html = monStack(rows, { empty: '近 ' + hours + ' 小时没有观测样本。' })
      + monKv('窗口成功率', monPct(d.success_rate, 1) + ' <span class="pc">' + known + ' 条已知判定</span>')
      + monKv('P50 / P95 延迟', monMs(lat.p50_ms) + ' / ' + monMs(lat.p95_ms))
      + monKv('数据延迟', monDur(d.data_lag_seconds) + ' <span class="pc">不受时间窗影响</span>')
      + monCap('小时吞吐（观测条数）') + monCols(d.throughput || []);

    var f = (monCtl.data.health || {}).features;
    if (f && Object.keys(f).length) {
      html += monCap('特征窗口 ' + (f.window || '—') + '（' + (monCtl.focus || '—') + '）')
        + monKv('调用量 / 平均步数', monN(f.call_volume) + ' / ' + monN(f.mean_steps, 1))
        + monKv('失败率 EWMA', monPct(f.failure_rate_ewma, 1))
        + monKv('覆盖广度 / 陈旧度', monPct(f.coverage_breadth, 0) + ' / ' + monN(f.staleness_days, 2) + ' 天')
        + monKv('P95 延迟', monMs(f.latency_p95_ms))
        + monKv('最近收益 / 平均收益', monPct(f.last_gain, 1) + ' / ' + monPct(f.mean_gain, 1));
    }
    var counts = (meta.counts || {});
    html += monCap('窗口聚合')
      + monKv('特征快照', monN(counts.feature_snapshots) + ' 个 <span class="pc">'
        + monT(((meta.budget || {}).window) || '') + '</span>')
      + monKv('观测总量', monN(counts.observations));
    if (monCtl.admin) {
      html += '<div class="mon-btns">' + monBtn('落一次窗口聚合', 'aggregate', true,
        monCtl.focus ? '' : '先选择一个 Skill') + '</div>';
    }
    monSet('monL0Body', html);
  }

  function monProfileTable(profile, radar) {
    var names = { failure_rate: '失败率', coverage: '覆盖率', staleness: '新鲜度', last_gain: '收益增益' };
    var rows = Object.keys(names).map(function (k) {
      var p = (profile || {})[k] || {};
      var score = Number((radar || {})[k]);
      return {
        tone: p.status === 'alert' ? 'warn' : '',
        cells: [names[k], isFinite(Number(p.value)) ? monN(p.value, 4) : '—',
          p.threshold === null || p.threshold === undefined ? '—' : monN(p.threshold, 2),
          isFinite(score) ? score.toFixed(2) : '—',
          p.status === 'alert' ? '告警' : p.status === 'ok' ? '正常' : '无数据'],
      };
    });
    return monTable(['维度', '当前值', '阈值', '得分', '状态'], rows);
  }

  /* 四维雷达：得分 0–1（越外越好）。缺失维度按 0 画，但在标签上写明"无数据"
     —— 画成 0 是为了多边形闭合，不是因为它差。 */
  function monRadar(radar, profile) {
    var axes = [
      { key: 'failure_rate', label: '失败率' },
      { key: 'coverage', label: '覆盖率' },
      { key: 'staleness', label: '新鲜度' },
      { key: 'last_gain', label: '收益' },
    ];
    var cx = 84, cy = 78, R = 46;
    var ring = function (k) {
      return axes.map(function (a, i) {
        var ang = -Math.PI / 2 + i * Math.PI / 2;
        return (cx + Math.cos(ang) * R * k).toFixed(1) + ',' + (cy + Math.sin(ang) * R * k).toFixed(1);
      }).join(' ');
    };
    var spokes = axes.map(function (a, i) {
      var ang = -Math.PI / 2 + i * Math.PI / 2;
      return '<line x1="' + cx + '" y1="' + cy + '" x2="' + (cx + Math.cos(ang) * R).toFixed(1)
        + '" y2="' + (cy + Math.sin(ang) * R).toFixed(1) + '" stroke="var(--br)" stroke-width="1"'
        + ' vector-effect="non-scaling-stroke"/>';
    }).join('');
    var marks = '', pts = [], labelHtml = '';
    axes.forEach(function (a, i) {
      var ang = -Math.PI / 2 + i * Math.PI / 2;
      var p = (profile || {})[a.key] || {};
      var score = Number((radar || {})[a.key]);
      var show = isFinite(score) ? Math.max(0, Math.min(1, score)) : 0;
      var x = cx + Math.cos(ang) * R * show, y = cy + Math.sin(ang) * R * show;
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
      marks += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="4" fill="'
        + (p.status === 'alert' ? 'var(--wn)' : 'var(--ac)') + '" stroke="var(--bg)" stroke-width="2">'
        + '<title>' + monT(a.label + ' 得分 ' + (isFinite(score) ? score.toFixed(2) : '无数据')) + '</title></circle>';
      var lx = cx + Math.cos(ang) * (R + 16), ly = cy + Math.sin(ang) * (R + 18);
      var anchor = i === 0 || i === 2 ? 'middle' : (i === 1 ? 'start' : 'end');
      var val = isFinite(score) ? score.toFixed(2) : '无数据';
      labelHtml += '<text x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1) + '" text-anchor="' + anchor
        + '" font-size="11" fill="var(--tt)">' + monT(a.label) + '</text>'
        + '<text x="' + lx.toFixed(1) + '" y="' + (ly + 12).toFixed(1) + '" text-anchor="' + anchor
        + '" font-size="11.5" fill="' + (p.status === 'alert' ? 'var(--wn)' : 'var(--tp)') + '">'
        + monT(val + (p.status === 'alert' ? ' 告警' : '')) + '</text>';
    });
    var svg = '<svg class="mon-radar" viewBox="0 0 168 168" role="img">'
      + '<polygon points="' + ring(1) + '" fill="none" stroke="var(--br)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
      + '<polygon points="' + ring(0.5) + '" fill="none" stroke="var(--br)" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>'
      + spokes
      + '<polygon points="' + pts.join(' ') + '" fill="var(--ac)" fill-opacity="0.16" stroke="var(--ac)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
      + marks + labelHtml + '</svg>';
    return svg + monProfileTable(profile, radar);
  }

  function monDriftTable(drift) {
    var dims = (drift || {}).dimensions || {};
    var names = { failure_rate: '失败率', latency_ms: '延迟', coverage: '覆盖' };
    var rows = Object.keys(names).map(function (k) {
      var d = dims[k] || {};
      var st = d.statistics || {}, votes = d.votes || {};
      var val = function (t) { var x = st[t]; return x === null || x === undefined ? '—' : Number(x).toFixed(3); };
      var avail = d.available || [];
      return {
        tone: d.detected ? 'bad' : '',
        cells: [names[k], val('psi'), val('kl'), val('mmd'),
          (Number(d.vote_count) || 0) + ' / ' + (avail.length || 3),
          d.detected ? '已漂移' : (avail.length ? '正常' : '样本不足'),
          (Number((d.sample_size || {}).baseline) || 0) + ' → ' + (Number((d.sample_size || {}).current) || 0)],
      };
    });
    return monTable(['维度', 'PSI', 'KL', 'MMD', '命中票', '结论', '基线→当前'], rows)
      + monMm((drift || {}).insufficient_samples
        ? '检验样本不足，"正常"只是暂时看不出来，不代表没漂移。'
        : '阈值 PSI 0.2 · KL 0.1 · MMD 0.05；三检验中两票即判该维漂移。');
  }

  function monEarly(ew) {
    ew = ew || {};
    var risk = Number(ew.risk);
    var html = monKv('先行风险', isFinite(risk) ? risk.toFixed(2)
      + (risk >= 0.6 ? ' <b style="color:var(--wn)">偏高</b>' : '') : '—');
    var list = ew.lead_signals || [];
    if (!list.length) return html + monMm('没有出现先行退化信号。');
    return html + list.map(function (s) {
      return '<div class="mon-ev">' + monDot(s.severity === 'alert' ? 'warn' : 'mute')
        + '<span class="tx">' + monT(s.detail) + '<br><span class="pc">' + monT(s.code)
        + (s.severity === 'alert' ? ' · 告警' : ' · 提示') + '</span></span></div>';
    }).join('');
  }

  function monPaintL1() {
    if (!monGuard('monL1Body', ['health'])) return;
    var h = monCtl.data.health || {};
    var name = h.skill_name || monCtl.focus || '';
    var label = h.status === 'healthy' ? '健康' : h.status === 'degraded' ? '退化' : (h.status || '—');
    monTx('monL1Sub', (name || '—') + ' · ' + label + ' · 样本 ' + monN(h.sample_count));
    var html = '';
    if (!monCtl.skill) {
      var list = (((monCtl.data.overview || {}).skills) || []).filter(function (x) { return Number(x.sample_count) > 0; });
      if (list.length) {
        html += monCap('全部 Skill 健康矩阵（按信号数排序）') + monTable(
          ['Skill', '状态', '通过率', 'P95', '漂移', '信号数'],
          list.slice().sort(function (a, b) {
            return (b.signals || []).length - (a.signals || []).length;
          }).map(function (x) {
            return {
              tone: x.status === 'healthy' ? '' : 'bad',
              cells: [x.skill_name, x.status === 'healthy' ? '健康' : '退化', monPct(x.success_rate, 0),
                monMs(x.p95_latency_ms),
                ((x.drift || {}).detected ? '已漂移' : ((x.drift || {}).insufficient_samples ? '样本不足' : '正常')),
                (x.signals || []).length],
            };
          }));
      }
      html += monMm('下面是退化最严重的一个 Skill 的详细诊断，点上方技能条可切换。');
    }
    html += monCap('四维健康诊断（' + (name || '—') + '）') + monRadar(h.radar, h.profile)
      + monCap('三检验漂移（PSI / KL / MMD 投票）') + monDriftTable(h.drift)
      + monCap('先行退化信号') + monEarly(h.early_warning)
      + monCap('成功基线对照')
      + monTable(['指标', '当前实测', '登记基线'], [
        { cells: ['成功率', monPct(h.success_rate, 1), monPct((h.baseline || {}).success_rate, 1)] },
        { cells: ['P95 延迟', monMs(h.p95_latency_ms), monMs((h.baseline || {}).p95_latency_ms)] },
        { cells: ['覆盖率', monPct(h.coverage, 0), monPct((h.baseline || {}).coverage, 0)] },
      ]);
    if ((h.signals || []).length) {
      html += monMm('本 Skill 命中的健康信号：' + h.signals.join('、'));
    }
    monSet('monL1Body', html);
  }

  /* 帕累托散点：四个目标都是 0–1 的效用分，坐标轴因此固定 0–1，
     不同 Skill 之间可以直接比。平面只放质量×时延两维（画不下四维），
     另两维在下面的表里 —— 散点不是全貌，表才是。 */
  function monScatter(items) {
    var W = 320, H = 188, L = 34, B = 26, T = 12, R = 16;
    var px = function (v) { return (L + (W - L - R) * Math.max(0, Math.min(1, v))).toFixed(1); };
    var py = function (v) { return ((H - B) - (H - B - T) * Math.max(0, Math.min(1, v))).toFixed(1); };
    var pts = (items || []).map(function (c, i) {
      var o = c.objectives || {};
      return {
        n: i + 1, q: Number(o.quality), l: Number(o.latency), o: o,
        patch: c.patch_type, bandit: c.bandit || {}, approval: c.requires_human_approval,
      };
    }).filter(function (p) { return isFinite(p.q) && isFinite(p.l); });
    if (!pts.length) return monMm('没有可画的目标向量（candidates 未返回 objectives）。');
    var dom = function (a, b) { return b.q >= a.q && b.l >= a.l && (b.q > a.q || b.l > a.l); };
    var front = pts.filter(function (p) {
      return !pts.some(function (q) { return q !== p && dom(p, q); });
    });
    // 目标向量完全相同的候选合并成一个标记：分开画会圆点叠圆点、标签压标签，图上反而看不出有几个
    var groups = [];
    pts.forEach(function (p) {
      var cx = px(p.q), cy = py(p.l);
      var g = groups.filter(function (x) { return x.cx === cx && x.cy === cy; })[0];
      if (g) g.list.push(p); else groups.push({ cx: cx, cy: cy, list: [p] });
    });
    var labelW = function (s) {
      var w = 0;
      for (var i = 0; i < s.length; i++) w += s.charCodeAt(i) > 255 ? 11 : 6.4;
      return w;
    };
    var placed = [];
    groups.forEach(function (g) {
      placed.push({ x1: Number(g.cx) - 6, x2: Number(g.cx) + 6, y1: Number(g.cy) - 6, y2: Number(g.cy) + 6 });
    });
    var hit = function (r) {
      return placed.some(function (p) {
        return !(r.x2 < p.x1 - 2 || r.x1 > p.x2 + 2 || r.y2 < p.y1 - 2 || r.y1 > p.y2 + 2);
      });
    };
    var grid = '';
    [0, 0.5, 1].forEach(function (t) {
      grid += '<line x1="' + px(t) + '" y1="' + T + '" x2="' + px(t) + '" y2="' + (H - B)
        + '" stroke="var(--br)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        + '<text x="' + px(t) + '" y="' + (H - B + 13) + '" text-anchor="middle" font-size="10" fill="var(--tt)">' + t + '</text>'
        + '<line x1="' + L + '" y1="' + py(t) + '" x2="' + (W - R) + '" y2="' + py(t)
        + '" stroke="var(--br)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        + '<text x="' + (L - 5) + '" y="' + py(t) + '" text-anchor="end" font-size="10" fill="var(--tt)">' + t + '</text>';
    });
    var marks = groups.map(function (g) {
      var on = front.indexOf(g.list[0]) >= 0;
      var cx = Number(g.cx), cy = Number(g.cy);
      var text = 'C' + g.list.map(function (p) { return p.n; }).join('·') + (on ? ' 前沿' : '');
      var tip = g.list.map(function (p) {
        return '候选 ' + p.n + '：质量 ' + p.q.toFixed(2) + ' · 时延 ' + p.l.toFixed(2)
          + ' · 覆盖 ' + monN(p.o.coverage, 2) + ' · 成本 ' + monN(p.o.cost, 2);
      }).join('；');
      var w = labelW(text);
      // 标签依次试 上、下、再往上/下各挪一行；都被占就回原位 —— 宁可压字也不能丢标签
      var pos = null;
      [-9, 17, -21, 29].forEach(function (dy) {
        if (pos) return;
        var y = cy + dy;
        if (y < 12 || y > H - B - 4) return;
        var x = cx, anchor = 'middle';
        if (x + w / 2 > W - R) { anchor = 'end'; x = W - R; }
        else if (x - w / 2 < L) { anchor = 'start'; x = L; }
        var x1 = anchor === 'end' ? x - w : anchor === 'start' ? x : x - w / 2;
        var r = { x1: x1, x2: x1 + w, y1: y - 9, y2: y + 3 };
        if (!hit(r)) { placed.push(r); pos = { x: x, y: y, a: anchor }; }
      });
      if (!pos) pos = { x: cx, y: cy - 9, a: 'middle' };
      return '<circle cx="' + g.cx + '" cy="' + g.cy + '" r="5" fill="var(--ac)" stroke="'
        + (on ? 'var(--ps)' : 'var(--bg)') + '" stroke-width="2">'
        + '<title>' + monT(tip) + '</title></circle>'
        + '<text x="' + pos.x.toFixed(1) + '" y="' + pos.y.toFixed(1) + '" text-anchor="' + pos.a
        + '" font-size="10.5" fill="var(--tp)">' + monT(text) + '</text>';
    }).join('');
    var dup = groups.filter(function (g) { return g.list.length > 1; });
    return '<svg class="mon-scatter" viewBox="0 0 ' + W + ' ' + H + '" role="img">' + grid + marks + '</svg>'
      + '<div class="mon-xlab"><span>横轴 质量得分</span><span>纵轴 时延得分</span><span>圆环 = 前沿</span></div>'
      + (dup.length ? monMm('C' + dup.map(function (g) {
          return g.list.map(function (p) { return p.n; }).join('·');
        }).join('、C') + ' 的目标向量完全相同，坐标重合，已合并为同一个标记。') : '');
  }

  function monPaintL2() {
    if (!monGuard('monL2Body', ['cand'])) return;
    var items = ((monCtl.data.cand || {}).items) || [];
    var b = ((monCtl.data.meta || {}).budget) || {};
    var used = Number(b.tasks_used) || 0, max = Number(b.max_tasks_per_cycle) || 0;
    var w = max ? Math.min(100, Math.round(used / max * 100)) : 0;
    monTx('monL2Sub', items.length ? items.length + ' 个候选 · 预算 ' + used + '/' + max : '无候选');
    var html = monCap('本周预算（' + (b.window || '—') + '）')
      + '<div class="mon-row"><span class="nm">任务配额</span><span class="mon-bar"><i style="width:'
      + w + '%;background:' + (max && used >= max ? 'var(--wn)' : 'var(--ac)') + '"></i></span>'
      + '<span class="num">' + used + ' / ' + (max || '—') + '</span></div>'
      + monKv('Token 预留 / 上限', monN(b.tokens_reserved) + ' / ' + monN(b.max_tokens_per_cycle))
      + monKv('剩余可开任务', monN(b.tasks_remaining));
    if (!items.length) {
      html += monMm('当前技能没有生成进化候选。候选由健康信号驱动（退化、漂移、预算填充），'
        + '信号为空时不会凭空造候选。');
      monSet('monL2Body', html);
      return;
    }
    html += monCap('帕累托前沿 · 四目标效用（' + (monCtl.focus || '—') + '）') + monScatter(items)
      + monTable(['候选', '补丁类型', '触发信号', '质量', '时延', '覆盖', '成本', 'Bandit 采样', '人工批准'],
        items.map(function (c, i) {
          var o = c.objectives || {}, bb = c.bandit || {};
          return {
            cells: ['C' + (i + 1) + ' ' + c.candidate_id, c.patch_type || '—', c.signal || '—',
              monN(o.quality, 2), monN(o.latency, 2), monN(o.coverage, 2), monN(o.cost, 2),
              monN(bb.sampled, 3) + '（α' + monN(bb.alpha, 1) + ' / β' + monN(bb.beta, 1)
                + (bb.cold_start ? ' · 冷启动' : '') + '）',
              c.requires_human_approval ? '需要' : '否'],
          };
        }))
      + (items[0] && items[0].hypothesis ? monMm('假设：' + items[0].hypothesis) : '')
      + monMm('Bandit 用 Thompson 采样从候选中挑一批执行；冷启动（α=β=1）时采样噪声最大。');
    monSet('monL2Body', html);
  }

  function monPaintL3() {
    if (!monGuard('monL3Body', ['clusters'])) return;
    monTx('monL3Sub', monCtl.focus ? monCtl.focus : '—');
    var items = ((monCtl.data.clusters || {}).items) || [];
    if (!items.length) {
      monSet('monL3Body', monMm('当前技能没有可归并的失败样本 —— 失败场景切片为空。'));
      return;
    }
    var total = items.reduce(function (s, x) { return s + (Number(x.size) || 0); }, 0);
    var html = monCap('失败聚类（' + total + ' 条样本 · ' + items.length + ' 簇 · '
      + monT(items[0].similarity || '') + ' 相似度）')
      + monBars(items.map(function (x, i) {
        return {
          name: (i + 1) + '. ' + (x.error_type || 'unknown'), value: Number(x.size) || 0,
          tone: 'bad', label: (Number(x.size) || 0) + ' 条',
        };
      }), { empty: '没有失败簇。' })
      + monTable(['簇', '错误类型', '样本', '代表步骤（Agent·工具）', '重试', '单步耗时', '成功参考步数'],
        items.map(function (x, i) {
          var k = ((x.representative || {}).key_step) || {};
          var g = ((x.representative || {}).grey_fact) || {};
          return {
            tone: 'bad',
            cells: ['#' + (i + 1), x.error_type || '—', monN(x.size),
              (k.agent || '—') + '·' + (k.tool || '—') + ' 第 ' + monN(k.step) + ' 步',
              monN(k.retries), monMs(k.elapsed_ms), monN(g.success_step_upper)],
          };
        }))
      + monMm('簇内代表样本按"第一个失败/降级步骤"定位；成功参考步数是同技能成功轨迹的步数上界。');
    html += '<div class="mon-btns">' + monBtn('根因分析（样本最多的簇）', 'rootcause', true, '') + '</div>'
      + '<div id="monRC"></div>';
    monSet('monL3Body', html);
    monPaintRC();
  }

  function monPaintRC() {
    var box = document.getElementById('monRC');
    if (!box) return;
    var d = monCtl.rc;
    if (!d) { box.innerHTML = ''; return; }
    var diag = d.diagnosis || {}, ev = d.evidence_check || {};
    var html = monCap('根因分析 ' + (d.cluster_id || '') + ' · 引擎 ' + (d.engine || 'rule')
      + (d.model_hint ? ' / ' + d.model_hint : ''))
      + monKv('诊断结论', monT(diag.finding))
      + monKv('建议补丁类型', monT(d.patch_type));
    (diag.evidence || []).forEach(function (e) {
      html += '<div class="mon-ev"><span class="tm">证据</span><span class="tx">' + monT(e) + '</span></div>';
    });
    html += monCap('假设与校验')
      + monTable(['假设', '先验', '校验', '采纳'], (d.hypotheses || []).map(function (h) {
        return {
          tone: h.admitted ? '' : 'warn',
          cells: [h.hypothesis, monN(h.prior, 3), h.verification || '—', h.admitted ? '采纳' : '未采纳'],
        };
      }))
      + monKv('证据校验', (ev.passed ? '通过' : '未通过') + '（' + monN(ev.case_count) + ' 个样本 · '
        + monMs(ev.evidence_ms) + '）')
      + (ev.statement ? monMm(ev.statement) : '')
      + ((d.agents || []).length ? monMm('执行链：' + d.agents.map(function (a) {
        return a.name + '(' + a.role + ')';
      }).join(' → ')) : '');
    box.innerHTML = html;
  }

  var MON_STATUS = {
    candidate: '待验证候选', patch_ready: '补丁就绪', patch_rejected: '补丁被拒',
    validation_passed: '门禁通过', validation_failed: '门禁未通过',
    rolling_out: '灰度中', rollout_complete: '灰度全量', rolled_back: '已回滚',
    active: '已生效 Keep', reverted: '已回退 Revert',
  };
  var MON_GATE_LABEL = {
    delta_success_rate: 'Δ成功率 ≥ ε', latency: 'P95 延迟在容忍内', coverage: '覆盖率未跌破下限',
    no_critical_failure: '零严重失败', budget: '运行次数在任务预算内', no_regression: '任一维都不回退',
  };
  var MON_DIM_LABEL = {
    success_rate: '成功率', p95_latency_ms: 'P95 延迟', coverage: '覆盖率', critical_failures: '严重失败',
  };

  /* id 是可选的业务主键（如 review_id）：monOnClick 会读 data-mon-id 回传给
     monWrite，少了它抽查结论会打到没有 id 的路径上。 */
  function monBtn(label, action, on, hint, id) {
    return '<button class="btn sm" data-mon-write="' + esc(action) + '"'
      + (id ? ' data-mon-id="' + esc(id) + '"' : '') + (on ? '' : ' disabled')
      + (hint ? ' title="' + esc(hint) + '"' : '') + '>' + esc(label) + '</button>';
  }
  function monShortPath(p) {
    var s = String(p || '');
    var parts = s.split(/[\\/]/);
    return parts.slice(-2).join('/') || s;
  }
  function monTaskById(id) {
    return (((monCtl.data.tasks || {}).items) || []).filter(function (x) { return x.task_id === id; })[0];
  }

  function monGateTable(gate) {
    var checks = (gate || {}).checks || {};
    var th = (gate || {}).thresholds || {};
    var me = (gate || {}).measured || {};
    var keys = ['delta_success_rate', 'latency', 'coverage', 'no_critical_failure', 'budget', 'no_regression'];
    var rows = keys.map(function (k) {
      var ok = checks[k];
      return {
        tone: ok === false ? 'bad' : '',
        cells: [MON_GATE_LABEL[k] || k, ok === undefined ? '未执行' : (ok ? '通过' : '未通过')],
      };
    });
    return monTable(['门禁', '结论'], rows)
      + monKv('阈值', '成功率 ≥ ' + monN(th.min_success_rate, 3) + ' · P95 ≤ ' + monMs(th.max_p95_latency_ms)
        + ' · 覆盖 ≥ ' + monN(th.min_coverage, 3) + ' · 运行 ≤ ' + monN(th.max_runs))
      + monKv('实测', '成功率 ' + monPct(me.success_rate, 1) + ' · P95 ' + monMs(me.p95_latency_ms)
        + ' · 覆盖 ' + monPct(me.coverage, 0) + ' · 严重失败 ' + monN(me.critical_failures)
        + ' · 运行 ' + monN(me.run_count))
      + monMm('六道闸全过才允许推进灰度；任何一道不过就地停在原地，不会"带伤上线"。');
  }

  /* 同基线 A/B：两边必须来自同一个 holdout，跨集比数的差值不成立 ——
     接口在 holdout 字段里写明这一点，这里如实转述。 */
  function monAbTable(ab) {
    var dims = (ab || {}).dimensions || {};
    var keys = ['success_rate', 'p95_latency_ms', 'coverage', 'critical_failures'];
    var rows = keys.map(function (k) {
      var d = dims[k] || {};
      var verdict = d.verdict === 'better' ? '候选更好' : d.verdict === 'worse' ? '候选更差'
        : (d.verdict === 'tie' ? '持平' : '不可比');
      var isLat = k === 'p95_latency_ms';
      return {
        tone: d.verdict === 'worse' ? 'bad' : '',
        cells: [MON_DIM_LABEL[k], isLat ? monMs(d.baseline) : monN(d.baseline, 3),
          isLat ? monMs(d.candidate) : monN(d.candidate, 3),
          monEmpty(d.delta) ? '—' : (Number(d.delta) > 0 ? '+' : '') + (isLat ? monMs(d.delta) : monN(d.delta, 3)),
          verdict],
      };
    });
    var win = (ab || {}).winner;
    return monTable(['维度', '基线', '候选', '差值', '结论'], rows)
      + monKv('回放口径', monT((ab || {}).holdout || '—'))
      + monKv('判定', win === 'candidate' ? '候选占优' : win === 'baseline' ? '基线占优' : '持平')
      + monMm('差值 = 候选 − 基线；"候选更好"已按每个维度的方向（成功率越高越好、延迟越低越好）判定。');
  }

  function monFunnel(rollout) {
    var stages = [['shadow', '影子 0%'], ['canary', '小流量 5%'], ['full', '全量 100%']];
    var cur = String((rollout || {}).stage || 'pending');
    var idx = -1;
    stages.forEach(function (s, i) { if (s[0] === cur) idx = i; });
    if (cur === 'full') idx = 2;
    var boxes = stages.map(function (s, i) {
      var on = idx >= i;
      return '<i class="' + (on ? 'on' : '') + '">' + monT(s[1]) + '<br><span class="pc">'
        + (on ? (i === idx ? '当前' : '已过') : '未到') + '</span></i>';
    }).join('');
    var hist = ((rollout || {}).history) || [];
    var rows = hist.slice().reverse().map(function (h) {
      var act = h.action === 'advance' ? '推进' : h.action === 'rollback' ? '回滚'
        : h.action === 'start' ? '启动' : h.action === 'full' ? '全量' : String(h.action || '');
      return {
        tone: h.action === 'rollback' ? 'bad' : '',
        cells: [fmtTime(h.at), act, (h.stage || '') + ' ' + (h.percent === undefined ? '' : h.percent + '%'),
          h.reason || (h.preflight === undefined || h.preflight === null ? '' : '预演通过：' + (h.preflight ? '是' : '否'))],
      };
    });
    return '<div class="mon-fn">' + boxes + '</div>'
      + monTable(['时间', '动作', '档位', '说明'], rows, { empty: '还没有灰度记录。' })
      + monMm(cur === 'pending' ? '候选尚未进入灰度。灰度分影子 → 小流量 → 全量三档，每档都要重新过门禁。'
        : '灰度期间每一档都重新过门禁，任一档不过就地回滚到影子并记为 rolled_back。');
  }

  function monPaintL4() {
    if (!monGuard('monL4Body', ['tasks'])) return;
    var items = ((monCtl.data.tasks || {}).items) || [];
    if (!items.length) {
      monTx('monL4Sub', '暂无进化任务');
      monSet('monL4Body', monMm('本周期还没有进化任务。任务由「触发一轮周期」批量创建，'
        + '或从退化 Skill 的候选单独建任务；没有任务时回放、门禁、灰度、账本都无从谈起。')
        + (monCtl.admin ? '<div class="mon-btns">' + monBtn('触发一轮周期（最多 3 个 Skill）', 'cycle', true, '') + '</div>' : ''));
      return;
    }
    if (!monCtl.taskId || !monTaskById(monCtl.taskId)) monCtl.taskId = items[0].task_id;
    var t = monTaskById(monCtl.taskId) || items[0];
    var st = String(t.status || '');
    monTx('monL4Sub', items.length + ' 个任务 · ' + (MON_STATUS[st] || st));
    var html = '<div class="mon-chips">' + items.map(function (x) {
      return '<button class="mon-chip' + (x.task_id === t.task_id ? ' on' : '') + '" data-mon-task="'
        + esc(x.task_id) + '">' + esc(x.skill_name) + ' · ' + esc(MON_STATUS[x.status] || x.status) + '</button>';
    }).join('') + '</div>'
      + monKv('任务 ' + t.task_id, monT(t.base_version) + ' → ' + monT(((t.result || {}).candidate_version) || '候选'))
      + monKv('状态 / 决策', monT(MON_STATUS[st] || st) + ' / ' + monT(t.decision || '未决'))
      + monKv('假设', monT(t.hypothesis))
      + monKv('创建 / 更新', fmtTime(t.created_at) + ' / ' + fmtTime(t.updated_at));

    if ((t.guard_violations || []).length) {
      html += '<p class="mon-err">补丁命中禁改名单，已拒绝落盘：'
        + esc((t.guard_violations || []).join('、')) + '</p>';
    }
    if (t.patch_artifact) {
      html += monKv('补丁工件', '<span title="' + monT(t.patch_artifact) + '">'
        + monT(monShortPath(t.patch_artifact)) + '</span> <span class="pc">服务端落盘</span>');
    }

    var replay = monCtl.data.replay;
    var ab = (replay && replay.ab) || (t.result || {}).ab;
    var gate = (replay && replay.gate) || (t.result || {}).gate;
    if (ab) {
      html += monCap('对比实验 · 同基线回放 A/B');
      html += monAbTable(ab);
    } else {
      html += monCap('对比实验 · 同基线回放 A/B')
        + monMm(st === 'candidate' || st === 'patch_ready'
          ? '候选还没跑回放验证：跑完才有基线与候选的同集对照。'
          : '这个任务没有回放对照数据。');
    }
    html += monCap('指标门禁（六闸）') + (gate ? monGateTable(gate) : monMm('门禁未执行。'));

    var pre = t.preflight || monCtl.data.preflight;
    if (pre) {
      html += monCap('三道防线 · 事前漂移预演')
        + monKv('结论', pre.passed ? '<b style="color:var(--ps)">通过</b>' : '<b style="color:var(--dg)">阻断</b>')
        + monKv('检查样本', monN(pre.samples) + ' 条 · ' + fmtTime(pre.checked_at))
        + (pre.notes || []).map(function (n) {
          return '<div class="mon-ev">' + monDot('mute') + '<span class="tx">' + monT(n) + '</span></div>';
        }).join('')
        + (pre.blocking || []).map(function (n) {
          return '<div class="mon-ev">' + monDot('bad') + '<span class="tx">' + monT(n) + '</span></div>';
        }).join('');
    }
    html += monCap('灰度漏斗（影子 → 小流量 → 全量）') + monFunnel(t.rollout);
    monSet('monL4Body', html);
  }

  function monAuditText(e) {
    var d = e.detail || {};
    var k = String(e.kind || '');
    var skill = e.skill_name || '全部';
    if (k === 'freeze') return '熔断：' + (d.reason || '') ;
    if (k === 'unfreeze') return '解除熔断';
    if (k.indexOf('ledger:') === 0) {
      return (k.slice(7) === 'keep' ? 'Keep 生效 ' : 'Revert 留痕 ') + skill + '：'
        + (d.from_version || '—') + ' → ' + (d.to_version || '—');
    }
    if (k.indexOf('review:') === 0) return '人工抽查' + (k.slice(7) === 'resolved' ? '已填写' : '待办') + ' · ' + skill;
    if (k.indexOf('hitl_') === 0) return '抽查结论 ' + (k.slice(5) === 'approve' ? '通过' : '驳回') + ' · ' + skill;
    return '决策 ' + skill + '：' + (d.decision || k);
  }

  function monPaintL5() {
    if (!monGuard('monL5Body', ['meta'])) return;
    var m = monCtl.data.meta || {};
    var sch = m.scheduler || {}, fr = m.frozen || {}, safe = m.safety || {}, c = m.counts || {};
    monTx('monL5Sub', fr.frozen ? '已熔断' : (sch.enabled ? '周级调度开启' : '调度关闭'));
    var html = monCap('元控制')
      + monKv('周级调度', (sch.enabled ? '开启' : '关闭') + ' · 每 ' + monN(sch.interval_days) + ' 天'
        + (sch.last_run_at ? ' · 上次 ' + fmtTime(sch.last_run_at) : ' · 尚未跑过')
        + (sch.next_run_at ? ' · 下次 ' + fmtTime(sch.next_run_at) : ''))
      + monKv('熔断', fr.frozen
        ? '<b style="color:var(--dg)">已熔断</b> · ' + monT(fr.reason) + '（' + fmtTime(fr.at) + '）'
        : '正常（未熔断）')
      + monKv('累计', monN(c.observations) + ' 条观测 · ' + monN(c.tasks) + ' 个任务 · '
        + monN(c.cycles) + ' 轮周期 · ' + monN(c.feature_snapshots) + ' 个特征快照')
      + monCap('安全红线')
      + monKv('权限位', safe.permissions_changed ? '已变更' : '未变更')
      + monKv('人在环', safe.human_in_the_loop ? '开启（GATE 由人按）' : '关闭')
      + monKv('熔断入口', monT(safe.kill_switch))
      + monMm('禁改名单（补丁命中即拒绝落盘）：' + (safe.forbidden_tokens || []).join('、'));

    // 调度开关与周期只有管理员能改（服务端另有 RequireAdmin 兜底）。
    if (monCtl.admin) {
      html += '<div class="mon-btns">'
        + monBtn(sch.enabled ? '关闭周期调度' : '开启周期调度', 'sched-toggle', true, '')
        + monBtn('调整进化周期', 'sched-interval', true, '')
        + '</div>'
        + monMm('调度是进程内的：到点自动跑一轮周期。改周期或开启后，到期时间按新周期'
          + '从现在起重排，不会立刻跑一轮。进程级急停用环境变量 EVOLUTION_SCHEDULER_ENABLED。');
    }

    var art = (monCtl.data.overview || {}).artifacts || {};
    var keys = Object.keys(art);
    if (keys.length) {
      html += monCap('落盘位置')
        + keys.map(function (k) {
          return monKv(k, '<span title="' + monT(art[k]) + '">' + monT(monShortPath(art[k])) + '</span>');
        }).join('');
    }

    var entries = ((monCtl.data.audit || {}).entries) || [];
    html += monCap('审计时间线（' + entries.length + ' 条）');
    if (!entries.length) {
      html += monMm('还没有决策、账本或抽查记录 —— 飞轮跑起来之后这里会合成一条时间线。');
    } else {
      html += entries.slice(0, 12).map(function (e) {
        return '<div class="mon-ev"><span class="tm">' + monT(fmtTime(e.at)) + '</span><span class="tx">'
          + monT(monAuditText(e)) + '</span></div>';
      }).join('');
    }
    monSet('monL5Body', html);
  }

  /* 北极星来自 overview 内嵌的 north_star（不再单独请求 /evolution/north-star）。 */
  function monNorth() { return ((monCtl.data.overview || {}).north_star) || {}; }

  function monPaintNorth() {
    if (!monGuard('monNorthBody', ['overview'])) return;
    var n = monNorth();
    var hitl = n.hitl || {};
    var delta = n.keep_ratio_delta;
    monSet('monNorthBody',
      monKv('周进化收益', monPct(n.weekly_gain, 1))
      + monKv('漂移召回', monPct(n.drift_recall, 1))
      + monKv('先行信号覆盖', monPct(n.lead_signal_coverage, 1))
      + monKv('门禁通过率', monPct(n.gate_pass_rate, 1))
      + monKv('Keep 比例', monPct(n.keep_ratio, 1) + (delta === null || delta === undefined ? ''
        : ' <span class="pc">环比 ' + (Number(delta) >= 0 ? '+' : '') + pct(delta, 1) + '</span>'))
      + monKv('Revert 比例', monPct(n.revert_ratio, 1))
      + monKv('已决任务', monN(n.decided_tasks))
      + monKv('人工抽查', monN(hitl.pending) + ' 待办 · ' + monN(hitl.resolved) + ' 已办 · 通过率 '
        + monPct(hitl.approve_ratio, 0))
      + monKv('周期数', monN(n.cycle_count))
      + monMm('收益、Keep/Revert 比例的分母是"已决策的任务数"，不是时间 —— '
        + '没有决策时显示 — 才是对的，填 0 会假装"一个都没成"。'));
  }

  function monPaintHitl() {
    if (!monGuard('monHITLBody', ['reviews'])) return;
    var items = ((monCtl.data.reviews || {}).items) || [];
    var pending = items.filter(function (x) { return x.status === 'pending'; });
    var html = monCap('待抽检队列（' + pending.length + '）');
    if (!pending.length) {
      html += monMm('没有待办抽查。灰度走到全量时会按比例抽样送进这里，结论由人填写。');
    } else {
      html += pending.map(function (r) {
        return '<div class="mon-ev">' + monDot('warn') + '<span class="tx">'
          + monT(r.skill_name) + ' · ' + monT(r.patch_type || '')
          + '<br><span class="pc">' + monT(r.reason || '') + '</span>'
          + (monCtl.admin ? '<div class="mon-btns">' + monBtn('通过', 'approve', true, '', r.review_id) + monBtn('驳回', 'reject', true, '', r.review_id) + '</div>' : '')
          + '</span></div>';
      }).join('');
    }
    var fr = ((monCtl.data.meta || {}).frozen) || {};
    html += monCap('安全红线 · 熔断开关')
      + monKv('熔断状态', fr.frozen ? '<b style="color:var(--dg)">已熔断</b>' : '正常');
    if (monCtl.admin) {
      html += '<div class="mon-btns">'
        + (fr.frozen ? monBtn('解除熔断', 'unfreeze', true, '') : monBtn('一键熔断', 'freeze', true, ''))
        + monBtn('触发一轮周期', 'cycle', true, '')
        + monBtn('刷新飞轮状态', 'refresh', true, '') + '</div>'
        + monMm('熔断立刻停掉一切自动进化动作；周期会消耗本周任务配额。'
          + '这些按钮只有管理员渲染，服务端另有校验。');
    } else {
      html += monMm('熔断、抽查与周期触发是管理员动作，当前账号只读。');
    }
    monSet('monHITLBody', html);
  }

  /* 告警是**前端按规则推导**的：后端没有"告警表"，这一层只是把已有的状态位
     翻译成"该看哪儿"。规则写在卡片底部，不让人猜数字从哪来。 */
  function monAlerts() {
    var out = [], seen = {};
    var meta = monCtl.data.meta || {}, fr = meta.frozen || {}, b = meta.budget || {};
    var ov = monCtl.data.overview || {}, counts = ov.task_counts || {};
    var tasks = ((monCtl.data.tasks || {}).items) || [];
    var hitl = (monNorth().hitl) || {};
    var obs = monCtl.data.obs || {}, h = monCtl.data.health || {};
    var add = function (lv, text, anchor) {
      if (seen[text]) return;
      seen[text] = 1;
      out.push({ lv: lv, text: text, anchor: anchor });
    };
    if (fr.frozen) add('P0', '进化飞轮已熔断：' + (fr.reason || '无原因记录'), 'monCardL5');
    tasks.forEach(function (t) {
      var st = String(t.status || '');
      if (st === 'rolled_back') add('P0', '灰度回滚：' + t.skill_name, 'monCardL4');
      if (st === 'reverted') add('P0', '版本已 Revert：' + t.skill_name, 'monCardL4');
      if ((t.guard_violations || []).length) add('P0', '补丁命中禁改名单被拒：' + t.skill_name, 'monCardL4');
      if ((((t.result || {}).gate) || {}).passed === false) {
        add('P0', '门禁未通过：' + t.skill_name + '（' + t.task_id + '）', 'monCardL4');
      }
    });
    var drift = h.drift || {};
    if (drift.detected) {
      add('P1', '分布漂移：' + h.skill_name + ' 在 ' + (drift.drifted_dimensions || []).join('/') + ' 维成立', 'monCardL1');
    }
    if (Number((h.early_warning || {}).risk) >= 0.6) {
      add('P1', '先行风险 ' + Number(h.early_warning.risk).toFixed(2) + '：' + h.skill_name, 'monCardL1');
    }
    if (Number(b.max_tasks_per_cycle) && Number(b.tasks_used) >= Number(b.max_tasks_per_cycle)) {
      add('P1', '本周进化预算已用尽（' + b.tasks_used + '/' + b.max_tasks_per_cycle + '），元控制将拒绝新建任务', 'monCardL2');
    }
    if (Number(ov.degraded_count) > 0) {
      add('P1', '共 ' + ov.degraded_count + ' 个原子 Skill 处于退化状态', 'monCardSkillUse');
    }
    if (Number(obs.data_lag_seconds) > 7200) {
      add('P2', '观测数据延迟 ' + monDur(obs.data_lag_seconds) + '——超过 2 小时没有新样本', 'monCardL0');
    }
    if (Number(hitl.pending) >= 5) add('P2', '人工抽查积压 ' + hitl.pending + ' 条待办', 'monCardHITL');
    if (Number(counts.validation_failed) > 0) {
      add('P2', '门禁未通过的任务 ' + counts.validation_failed + ' 个', 'monCardL4');
    }
    return out;
  }

  function monPaintAlerts() {
    var list = monAlerts();
    var by = function (lv) { return list.filter(function (x) { return x.lv === lv; }); };
    monTx('monAlertsSub', list.length
      ? 'P0 ' + by('P0').length + ' · P1 ' + by('P1').length + ' · P2 ' + by('P2').length
      : '无告警');
    var box = document.getElementById('monAlertsBody');
    if (!box) return;
    if (!list.length) { box.innerHTML = monMm('当前没有触发任何告警规则。'); return; }
    var html = '';
    [['P0', 'p0'], ['P1', 'p1'], ['P2', 'p2']].forEach(function (pair) {
      var arr = by(pair[0]);
      if (!arr.length) return;
      html += monCap(pair[0] + ' · ' + arr.length + ' 条')
        + arr.map(function (x) {
          return '<div class="mon-al ' + pair[1] + '"><span class="lv">' + x.lv + '</span><span>'
            + monT(x.text) + '</span><span class="ax" data-mon-jump="' + esc(x.anchor) + '">定位</span></div>';
        }).join('');
    });
    html += monMm('P0 = 已熔断 / 灰度回滚 / 版本 Revert / 补丁被拒 / 门禁未通过；'
      + 'P1 = 漂移成立 / 先行风险 ≥ 0.6 / 预算耗尽 / 存在退化 Skill；'
      + 'P2 = 数据延迟超 2 小时 / 抽查积压 ≥ 5 / 存在门禁失败任务。');
    box.innerHTML = html;
  }

  function monConfirm(title, body, path, opt, okText) {
    var done = false;
    var confirm = btn(okText || '确认', 'pr', function () {
      if (done) return;
      done = true;
      confirm.disabled = true;
      J(path, opt).then(function () {
        closeModal();
        toast('已执行：' + title);
        return monLoad();
      }).catch(function (e) {
        done = false;
        confirm.disabled = false;
        toast('操作失败：' + e.message);
      });
    });
    openModal({ title: title, body: body, footer: [btn('取消', '', closeModal), confirm] });
  }

  function monFreezeDialog() {
    var wrap = el('div');
    var field = el('div', 'lp-field');
    field.innerHTML = '<label>熔断原因（写进审计时间线，供事后追查）</label>';
    var input = document.createElement('input');
    input.value = '人工熔断：监控中心';
    field.appendChild(input);
    wrap.appendChild(field);
    wrap.appendChild(el('p', 'muted', '熔断后所有自动进化动作立即停止，解除后恢复。'));
    var done = false;
    var confirm = btn('确认熔断', 'pr', function () {
      if (done) return;
      done = true;
      confirm.disabled = true;
      J('/evolution/freeze', { method: 'POST', json: { reason: input.value.trim() || '人工熔断' } })
        .then(function () { closeModal(); toast('已熔断'); return monLoad(); })
        .catch(function (e) { done = false; confirm.disabled = false; toast('熔断失败：' + e.message); });
    });
    openModal({ title: '一键熔断', body: wrap, footer: [btn('取消', '', closeModal), confirm] });
  }

  function monIntervalDialog() {
    var cur = ((monCtl.data.meta || {}).scheduler) || {};
    var wrap = el('div');
    var field = el('div', 'lp-field');
    field.innerHTML = '<label>进化周期（天，支持小数：0.5 = 12 小时）</label>';
    var input = document.createElement('input');
    input.type = 'number';
    input.step = '0.5';
    input.min = '0.01';
    input.value = String(cur.interval_days === undefined || cur.interval_days === null ? 7 : cur.interval_days);
    field.appendChild(input);
    wrap.appendChild(field);
    wrap.appendChild(el('p', 'muted',
      '保存后到期时间按新周期从现在起重排，不会立刻跑一轮。周期越短，进化动作越频繁。'));
    var done = false;
    var confirm = btn('保存', 'pr', function () {
      var v = Number(input.value);
      if (!isFinite(v) || v <= 0) { toast('周期必须大于 0 天'); return; }
      if (done) return;
      done = true;
      confirm.disabled = true;
      J('/evolution/scheduler', { method: 'POST', json: { interval_days: v } })
        .then(function () { closeModal(); toast('进化周期已更新'); return monLoad(); })
        .catch(function (e) { done = false; confirm.disabled = false; toast('更新失败：' + e.message); });
    });
    openModal({ title: '调整进化周期', body: wrap, footer: [btn('取消', '', closeModal), confirm] });
  }

  function monReview(verdict, reviewId) {
    var wrap = el('div');
    var field = el('div', 'lp-field');
    field.innerHTML = '<label>抽查备注（可选）</label>';
    var input = document.createElement('input');
    field.appendChild(input);
    wrap.appendChild(field);
    // 驳回**不会**自动回滚：结论只是"人工认为不对"，是否回滚由 Revert 决策单独做。
    wrap.appendChild(el('p', 'muted', verdict === 'reject'
      ? '驳回只记录人工判断，不会自动回滚；是否回滚请在验证守门域对任务做 Revert 决策。'
      : '通过表示抽查样本与离线结论一致。'));
    var done = false;
    var confirm = btn(verdict === 'reject' ? '确认驳回' : '确认通过', 'pr', function () {
      if (done) return;
      done = true;
      confirm.disabled = true;
      J('/evolution/reviews/' + encodeURIComponent(reviewId), {
        method: 'POST', json: { verdict: verdict === 'reject' ? 'reject' : 'approve', note: input.value.trim() },
      }).then(function () { closeModal(); toast('抽查结论已记录'); return monLoad(); })
        .catch(function (e) { done = false; confirm.disabled = false; toast('提交失败：' + e.message); });
    });
    openModal({ title: '人工抽查结论', body: wrap, footer: [btn('取消', '', closeModal), confirm] });
  }

  function monRootCause() {
    var skill = monCtl.focus;
    if (!skill) { toast('先选择一个 Skill'); return Promise.resolve(); }
    var node = document.querySelector('[data-mon-write="rootcause"]');
    if (node) node.disabled = true;
    return J('/evolution/root-cause/' + encodeURIComponent(skill))
      .then(function (d) {
        monCtl.rc = d;
        monPaintRC();
        toast('根因分析完成');
      }, function (e) { toast('根因分析失败：' + e.message); })
      .then(function () { if (node && node.isConnected) node.disabled = false; });
  }

  function monWrite(action, id) {
    if (!monCtl.admin) { toast('只有管理员可以执行该操作'); return; }
    if (action === 'freeze') { monFreezeDialog(); return; }
    if (action === 'unfreeze') {
      monConfirm('解除熔断', '解除后自动进化恢复运行。', '/evolution/unfreeze',
        { method: 'POST' }, '确认解除');
      return;
    }
    if (action === 'sched-toggle') {
      var sch = ((monCtl.data.meta || {}).scheduler) || {};
      var next = !sch.enabled;
      monConfirm(next ? '开启周期调度' : '关闭周期调度',
        next ? '开启后到点自动跑一轮周期；到期时间按当前周期从现在起重排，不会立刻跑。'
          : '关闭后飞轮只在人工点「触发一轮周期」时前进。',
        '/evolution/scheduler', { method: 'POST', json: { enabled: next } },
        next ? '确认开启' : '确认关闭');
      return;
    }
    if (action === 'sched-interval') { monIntervalDialog(); return; }
    if (action === 'refresh') {
      monConfirm('刷新飞轮状态', '重算全部 Skill 的特征与基线，并把本轮状态追加到 results.tsv。',
        '/evolution/refresh', { method: 'POST' }, '开始刷新');
      return;
    }
    if (action === 'cycle') {
      monConfirm('触发一轮周期', '按退化程度挑最多 3 个原子 Skill 走一遍观测到执行的全链路并建候选任务；'
        + '会消耗本周任务配额（可被预算闸拒绝）。', '/evolution/cycle',
        { method: 'POST', query: { max_skills: 3 } }, '开始跑一轮');
      return;
    }
    if (action === 'aggregate') {
      if (!monCtl.focus) { toast('先选择一个 Skill'); return; }
      monConfirm('落一次窗口聚合', '把 ' + monCtl.focus + ' 的当前特征窗口写进 feature store 快照。',
        '/evolution/window/' + encodeURIComponent(monCtl.focus) + '/aggregate',
        { method: 'POST', json: {} }, '开始聚合');
      return;
    }
    if (action === 'approve') { monReview('approve', id); return; }
    if (action === 'reject') { monReview('reject', id); return; }
    if (action === 'rootcause') { monRootCause(); return; }
  }

  function monSelectTask(id) {
    if (!id || id === monCtl.taskId) return Promise.resolve();
    monCtl.taskId = id;
    monCtl.data.replay = null;
    var box = document.getElementById('monL4Body');
    if (box) box.classList.add('mon-stale');
    return monFetch('replay', '/evolution/tasks/' + encodeURIComponent(id) + '/replay').then(function () {
      if (box) box.classList.remove('mon-stale');
      monPaintL4();
    });
  }

  function monOnClick(e) {
    var chip = e.target.closest('[data-mon-task]');
    if (chip) { monSelectTask(chip.getAttribute('data-mon-task')); return; }
    var jump = e.target.closest('[data-mon-jump]');
    if (jump) {
      var el2 = document.getElementById(jump.getAttribute('data-mon-jump'));
      if (el2) el2.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    var w = e.target.closest('[data-mon-write]');
    if (w && !w.disabled) monWrite(w.getAttribute('data-mon-write'), w.getAttribute('data-mon-id') || '');
  }

  function monReloadSkill() {
    if (monCtl.busy) return Promise.resolve();
    monCtl.busy = true;
    monStale(true);
    return monLoadSkill().then(function () {
      monCtl.busy = false;
      monStale(false);
      monPaintKpis();
      monPaintL0();
      monPaintL1();
      monPaintL2();
      monPaintL3();
      monPaintAlerts();
    });
  }

  function monStopTimer() {
    if (monCtl.timer) { window.clearInterval(monCtl.timer); monCtl.timer = 0; }
  }
  /* 自动刷新分两档节奏：轻量 tick 只重取会变的计数类接口，每 6 次 tick 走一轮
     整刷（含 skill 级诊断）。这是「看着像实时」与「把后端压垮」之间的取舍：
     overview 有后端短缓存兜底，其余重接口不该被秒级轮询拖着跑。 */
  var MON_TICKS_PER_FULL = 6;
  function monTick() {
    var page = document.getElementById('pg-monitor');
    if (!page || !page.classList.contains('on') || document.hidden || monCtl.busy) return;
    monCtl.ticks += 1;
    if (monCtl.ticks % MON_TICKS_PER_FULL === 0) monLoad(); else monLoadCore();
  }
  function monStartTimer() {
    monStopTimer();
    if (!monCtl.interval) return;
    monCtl.ticks = 0;
    monCtl.timer = window.setInterval(monTick, monCtl.interval * 1000);
  }

  function monBind() {
    var page = document.getElementById('pg-monitor');
    if (!page || page.getAttribute('data-mon-bound')) return;
    page.setAttribute('data-mon-bound', '1');
    var r = document.getElementById('monRefresh');
    if (r) r.addEventListener('click', function () { monLoad().then(function () { toast('监控中心已刷新'); }); });
    var a = document.getElementById('monAuto');
    if (a) a.addEventListener('change', function () {
      monCtl.interval = Number(a.value) || 0;
      if (monCtl.interval) monStartTimer(); else monStopTimer();
    });
    var chips = document.getElementById('monChips');
    if (chips) chips.addEventListener('click', function (e) {
      var c = e.target.closest('[data-mon-skill]');
      if (!c) return;
      var v = c.getAttribute('data-mon-skill');
      if (v === monCtl.skill) return;
      monCtl.skill = v;
      monCtl.rc = null;
      monPaintChips();
      monReloadSkill();
    });
    page.addEventListener('click', monOnClick);
    // 轮询只在「停在本页」时才该烧接口：切走就停表，切回来（间隔还开着）再起。
    document.addEventListener('click', function (e) {
      var nav = e.target.closest('.side .nav, [data-go]');
      if (!nav) return;
      var id = nav.getAttribute('data-pg') || nav.getAttribute('data-go');
      if (id !== 'pg-monitor') { if (monCtl.timer) monStopTimer(); }
      else if (monCtl.interval && !monCtl.timer) monStartTimer();
    });
  }

  function renderMonitor() {
    var page = document.getElementById('pg-monitor');
    if (!page) return Promise.resolve();
    page.classList.add('loaded');
    monBind();
    // 自动刷新定时器不能只靠导航点击的委托启动（刷新页面/直达页面/路由切换
    // 时都不会触发点击，造成选了 10 秒却从不自动刷新）。渲染本页即兜底启动。
    if (monCtl.interval && !monCtl.timer) monStartTimer();
    return monLoad();
  }
  /* ------------------------------------------------------------ 页面注册与启动 */
  var RENDER = {
    'pg-dash': renderDash,
    'pg-ai': bindChat,
    'pg-expert': renderExperts,
    'pg-contract': renderContract,
    'pg-evchain': renderEvchain,
    'pg-case': renderCases,
    'pg-doc': renderDoc,
    'pg-search': renderSearch,
    'pg-cal': renderCal,
    'pg-skill': renderSkill,
    'pg-monitor': renderMonitor,
    // 编辑页。**只是注册，不在侧边栏加导航项** —— 它只能由"生成 / 打开一份
    // 草稿"进入，返回目标存在 docEdit.from 里。renderDocEdit 每次进入都重跑
    // （要按当前草稿重新拉正文），所以进入前会先清 rendered 标志。
    'pg-docedit': renderDocEdit,
  };

  function boot() {
    if (!tok()) return;      // 没登录：守卫会处理跳转，这里什么都不做
    ensureModal();

    // 导航切换 → 首次进入该页时拉数据（保留原有的 go() 行为）
    $$('.side .nav').forEach(function (n) {
      n.addEventListener('click', function () {
        var id = n.getAttribute('data-pg');
        setTimeout(function () { renderPage(id); }, 0);
      });
    });
    document.addEventListener('click', function (e) {
      var g = e.target.closest('[data-go]');
      if (g) { var id = g.getAttribute('data-go'); setTimeout(function () { renderPage(id); }, 0); }
    });

    // 全局搜索：回车 → 用该关键词跑类案检索
    var gs = $('#gs');
    if (gs) gs.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && gs.value.trim()) {
        e.preventDefault();
        var q = gs.value.trim();
        gs.value = '';
        goSearch(q);
      }
    });

    // 案件管理类型筛选：点 tab 切 case_type 重查（all=全部，不传参）
    var cf = $('#caseFilter');
    if (cf) cf.addEventListener('click', function (e) {
      var c = e.target.closest('.fchip[data-f]');
      if (!c) return;
      var v = c.getAttribute('data-f');
      if (v === casePage.type) return;
      casePage.type = v;
      casePage.n = 1;
      renderCases();
    });

    bindSearchHits();
    bindTaskClicks();
    bindActions();
    bindDraftRowActions();
    bindDocEditOnce();

    // 首屏：工作台
    renderPage('pg-dash');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();