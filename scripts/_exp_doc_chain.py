"""实验：文书生成 × 证据链（A/B）。

设计
----
- **卷宗**（案件材料）写得很全：入职/解除时间、工资、加班、催款聊天记录、录音、
  银行流水、打卡截图、仲裁请求。
- **起草时输入的文字**故意写得很短 —— 这是律师真实的打字量。
- A 臂：`/doc/compose` 不带 case_id  → 模型只能拿到那段短文字。
- B 臂：`/doc/compose` 带 case_id + use_chain → 后端先把该案证据链压成摘要进 prompt。

判据（可证伪）
--------------
1. B 臂的 `chain.available` 是否为真（链到底取到没有）；
2. 两臂的正文**有没有差异**；差异是不是"卷宗里有、短文字里没有"的那些事实
   （银行流水 / 录音 / 打卡记录 / 具体金额）；
3. B 臂的 `missing`（待补栏目）是否比 A 臂少。

⚠️ 若模型在降级（/health 的 degraded），两臂可能完全相同 —— 那本身也是结论，
脚本会把 `provider`/`degraded` 一并记下来，别把它读成"功能没生效"。

用法：`.venv/Scripts/python.exe scripts/_exp_doc_chain.py`
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "scripts" / "_exp_doc_chain.log"
OUT_DIR = ROOT / "scripts" / "_exp_out"
BASE = "http://127.0.0.1:8001"
API = BASE + "/api/v1"

_logf = LOG_PATH.open("w", encoding="utf-8")


def log(msg: str = "") -> None:
    print(msg, flush=True)
    _logf.write(msg + "\n")
    _logf.flush()


CASE_TITLE = "实验案 · 张伟诉某科技公司劳动争议"
CASE_ID = "EXP-" + uuid.uuid4().hex[:8].upper()

MATERIAL = """劳动争议案件材料（申请人提交）

申请人：张伟，男，1990年5月12日出生，身份证号 110101199005124512，住北京市朝阳区。
被申请人：北京某某科技有限公司，法定代表人李强，统一社会信用代码 91110105XXXXXXXXXX。
联系电话：13800000000。委托诉讼代理人：无。

一、劳动关系与工资
2023年3月1日，申请人入职被申请人处，任软件工程师，双方**未签订书面劳动合同**。
月工资 18000 元，通过银行转账发放，有**工资银行流水**。

二、解除事实
2024年6月30日，被申请人以"经营困难"为由口头通知解除劳动关系，
**未提前30日书面通知**，也未支付经济补偿。解除过程有**录音**。

三、加班事实
2024年3月至6月，申请人工作日延时加班累计 86 小时，被申请人未支付加班费。
有**考勤打卡记录截图**。

四、催讨留痕
申请人多次通过微信向被申请人人事经理王某催要上述款项，对方以"公司资金紧张"推脱，
**微信聊天记录**尚在。

