/**
 * 前端冒烟：用 jsdom 真跑一遍 web/ 的模块图，对着**真实后端**渲染每个页面。
 *
 * 为什么不用静态检查代替：静态检查查不出 `tabs.bar` 里的 TDZ、
 * 查不出 `row.content` 在列表接口里根本不存在、也查不出字段名对不上 ——
 * 这三类都是本项目实际踩过并且**不报错**（或只在运行时某一条路径上报错）的坑。
 * 只有真的 import + 真的渲染 + 真的发请求才会暴露。
 *
 * 它验证的是"能渲染出来、不发请求异常、关键文案出现"这一层，
 * **不校验样式与视觉**（jsdom 不做布局）。视觉仍需人工看一眼。
 *
 * 跑法（先起后端）：
 *
 *     # 1) 起后端
 *     .venv/Scripts/python.exe -m uvicorn backend.app.main:app --port 8001
 *     # 2) 另开一个终端跑冒烟
 *     node scripts/web_smoke.mjs
 *
 * 环境变量（都可省）：
 *   SMOKE_ORIGIN    后端地址，默认 http://127.0.0.1:8001
 *   SMOKE_USER      登录账号，默认 admin
 *   SMOKE_PASSWORD  口令，默认取 SMOKE_USER 对应的演示账号口令
 *   SMOKE_OUT       结果输出路径，默认 scripts/web_smoke.txt
 *   SMOKE_HEADFUL   =1 时把每次访问的 view 文本也打出来（排查断言失败用）
 *
 * 依赖：jsdom。本项目**不带** node_modules（前端是零构建的），
 * 所以脚本按下面的候选顺序找 jsdom，找不到会给出安装命令。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIR, '..');
const WEB = path.join(ROOT, 'web');
const ORIGIN = process.env.SMOKE_ORIGIN || 'http://127.0.0.1:8001';
const OUT_PATH = process.env.SMOKE_OUT || path.join(SCRIPT_DIR, 'web_smoke.txt');

// jsdom 的候选位置。**顺序有意义**：优先项目自己的 node_modules
// （团队里别人 `npm i jsdom` 之后就能跑），再退到托管 Node 工作区
// （本机环境，避免在项目目录里塞 node_modules）。
const JSDOM_CANDIDATES = [
  'jsdom',
  path.join(ROOT, 'node_modules', 'jsdom', 'lib', 'api.js'),
  'C:/Users/16610/.workbuddy/binaries/node/workspace/node_modules/jsdom/lib/api.js',
];

async function loadJsdom() {
  const tried = [];
  for (const candidate of JSDOM_CANDIDATES) {
    const spec = path.isAbsolute(candidate)
      ? pathToFileURL(candidate).href
      : candidate;
    try {
      return await import(spec);
    } catch (err) {
      tried.push(`  ${candidate}  -> ${err.code || err.message}`);
    }
  }
  console.error('找不到 jsdom。已尝试：\n' + tried.join('\n'));
  console.error('\n装一个即可（任选其一）：');
  console.error('  cd ' + ROOT + ' && npm i --no-save jsdom');
  console.error('  npm i -g jsdom   # 全局装（本机托管工作区也可以）');
  process.exit(2);
}

const { JSDOM } = await loadJsdom();

const OUT = [];

// ---------------------------------------------------------------- 环境
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const dom = new JSDOM(html, { url: `${ORIGIN}/ui/`, pretendToBeVisual: true });
const { window } = dom;

// 把 jsdom 的环境装进 globalThis，让 web/js 里的 `document` / `location` 能解析。
// `navigator` 在 Node 22 上是**只读 getter**，直接赋值会抛
// "Cannot set property navigator of #<Object> which has only a getter"，
// 必须用 defineProperty 覆盖。
for (const k of ['window', 'document', 'location', 'HTMLElement', 'Node',
  'CustomEvent', 'Event', 'KeyboardEvent', 'MouseEvent', 'DOMParser', 'getComputedStyle',
  'requestAnimationFrame', 'cancelAnimationFrame', 'Blob', 'URL', 'FormData', 'File']) {
  if (window[k] !== undefined) {
    try { globalThis[k] = window[k]; } catch { /* 只读属性忽略 */ }
  }
}
for (const k of ['navigator']) {
  try {
    Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
  } catch (err) { console.warn('无法覆盖 global', k, err.message); }
}
// sessionStorage 是令牌的存放处；localStorage 是主题
globalThis.sessionStorage = window.sessionStorage;
globalThis.localStorage = window.localStorage;

// fetch：把相对路径补成绝对地址（浏览器会自动补，Node 不会）
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : input.url;
  return realFetch(url.startsWith('/') ? ORIGIN + url : url, init);
};

// EventSource 不存在；SSE 是手写的 fetch 流，没有它不影响页面渲染
globalThis.EventSource = class { constructor() { throw new Error('jsdom 无 EventSource'); } };

