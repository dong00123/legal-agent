// 从 app.js 里**抠出真实的** ctResolve / ctStale / ctRetryHint 源码来跑，
// 而不是另写一份等价逻辑（那样测的是我重写的东西，不是发出去的那份）。
import fs from 'node:fs';

const src = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('找不到函数 ' + name);
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error('大括号不配对: ' + name);
}
const code = [grab('ctResolve'), grab('ctStale'), grab('ctRetryHint'),
              grab('ctRoleValue'), grab('ctGuessParties')].join('\n');
const make = new Function('contractState', 'CT_PARTIES',
                          code + '\nreturn {ctResolve,ctStale,ctRetryHint,ctRoleValue,ctGuessParties};');
const PARTIES = ['甲方', '乙方'];

const B = (t, n = 1) => ({ text: t, name: n + '份', chars: t.length, err: '' });
const EMPTY = () => ({ text: '', name: '', chars: 0, err: '' });

/** 造一个只够立场面板用的最小状态。 */
const mk = (party, names) => ({
  main: EMPTY(), tpl: EMPTY(), party,
  names: names || { '甲方': '', '乙方': '' },
  usedMaterial: '', usedTpl: '', usedSrc: '', usedRole: '',
});

/** 用「上传了什么」+「上次跑用了什么」还原出一次完整判断。 */
function run(main, tpl, used) {
  const st = { main, tpl, parsing: { main: false, tpl: false }, running: false,
               names: { '甲方': '', '乙方': '' }, party: '',
               usedMaterial: used?.m ?? '', usedTpl: used?.t ?? '', usedSrc: used?.s ?? '' };
  const api = make(st, PARTIES);
  const r = api.ctResolve();
  const stale = !!r.material && api.ctStale();
  return { src: r.src, sendsTpl: r.tpl, material: r.material, stale,
           btn: stale ? api.ctRetryHint(r).label : '（无按钮）' };
}

const A = B('待审正文', 'A');      // 待审合同
const T = B('范本正文', 'T');      // 范本合同

const cases = [
  ['① 只传待审 → 解析', A, EMPTY(), null, { src: 'main', sendsTpl: '', stale: false }],
  ['② 只传范本 → 解析', EMPTY(), T, null, { src: 'tpl', sendsTpl: '', stale: false }],
  ['③ 两份都传 → 解析', A, T, null, { src: 'main', sendsTpl: '范本正文', stale: false }],
  ['④ 都没传 → 解析', EMPTY(), EMPTY(), null, { src: '', material: '', stale: false }],

  ['⑤ 只传待审跑完，没再动', A, EMPTY(), { m: '待审正文', s: 'main' }, { stale: false }],
  ['⑥ 只传待审跑完 → 补传范本', A, T, { m: '待审正文', s: 'main' }, { stale: true }],
  ['⑦ 只传范本跑完 → 补传待审', A, T, { m: '范本正文', s: 'tpl' }, { stale: true }],
  ['⑧ 两份跑完 → 换掉待审', B('换过的待审', 'A2'), T, { m: '待审正文', t: '范本正文', s: 'main' }, { stale: true }],
  ['⑨ 两份跑完，没再动', A, T, { m: '待审正文', t: '范本正文', s: 'main' }, { stale: false }],
  ['⑩ 跑完 → 两个框都清空', EMPTY(), EMPTY(), { m: '待审正文', s: 'main' }, { stale: false }],
  ['⑪ 两份跑完 → 删掉范本', A, EMPTY(), { m: '待审正文', t: '范本正文', s: 'main' }, { stale: true }],
];