五、仲裁请求
1. 支付未签订书面劳动合同二倍工资差额 198000 元；
2. 支付违法解除劳动合同赔偿金 54000 元；
3. 支付加班费 21000 元。
"""

COMPOSE_TEXT = (
    "原告张伟，2023年3月入职北京某某科技有限公司任软件工程师，"
    "2024年6月被公司以经营困难为由辞退，没签书面劳动合同，也没发加班费。"
    "要求支付二倍工资差额、违法解除赔偿金和加班费。"
)

SHORT_FACTS = ("银行流水", "录音", "打卡", "考勤", "微信", "离职", "解除通知", "18000", "86")


def create_case(c: httpx.Client) -> tuple[bool, str]:
    r = c.post(f"{API}/cases", json={
        "case_id": CASE_ID, "title": CASE_TITLE, "case_type": "litigation",
        "cause_of_action": "劳动争议",
        "summary": "未签书面劳动合同、违法解除、拖欠加班费。",
    })
    return r.status_code in (200, 201), r.text[:300]


def upload_material(c: httpx.Client) -> tuple[str, str]:
    files = {"file": ("劳动仲裁材料.txt", MATERIAL.encode("utf-8"), "text/plain")}
    r = c.post(f"{API}/cases/{CASE_ID}/materials", files=files, data={"doc_type": "evidence"})
    if r.status_code not in (200, 201):
        return "", f"{r.status_code} {r.text[:200]}"
    mid = str(r.json().get("id") or "")
    p = c.post(f"{API}/materials/{mid}/parse")
    return mid, f"parse={p.status_code}"


def chain_block(body: dict) -> dict:
    return body.get("chain") or {}


def excerpt(content: str, *marks: str, span: int = 420) -> str:
    """从正文里截几段关键内容出来（全文太长，日志里放不下）。"""
    out = []
    for m in marks:
        i = content.find(m)
        if i >= 0:
            out.append(content[i:i + span].replace("\n", " ").strip())
    return "\n      | ".join(out) if out else "（未找到段落）"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(timeout=600.0)

    h = c.get(f"{BASE}/health").json()
    log(f"[环境] health={h.get('status')} version={h.get('version')}")
    subs = h.get("subsystems") or h.get("checks") or {}
    if isinstance(subs, dict):
        bad = {k: (v.get("status") if isinstance(v, dict) else v)
               for k, v in subs.items()
               if (v.get("status") if isinstance(v, dict) else v) not in ("ok", "ready", "up")}
        if bad:
            log(f"[环境] 降级项：{json.dumps(bad, ensure_ascii=False)}")

    tok = c.post(f"{API}/auth/login",
                 json={"username": "admin", "password": "admin123"}).json()["access_token"]
    c.headers["Authorization"] = "Bearer " + tok

    log(f"\n[1] 建案件 {CASE_ID}")
    okk, info = create_case(c)
    log(f"    -> {okk} {info}")
    if not okk:
        return 1

    log("[2] 上传卷宗材料 + 解析")
    mid, info = upload_material(c)
    log(f"    -> material_id={mid} {info}")

    log("[3] 生成证据链（显式，等价于页面上的「生成证据链」按钮）")
    t0 = time.time()
    r = c.post(f"{API}/cases/{CASE_ID}/evidence-chain")
    log(f"    -> HTTP {r.status_code}  {time.time() - t0:.1f}s")
    if r.status_code != 200:
        log(f"    正文：{r.text[:300]}")
    chain = (r.json().get("chain") or []) if r.status_code == 200 else []
    log(f"    链：{len(chain)} 条主张")
    for i, item in enumerate(chain, 1):
        evs = "、".join(f"{e.get('material', '')}（{e.get('support', '')}）"
                        for e in (item.get("evidence") or [])[:4]) or "无"
        log(f"      {i}. {str(item.get('claim') or '')[:60]}")
        log(f"         支撑：{evs[:140]}")
        if item.get("gap"):
            log(f"         缺口：{str(item['gap'])[:120]}")
    log(f"    generated_at={(r.json() or {}).get('generated_at') if r.status_code == 200 else ''}")

    # ---------------- A 臂：不关联案件 ----------------
    log("\n[4] A 臂：不带 case_id 起草（对照组）")
    t0 = time.time()
    ra = c.post(f"{API}/doc/compose", json={"text": COMPOSE_TEXT, "use_chain": False})
    log(f"    -> HTTP {ra.status_code}  {time.time() - t0:.1f}s")
    A = ra.json() if ra.status_code == 200 else {}
    if ra.status_code != 200:
        log(f"    正文：{ra.text[:300]}")
    (OUT_DIR / "armA_无证据链.txt").write_text(str(A.get("content") or ra.text),
                                               encoding="utf-8")

    # ---------------- B 臂：关联案件（带证据链）----------------
    log("\n[5] B 臂：带 case_id + use_chain 起草（实验组）")
    t0 = time.time()
    rb = c.post(f"{API}/doc/compose", json={
        "text": COMPOSE_TEXT, "case_id": CASE_ID, "use_chain": True})
    log(f"    -> HTTP {rb.status_code}  {time.time() - t0:.1f}s")
    B = rb.json() if rb.status_code == 200 else {}
    if rb.status_code != 200:
        log(f"    正文：{rb.text[:300]}")
    (OUT_DIR / "armB_带证据链.txt").write_text(str(B.get("content") or rb.text),
                                               encoding="utf-8")

    # ---------------- 对比 ----------------
    log("\n[6] 对比")
    if A and B:
        ca, cb = str(A.get("content") or ""), str(B.get("content") or "")
        log(f"    A 模板：{(A.get('template') or {}).get('file')}   "
            f"已填 {A.get('filled_count')}/{A.get('field_count')}  待填 {len(A.get('missing') or [])}")
        log(f"    B 模板：{(B.get('template') or {}).get('file')}   "
            f"已填 {B.get('filled_count')}/{B.get('field_count')}  待填 {len(B.get('missing') or [])}")
        log(f"    B 的 provider={(B.get('provider'))} degraded={B.get('degraded')}")

        ch = chain_block(B)
        log(f"    B.chain = {json.dumps(ch, ensure_ascii=False)[:400]}")
        gr = (B.get("evidence") or {}).get("evidence_gap") or []
        log(f"    B.evidence_gap 条数={len(gr)}")
        for g in gr[:4]:
            if isinstance(g, dict):
                log(f"      - [{g.get('kind', '')}] {str(g.get('reason') or '')[:120]}")

        log(f"\n    正文完全相同？ {'**是**' if ca == cb else '**否**（有差异）'}"
            f"   A={len(ca)} 字  B={len(cb)} 字")

        if ca != cb:
            # 只看"卷宗里有、短文字里没有"的那些词，有没有从 A 漏到 B
            log("\n    卷宗里的关键事实在正文里的出现情况：")
            for kw in SHORT_FACTS:
                log(f"      {kw:<8} A={'有' if kw in ca else '无'}   B={'有' if kw in cb else '无'}"
                    f"   {'← B 新增' if (kw not in ca and kw in cb) else ''}")
            log("\n    【B 的事实与理由】")
            log("      " + excerpt(cb, "事实与理由", "事实和理由", "事实及理由"))
            log("\n    【A 的事实与理由】")
            log("      " + excerpt(ca, "事实与理由", "事实和理由", "事实及理由"))

        log("\n    待补栏目差异（A 有 B 无 = 被证据链补上了）：")
        ma = set(A.get("missing") or [])
        mb = set(B.get("missing") or [])
        log(f"      A 有 B 无：{sorted(ma - mb)[:12]}")
        log(f"      B 有 A 无：{sorted(mb - ma)[:12]}")

    log(f"\n[产出] 两份正文已存到 {OUT_DIR}")
    log(f"[案件] {CASE_ID}（留在工作台里可打开看：文书中心 / 证据链页）")
    log("RESULT: DONE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("实验脚本自身出错：\n" + traceback.format_exc())
        raise SystemExit(1) from exc