// 收集页面里未捕获的异常：**这是本次冒烟的主要产出**
const errors = [];
window.addEventListener('error', (e) => errors.push(`window.error: ${e.message}`));
window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${e.reason}`));
process.on('uncaughtException', (e) => errors.push(`uncaughtException: ${e.stack || e.message}`));
process.on('unhandledRejection', (e) => errors.push(`unhandledRejection: ${e}`));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 等某个条件成立（渲染是异步的，不能 sleep 一个魔法数字就断言）。 */
async function until(fn, { timeout = 60000, step = 120, label = '' } = {}) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (fn()) return true; } catch { /* 条件本身可能抛，继续等 */ }
    await sleep(step);
  }
  OUT.push(`!! 超时未满足条件：${label || fn.toString().slice(0, 80)}`);
  return false;
}

const viewText = () => document.getElementById('view')?.textContent || '';

/** 走一遍某个页面：切到 hash → 等渲染 → 断言文案。 */
async function visit({ hash, expect, label, timeout = 60000, after }) {
  const before = errors.length;
  location.hash = hash;
  // 路由 resolve 是异步的，先让 hashchange 触发
  await sleep(60);
  if (expect) {
    const ok = await until(() => expect.test(viewText()), { timeout, label: `${label} 等文案 ${expect}` });
    OUT.push(`${ok ? 'OK  ' : 'FAIL'} ${label.padEnd(22)} ${hash}`
      + `  (view ${viewText().length} 字)  ${ok ? '' : '未出现：' + expect}`);
  } else {
    await sleep(timeout);
    OUT.push(`---- ${label} ${hash} (view ${viewText().length} 字)`);
  }
  if (after) await after();
  OUT.push(`     本轮新增异常 ${errors.length - before} 条`);
  if (process.env.SMOKE_HEADFUL === '1') {
    OUT.push('     ---- view ----\n' + viewText().replace(/\n{3,}/g, '\n\n').slice(0, 4000));
  }
  return viewText();
}

async function main() {
  const mainMod = await import(pathToFileURL(path.join(WEB, 'js', 'main.js')).href);
  OUT.push(`main.js 导入完成，具名导出：${Object.keys(mainMod).join(', ') || '（无，靠副作用启动）'}`);

  await sleep(400);
  OUT.push(`启动后 #login-screen hidden=${document.getElementById('login-screen').hidden}`
    + ` / #app-shell hidden=${document.getElementById('app-shell').hidden}`);

  // 若还在登录页，就往表单里填账号口令并提交
  const user = process.env.SMOKE_USER || 'admin';
  const password = process.env.SMOKE_PASSWORD
    || (user === 'admin' ? 'admin123' : 'lawyer123');
  const loginVisible = !document.getElementById('login-screen').hidden;
  if (loginVisible) {
    document.getElementById('login-username').value = user;
    document.getElementById('login-password').value = password;
    document.getElementById('login-form').dispatchEvent(
      new window.Event('submit', { bubbles: true, cancelable: true }));
    const shown = await until(() => !document.getElementById('app-shell').hidden,
      { label: '登录后出现主界面' });
    OUT.push(`${shown ? 'OK  ' : 'FAIL'} 登录 ${user}/***`);
  } else {
    OUT.push('---- 无需登录（会话已存在）');
  }

  await until(() => (document.getElementById('nav')?.textContent || '').includes('工作台'),
    { label: '左侧导航渲染' });
  OUT.push(`导航项：${(document.querySelectorAll('#nav .nav-item').length)} 个，`
    + `文案：${(document.getElementById('nav')?.textContent || '').replace(/\s+/g, ' ').trim()}`);

  // ---------------------------------------------------------------- 各页面
  await visit({ hash: '#/', label: '工作台', expect: /任务|案件|降级|统计/ });

  await visit({ hash: '#/chat', label: '智能问答', expect: /提问|输入|意图|案件/ });

  await visit({ hash: '#/contract', label: '合同审查', expect: /合同|审查|材料/ });

  await visit({
    hash: '#/search', label: '法规检索',
    expect: /依据检索|法规|检索/,
    after: async () => {
      // 真发一次检索，验证前端能渲染命中列表（不是字段名清单）
      const input = document.querySelector('#view input[type=search]');
      const btn = Array.from(document.querySelectorAll('#view button'))
        .find((b) => b.textContent.trim() === '检索');
      if (!input || !btn) { OUT.push('!! 检索页找不到输入框或按钮'); return; }
      input.value = '未签订书面劳动合同二倍工资';
      btn.click();
      await until(() => /条命中/.test(viewText()), { label: '检索结果出现条数' });
      const hitCount = (/共?\s*(\d+)\s*条命中/.exec(viewText()) || [])[1];
      OUT.push(`    检索「未签订书面劳动合同二倍工资」→ ${hitCount ?? '?'} 条命中`);
      if (!hitCount || hitCount === '0') OUT.push('!! 检索 0 条命中（法条库应能命中）');
      // 命中块里应当出现法条名与条文号，而不是字段名
      if (!/《.+》/.test(viewText())) OUT.push('!! 检索结果里看不到法规名称');
    },
  });

  await visit({ hash: '#/cases', label: '案件管理', expect: /案件|新建|材料/ });

  // 行是 `onRowClick`（点一下改 hash），不是 `<a href>`，所以要真点一下
  const caseRow = document.querySelector('#view tbody tr.clickable');
  if (caseRow) {
    caseRow.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await sleep(120);
    await visit({ hash: location.hash, label: '案件详情', expect: /材料|进度|案件/ });
  } else {
    OUT.push('---- 案件列表没有可点的行，跳过案件详情');
  }

  await visit({ hash: '#/tasks', label: '任务中心', expect: /任务|状态|来源端|执行中|降级完成|成功/ });

  const taskRow = document.querySelector('#view tbody tr.clickable');
  if (taskRow) {
    taskRow.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await sleep(120);
    await visit({
      hash: location.hash, label: '任务详情',
      expect: /报告|引用|证据|Trace|质量/, timeout: 90000,
    });
  } else {
    OUT.push('---- 任务列表里没有可点的行，跳过任务详情');
  }

  await visit({
    hash: '#/drafts', label: '文书草稿',
    expect: /草稿|标题|版本/,
    after: async () => {
      // 点开列表第一份草稿，验证编辑器能渲染（正文要真的出现在 textarea 里）
      const row = document.querySelector('#view tbody tr[data-draft-id]');
      if (!row) { OUT.push('---- 草稿列表为空，跳过编辑区'); return; }
      row.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      await until(() => document.querySelector('#view textarea.editor'), { label: '草稿编辑区出现' });
      const ta = document.querySelector('#view textarea.editor');
      if (!ta) { OUT.push('!! 点了草稿行但没有出现编辑区'); return; }
      OUT.push(`    打开草稿 ${row.dataset.draftId}，正文字数 ${ta.value.length}`
        + `，按钮：${Array.from(document.querySelectorAll('#view button')).map((b) => b.textContent.trim()).filter(Boolean).slice(0, 10).join(' / ')}`);
      if (ta.value.length === 0) OUT.push('!! 草稿正文为空（列表没带 content，或字段名对不上）');
      if (!/保存为新版本 v\d+/.test(viewText())) OUT.push('!! 保存按钮文案不是「保存为新版本 vN」');
      const dl = Array.from(document.querySelectorAll('#view button'))
        .find((b) => b.textContent.trim() === '下载导出文件');
      if (dl) {
        // exported 的草稿按钮可用、未导出的不可用。这里只如实报状态，不预设结论 ——
        // 断死"必须 disabled"会在草稿恰好已导出时误报。
        const exported = /上次导出：/.test(viewText());
        OUT.push(`    下载按钮 disabled=${dl.disabled}（该草稿 exported=${exported}，`
          + `${dl.disabled === exported ? '不一致' : '一致'}）`);
      }
    },
  });

  await visit({
    hash: '#/capabilities', label: '能力与降级',
    expect: /降级|后端|工具/,
    after: async () => {
      const btns = Array.from(document.querySelectorAll('#view .tab'));
      OUT.push(`    tab：${btns.map((b) => b.textContent).join(' / ')}`);
      for (const b of btns.slice(1)) {
        const before = errors.length;
        b.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        await sleep(400);
        OUT.push(`    点击 tab「${b.textContent}」→ view ${viewText().length} 字，`
          + `新增异常 ${errors.length - before}`);
      }
    },
  });

  await visit({
    hash: '#/admin', label: '管理后台',
    expect: /团队|用户|配置|审计|统计/,
    after: async () => {
      const btns = Array.from(document.querySelectorAll('#view .tab'));
      OUT.push(`    tab：${btns.map((b) => b.textContent).join(' / ')}`);
      for (const b of btns) {
        const before = errors.length;
        b.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        await sleep(900);
        OUT.push(`    点击 tab「${b.textContent}」→ view ${viewText().length} 字，`
          + `新增异常 ${errors.length - before}`);
      }
    },
  });

  // ?tab=knowledge 深链（检索页的管理员链接指向这里）
  await visit({
    hash: '#/admin?tab=knowledge', label: '管理后台·法规库深链',
    expect: /法规库|知识库/,
  });

  await visit({ hash: '#/nonexistent-page', label: '不存在的页面', expect: /没有这个页面|页面不存在/ });

  // ---------------------------------------------------------------- 结果
  OUT.push('\n================ 未捕获异常 ================');
  if (!errors.length) OUT.push('（无）');
  for (const e of errors) OUT.push(String(e).split('\n').slice(0, 4).join('\n'));

  const text = OUT.join('\n');
  fs.writeFileSync(OUT_PATH, text, 'utf8');
  console.log(text);
  console.log(`\n（结果已写入 ${OUT_PATH}）`);
  window.close();
}

main().then(() => process.exit(errors.length ? 1 : 0)).catch((e) => {
  console.error('冒烟脚本自身出错：', e);
  process.exit(1);
});