let bad = 0;
for (const [name, main, tpl, used, want] of cases) {
  const got = run(main, tpl, used);
  const fails = Object.entries(want).filter(([k, v]) => got[k] !== v);
  if (fails.length) bad++;
  console.log(
    (fails.length ? '❌' : '✅') + ' ' + name.padEnd(26) +
    ' src=' + String(got.src || '-').padEnd(5) +
    ' 发范本=' + String(got.sendsTpl || '无').padEnd(6) +
    ' ' + got.btn
  );
  for (const [k, v] of fails) console.log(`      ↑ ${k}: 期望 ${JSON.stringify(v)}，实得 ${JSON.stringify(got[k])}`);
}
// ── 立场取值：发给后端 client_role 的字符串 ──────────────────
console.log('\n审查立场 client_role');
const roleCases = [
  ['没选立场',        mk('', {}),                                             ''],
  ['选了中立',        mk('中立', {}),                                          ''],
  ['中立也带名字框的值', mk('中立', { '中立': '张三' }),                          ''],
  ['选了乙方，名字空', mk('乙方', {}),                                          '乙方'],
  ['选了乙方，有名字', mk('乙方', { '乙方': '李明轩' }),                          '乙方（李明轩）'],
  ['名字纯空白',      mk('乙方', { '乙方': '   ' }),                             '乙方'],
  ['名字带空白',      mk('甲方', { '甲方': ' 上海具识智能科技有限公司 ' }),        '甲方（上海具识智能科技有限公司）'],
];
for (const [name, st, want] of roleCases) {
  const got = make(st, PARTIES).ctRoleValue();
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? '✅' : '❌'} ${name.padEnd(16)} ${JSON.stringify(got)}`);
  if (!ok) console.log(`      ↑ 期望 ${JSON.stringify(want)}`);
}

// ── 从正文里抽甲乙方 ────────────────────────────────────────
console.log('\n从正文抽甲乙方');
const guessCases = [
  ['冒号紧跟',       '甲方：上海具识智能科技有限公司\n乙方：李明轩',
                     { '甲方': '上海具识智能科技有限公司', '乙方': '李明轩' }],
  ['全角空格',       '甲方　：　上海具识智能科技有限公司',
                     { '甲方': '上海具识智能科技有限公司', '乙方': '' }],
  ['谓语句不误命中', '甲方根据工作需要，安排乙方从事…乙方应当服从',
                     { '甲方': '', '乙方': '' }],
  // 代表人不是当事人，取成"张志远"就错了 —— 故意不匹配，让它跳去真正的甲方行
  ['代表人不算当事人', '甲方代表人（委托人）：张志远',
                     { '甲方': '', '乙方': '' }],
  ['签名区取公司不取人', '甲方：上海具识智能科技有限公司 甲方代表人（委托人）：张志远',
                     { '甲方': '上海具识智能科技有限公司', '乙方': '' }],
  ['名字后面还有别的', '乙方：李明轩 性别：男 身份证号：310115199503124210',
                     { '甲方': '', '乙方': '李明轩' }],
  ['取第一个匹配',   '乙方：李明轩\n乙方：第二个人',
                     { '甲方': '', '乙方': '李明轩' }],
  ['空文本',         '',
                     { '甲方': '', '乙方': '' }],
  ['只有一字不取',   '甲方：甲', { '甲方': '', '乙方': '' }],
];
for (const [name, text, want] of guessCases) {
  const got = make(mk('', {}), PARTIES).ctGuessParties(text);
  const fails = Object.entries(want).filter(([k, v]) => got[k] !== v);
  if (fails.length) bad++;
  console.log(`${fails.length ? '❌' : '✅'} ${name.padEnd(16)} 甲=${JSON.stringify(got['甲方'])} 乙=${JSON.stringify(got['乙方'])}`);
  for (const [k, v] of fails) console.log(`      ↑ ${k}: 期望 ${JSON.stringify(v)}，实得 ${JSON.stringify(got[k])}`);
}

// ── 开跑入口 ────────────────────────────────────────────────
// 用户拍板："我还没有点击甲方或者乙方，他自己就跑了，我想我点过之后再跑。"
// 所以**上传只负责解析 + 切页**，全页唯一的开跑入口是立场卡。
// 这条纯逻辑层测不到（是 DOM 事件绑定），只能对源码做断言 —— 朴素，但正是
// 上次静默回退的那种改动：给 ctParse 尾巴加回一句 startReview() 就复发了。
console.log('\n开跑入口');
const parseBody = grab('ctParse');
const entryCases = [
  ['ctParse 不再自动开跑', !parseBody.includes('startReview(')],
  ['ctParse 改成切到审查页', parseBody.includes('ctEnter(')],
  ['点立场卡会开跑', /CT_STANCES\.forEach[\s\S]*?startReview\(\)/.test(src)],
];
for (const [name, ok] of entryCases) {
  if (!ok) bad++;
  console.log(`${ok ? '✅' : '❌'} ${name}`);
}

// ── 合同正文 → 文稿块（左栏排版）与风险卡 ──────────────────
// 这一整段（countChars / ctDocBlocks / ctBlockHTML / ctRiskCards…）是**连续**的
// 源码区间，一次抠出来即可 —— 其中 ctPaintDoc / ctDocCommit 那几个碰 DOM 的只是
// 被声明、不会被调用（只调用纯函数）。
// ctGuessParties 定义在更上面（主体审查页签删掉之后，只剩立场面板用它），单独抠
// 一份拼进来 —— 脚本尾巴那条"传一个真实合同 txt"的检查要拿它抽甲乙方名称。
const docCode = src.slice(src.indexOf('function countChars'), src.indexOf('function ctParse'))
  + '\n' + grab('ctGuessParties');
if (!docCode) throw new Error('抠不到合同正文那一段源码');
const doc = new Function('esc', 'icon', 'CT_PARTIES', docCode +
  '\nreturn {countChars,ctStripNoise,ctDocBlocks,ctBlockHTML,ctCleanQuote,ctRiskCards};'
)((s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;'), () => '', PARTIES);

const fixture = 'scripts/fixtures/ct_sample_text.txt';
const risks = JSON.parse(fs.readFileSync('scripts/fixtures/ct_sample_risks.json', 'utf8'));
// 夹具是**脏的那一版**（从 la_task 的 material_text 原样导出的）：
// 每页一行「示例文件 · 无法律效力 · 仅用于产品演示 · DEMO SAMPLE」、落款处一枚
// 「样例章 · 无效」，正文里还躺着一个解析器插进来的图片占位符
// ``[IMAGE_001: 图像内容未识别（generic，识别失败）]``。
// 正因为它脏，才留得住下面几条关于水印/占位符的断言。
const text = fs.readFileSync(fixture, 'utf8');
const cleanText = doc.ctStripNoise(text);      // 解析回来先过的那一道
const blocks = doc.ctDocBlocks(text);
const kind = (k) => blocks.filter((b) => b.kind === k);
// 回收要**照 ctReadDoc 的做法**来：分页线除了 data-mark 还带着一行并进来的页脚
// 页码（data-foot），少了它还原出的文本就比原文短 —— 那正是这条断言要抓的东西。
const readBack = blocks
  .map((b) => (b.kind === 'page' && b.foot ? b.text + '\n' + b.foot : b.text)).join('\n');
const roundTrip = doc.countChars(readBack);

console.log('\n合同正文 → 文稿块（' + fixture + '）');
// 夹具里确实躺着那几行水印与那个占位符 —— 下面几条靠它才不是空转。
const wmLines = text.split('\n').filter((l) => /示例文件|样例章/.test(l)).length;
const phLines = text.split('\n').filter((l) => /\[IMAGE_\d{3}/.test(l)).length;
const blockCases = [
  // ① 最要紧的一条：**一个字都不能丢**。字数徽标的口径就是后端 count_chars，
  //    丢了字，律师改完会看见"我什么都没改，字数怎么就少了"。
  //    比的是**摘掉水印与占位符之后**的那份：那两类是故意丢的（连字数一起减，
  //    见 ctParse 里那个 dropped），除此以外一个字都不许少。
  ['往返不丢字（水印/占位符除外）',
   roundTrip === doc.countChars(cleanText) && wmLines === 4 && phLines === 1,
   `原文 ${doc.countChars(text)} − 水印与占位符 ${doc.countChars(text) - doc.countChars(cleanText)}`
   + ` = ${doc.countChars(cleanText)} / 还原 ${roundTrip}`
   + `（夹具里 ${wmLines} 行水印、${phLines} 个图片占位符）`],
  // ② 水印行不许进正文块：屏幕上、送审材料里都不该有"示例文件 · 无法律效力"。
  ['水印不进正文', blocks.every((b) => !/示例文件|样例章|无法律效力|DEMO/.test(b.text))],
  // ②' 图片占位符同理。那是解析管道插进来的，合同里没有这一行 ——
  //     摆在"合同全文"里，律师会以为合同真写了这句话（用户点名的就是它）。
  ['图片占位符不进正文', blocks.every((b) => !/\[IMAGE_\d{3}/.test(b.text))
   // 摘掉是**整行消失**，不是留个空行（留空行的话，切块会多出一处段落边界）。
   && doc.ctStripNoise('[IMAGE_001]\n第二十六条 本合同自签订之日起生效。')
     === '第二十六条 本合同自签订之日起生效。'],
  // ②'' 但 ``[TABLE_00N: …]`` **不许**一起删：表格那块回插的是真数据。
  ['表格占位符留着（那是真数据）',
   doc.ctStripNoise('[TABLE_001: 岗位 | 月薪]\n第二十七条 附件。')
     === '[TABLE_001: 岗位 | 月薪]\n第二十七条 附件。'],
  // ③ 摘水印要**在合并之前**：落款日期不以句号收尾，摘晚了它会把水印接成一段
  //    （"2024 年 2 月 28 日示例文件 · …"），屏幕上就是这个鬼样子。
  ['落款日期没跟水印粘在一起', (kind('date')[0] || {}).text.trim() === '2024 年 2 月 28 日'
   && blocks[blocks.length - 1] === kind('date')[0],
   JSON.stringify((kind('date')[0] || {}).text)],
  ['落款日期单独成块（右对齐用）', kind('date').length === 1
   && doc.ctBlockHTML(kind('date')[0]).includes('class="ct-p ct-date"'),
   doc.ctBlockHTML(kind('date')[0] || { kind: 'date', text: '' })],
  // ④ 句子中间嵌的日期（"本合同于 2024 年 3 月 1 日签订。"）**不算落款日期**：
  //    整行是日期才认（首尾锚点）。少了锚点，正文里的日期会被抠出来右对齐，
  //    那一条就断成两截。
  ['句中的日期不当落款',
   doc.ctDocBlocks('第一条 合同期自 2024年3月1日 起至 2027年2月28日 止，共计 36个月。\n'
     + '本合同于 2024 年 3 月 1 日签订。').every((b) => b.kind !== 'date')],
  // ② 解析器的页标不许漏进正文块（屏幕上只该有一条分页线）
  ['页标不进正文', blocks.every((b) => b.kind === 'page' || !b.text.includes('<!--'))],
  // ③ 孤立的页脚页码不许当正文段落（"1" 这种一行），也不许在正文里留下一个
  //    孤零零的数字：三页的页脚全部并进各自的分页线（data-foot 带着回收）。
  ['孤页脚码不当正文段', !kind('para').some((b) => /^\d{1,3}$/.test(b.text.trim()))],
  ['孤页脚码并进分页线', kind('pagenum').length === 0 && kind('page').length === 3
   && kind('page').every((b) => b.foot),
   `残留 ${kind('pagenum').length} 块，分页线 ${kind('page').filter((b) => b.foot).length}/3 带页脚`],
  // ④ 被 PDF 版面切断的半句话要接回去：首段跨过了原来的换行
  ['续行合并（正文段）', (kind('para')[0] || {}).text.indexOf('共同遵守本合同所列条款') > 0],
  // ⑤ 条款同理：第一条要把"…共计 36"和"个月。"接成一条
  ['续行合并（条款）', (kind('clause')[0] || {}).text.indexOf('个月。') > 0],
  // ⑥ 条数与后端 IntakeNode 拆出来的一致（那条任务 state.clauses = 25）。
  //    前端不是在拆条，只是认出"第X条"开头的段 —— 对得上说明没把两条粘一起。
  ['条款数 = 后端拆条数 25', kind('clause').length === 25, '实得 ' + kind('clause').length],
  // 这份劳动合同是九章（一、劳动合同期限 … 九、其他规定）。
  ['章标题齐全（9 章）', kind('chapter').length === 9, '实得 ' + kind('chapter').length],
  ['大标题居中（仅一个）', kind('title').length === 1
   && (kind('title')[0].text || '').replace(/\s/g, '') === '劳动合同书',
   JSON.stringify((kind('title')[0] || {}).text)],
  ['文件名头单独成类', kind('head').length === 1, JSON.stringify((kind('head')[0] || {}).text)],
];
let blockBad = 0;
for (const [n, ok, extra] of blockCases) { if (!ok) { bad++; blockBad++; } console.log(`${ok ? '✅' : '❌'} ${n}${extra ? '  ' + extra : ''}`); }

// 分页块的屏幕上文案与回收用的原始标记是两回事：屏上"第 1 页"，
// data-mark 里必须还是原始那串（它计进字数）。
const pageHTML = doc.ctBlockHTML({ kind: 'page', text: '<!-- 第 1 页 -->', page: '1', foot: '1' });
const soloHTML = doc.ctBlockHTML({ kind: 'pagenum', text: '3' });
const pageCases = [
  // 只看**屏幕上那部分**（剥掉属性）：data-mark 里那串转义过的原始标记里也有
  // "第 1 页" 三个字，拿整段去找会数出两个，这条断言就永远绿不了。
  ['分页线只画一个页号', (pageHTML.replace(/<div[^>]*>/, '').replace(/<\/div>$/, '')
   .match(/第 1 页/g) || []).length === 1, '页脚那行并进来了'],
  ['原始标记留在 data-mark', pageHTML.includes('data-mark="&lt;!-- 第 1 页 --&gt;"')],
  ['页脚页码留在 data-foot（回收要用）', pageHTML.includes('data-foot="1"')],
  ['分页线不可编辑（免得被顺手删掉标记）', pageHTML.includes('contenteditable="false"')],
  // 上一块不是分页线时（中间隔了张图）兜底：单独一行小字页号，也不出现裸数字
  ['没并处也不显示裸数字', soloHTML.includes('第 3 页') && soloHTML.includes('data-mark="3"')
   && !/>\s*3\s*</.test(soloHTML)],
];
for (const [n, ok] of pageCases) { if (!ok) bad++; console.log(`${ok ? '✅' : '❌'} ${n}`); }

// ── 风险卡：一条一卡、不截断 ────────────────────────────────
console.log('\n风险卡');
const many = Array.from({ length: 12 }, (_, i) => ({
  clause_no: '第' + (i + 1) + '条', clause_title: '', clause_text: '原文 ' + i,
  risk: '风险 ' + i, level: i % 3 === 0 ? 'high' : (i % 3 === 1 ? 'medium' : 'low'),
  suggestion: '建议 ' + i, basis: i === 0 ? ['《民法典》第五百八十五条（经检索核对）'] : [],
}));
const cardsHTML = doc.ctRiskCards(risks);
const manyHTML = doc.ctRiskCards(many);
const cardCases = [
  ['真实风险点逐条成卡', (cardsHTML.match(/class="ct-risk"/g) || []).length === risks.length,
   risks.length + ' 条'],
  // 原来这里 slice(0,8)，第 9 条之后的**永远不显示**。别把截断加回来。
  ['12 条不截断', (manyHTML.match(/class="ct-risk"/g) || []).length === 12],
  ['四要素齐全（原文/风险/建议/依据）',
   cardsHTML.includes('ct-risk-q') && cardsHTML.includes('ct-risk-r')
   && cardsHTML.includes('ct-risk-s') && cardsHTML.includes('ct-risk-b')],
  ['依据被过滤空时如实写需补充', manyHTML.includes('（需补充法律依据）')],
  ['原文取自后端的 clause_text', cardsHTML.includes(risks[0].clause_text.slice(0, 8))],
];
for (const [n, ok, extra] of cardCases) { if (!ok) bad++; console.log(`${ok ? '✅' : '❌'} ${n}${extra ? '  ' + extra : ''}`); }

// ── 卡片里那段「原文」要干净 ────────────────────────────────
// 后端回填的 clause_text 是**按位置切**的，跨页的条款会把解析痕迹一起带进来：
// 页标 ``<!-- 第 3 页 -->``、孤立的页脚页码「3」、图片占位符。下面这段脏原文是
// **真实数据** —— 抄自 task_fa0e56868bcc 里第二十一条那两张卡的 clause_text
// （那份合同里夹着一枚"上海具识智融科技有限公司"的电子章）。
console.log('\n风险卡原文（跨页条款）');
const dirtyQuote = '损失、招聘费、培训费等）。\n\n\n<!-- 第 3 页 -->\n\n\n'
  + '[IMAGE_001: 红色圆形公章，中心为五角星图案，外圈文字沿圆周排列，'
  + '内容为“上海具识智融科技有限公司”]\n\n3';
const cleanQuote = doc.ctCleanQuote(dirtyQuote);
const cleanCards = doc.ctRiskCards([{ clause_no: '第二十一条', clause_title: '', level: 'medium',
  clause_text: dirtyQuote, risk: 'x', suggestion: 'y', basis: [] }]);
const quoteCases = [
  ['原文里的页标清掉', !cleanQuote.includes('<!--'), JSON.stringify(cleanQuote.slice(-40))],
  ['原文里的图片占位符清掉', !cleanQuote.includes('[IMAGE_')],
  ['原文里的孤立页码清掉', !/^\s*\d{1,3}\s*$/m.test(cleanQuote)],
  // **别清过头**：条款正文一个字都不许少（"留 3"这种误删比留着页标更糟）。
  // 被摘掉的是页标/页码/整行的占位符说明；条款正文必须原样留着。
  ['条款正文一个字不少', cleanQuote.includes('损失、招聘费、培训费等）。')
   && !cleanQuote.includes('上海具识智融科技有限公司'),
   JSON.stringify(cleanQuote.slice(0, 30))],
  ['干净原文原样返回', doc.ctCleanQuote(risks[0].clause_text) === risks[0].clause_text.trim()],
  ['卡片 HTML 里也没有解析痕迹',
   !cleanCards.includes('<!--') && !cleanCards.includes('[IMAGE_') && !cleanCards.includes('第 3 页')],
];
for (const [n, ok, extra] of quoteCases) { if (!ok) bad++; console.log(`${ok ? '✅' : '❌'} ${n}${extra ? '  ' + extra : ''}`); }

// 主体审查（ctSubjectPane / CT_SUBJ_ROWS）2026-09-29 整块删除，它那一节
// 断言（抽得到的照实写、抽不到的写"未接入"、绝不打绿勾）随之作废 ——
// 页面上已经没有任何地方会显示这些字段了。

// ── 真实合同正文：传一个 txt 路径才跑（平时不依赖桌面文件） ────
const samplePath = process.argv[2];
if (samplePath && fs.existsSync(samplePath)) {
  console.log('\n真实合同：' + samplePath);
  const text = fs.readFileSync(samplePath, 'utf8');
  const got = make(mk('', {}), PARTIES).ctGuessParties(text);
  const want = { '甲方': '上海具识智能科技有限公司', '乙方': '李明轩' };
  const fails = Object.entries(want).filter(([k, v]) => got[k] !== v);
  if (fails.length) bad++;
  console.log(`${fails.length ? '❌' : '✅'} ${text.length} 字  甲=${JSON.stringify(got['甲方'])} 乙=${JSON.stringify(got['乙方'])}`);
  for (const [k, v] of fails) console.log(`      ↑ ${k}: 期望 ${JSON.stringify(v)}，实得 ${JSON.stringify(got[k])}`);
} else if (samplePath) {
  console.log('\n⚠ 样本不存在，跳过：' + samplePath);
}

console.log(bad ? `\n${bad} 条不符` : '\n全部通过');
process.exit(bad ? 1 : 0);
