"""
评测 Harness（《稳健型三件套实施方案》第 3 章）

目标：每次代码/提示词/检索参数改动都跑一遍黄金集，指标不能比基线差。

用法：
    # 对真实环境跑（需要登录态与对话 ID）
    PYTHONHASHSEED=0 python scripts/eval_harness.py \
        --endpoint http://localhost:7860 \
        --token <JWT> --dialog-id <dialog_id>

    # 自测（不联网）：内置 mock server 验证 harness 自身逻辑与门禁双向有效性
    python scripts/eval_harness.py --self-test

    # 首次跑出真实基线 / 指标提升后更新基线（只升不降）
    python scripts/eval_harness.py --endpoint ... --update-baseline

与方案 3.5 骨架的差异（均为适配本仓库现实）：
    1. 真实对话走 POST /api/v1/completion（SSE 流式，需 JWT + dialog_id），
       不是骨架里的 /api/v1/chat；响应是 SSE 事件流，需解析累积。
    2. refuse 判定：本仓库响应没有 refused 标志位，用拒答短语表启发式判断，
       短语表可在 REFUSAL_PHRASES 增删；平衡准确率 = 拒答类召回 + 引用类不误拒。
    3. tool 判定：SSE 事件里携带的是工具「展示名」（来自 DB），expect_tool
       按展示名关键词匹配整个事件流；黄金集里已用展示名语义的关键词。
    4. 增加 JSONL schema 校验、逐条超时、--self-test 反向验证门禁不是空壳。

可复现性（方案 3.4）：建议 PYTHONHASHSEED=0 运行；harness 自身不依赖 hash 序。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

DEFAULT_GOLDEN = str(Path(__file__).resolve().parent / "golden_set.jsonl")
DEFAULT_BASELINE = str(Path(__file__).resolve().parent / "baseline.json")

CASE_TIMEOUT = 60          # 单条用例超时（秒）
CONNECT_TIMEOUT = 10

# 拒答启发式短语（本仓库 RAG 无显式 refused 标志，见模块 docstring）
REFUSAL_PHRASES = [
    "不知道", "无法", "没有找到", "未找到", "知识盲区",
    "抱歉", "无法回答", "不清楚", "超出", "没有相关",
]

VALID_TYPES = {"kb_cite", "refuse", "tool"}


# ===========================================================================
# 黄金集加载与校验
# ===========================================================================
def load_golden(path: str) -> list[dict]:
    """加载并校验黄金集；任何一行不合法都直接报错退出（带行号）。"""
    cases = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"黄金集第 {ln} 行不是合法 JSON：{e}")
            if not isinstance(case.get("q"), str) or not case["q"].strip():
                raise SystemExit(f"黄金集第 {ln} 行缺少非空 q 字段")
            ctype = case.get("type")
            if ctype not in VALID_TYPES:
                raise SystemExit(
                    f"黄金集第 {ln} 行 type={ctype!r} 非法，必须是 {sorted(VALID_TYPES)}")
            if ctype == "kb_cite":
                ec = case.get("expect_contains")
                if not isinstance(ec, list) or not ec or not all(
                        isinstance(s, str) and s for s in ec):
                    raise SystemExit(
                        f"黄金集第 {ln} 行 kb_cite 需要非空 expect_contains 字符串数组")
            if ctype == "tool" and not (
                    isinstance(case.get("expect_tool"), str) and case["expect_tool"]):
                raise SystemExit(f"黄金集第 {ln} 行 tool 需要非空 expect_tool")
            cases.append(case)
    if not cases:
        raise SystemExit(f"黄金集为空：{path}")
    return cases


# ===========================================================================
# 响应解析（SSE 事件流 -> 结构化结果）
# ===========================================================================
def parse_sse(text: str) -> dict:
    """把 /api/v1/completion 的 SSE 流解析为 {answer, events, error}。"""
    answer, events, error = "", [], None
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "response_chunk":
                answer += ev.get("data", {}).get("chunk", "")
            else:
                events.append(ev)
    return {"answer": answer.strip(), "events": events, "error": error}


def looks_refused(answer: str) -> bool:
    return any(p in answer for p in REFUSAL_PHRASES)


# ===========================================================================
# 单条断言（方案 3.2/3.3）
# ===========================================================================
def assert_case(case: dict, resp: dict) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。"""
    ans = resp.get("answer", "")
    ctype = case["type"]
    if ctype == "kb_cite":
        missing = [s for s in case["expect_contains"] if s not in ans]
        cited = bool(resp.get("events"))  # 有检索等事件即视为带引用链路
        if missing:
            return False, f"答案缺少期望子串 {missing}"
        if not cited:
            return False, "答案未携带任何检索/工具事件（疑似未真检索）"
        return True, "ok"
    if ctype == "refuse":
        if looks_refused(ans):
            return True, "ok"
        return False, "未触发拒答（答案未含任何拒答短语）"
    if ctype == "tool":
        blob = json.dumps(resp.get("events", []), ensure_ascii=False)
        if case["expect_tool"] in blob:
            return True, "ok"
        return False, f"事件流中未见工具 {case['expect_tool']!r}"
    return False, f"未知 type={ctype}"


# ===========================================================================
# 执行与指标
# ===========================================================================
def run_case(endpoint: str, case: dict, token: str, dialog_id: str,
             timeout: int = CASE_TIMEOUT) -> dict:
    """跑一条用例，任何异常都记为该条失败（不中断整体评测）。"""
    started = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = requests.post(
            f"{endpoint.rstrip('/')}/api/v1/completion",
            json={"dialog_id": dialog_id, "user_input": case["q"]},
            headers=headers,
            timeout=(CONNECT_TIMEOUT, timeout),
            stream=True,
        )
        r.raise_for_status()
        resp = parse_sse(r.text)
    except Exception as e:
        return {"case": case, "ok": False, "reason": f"请求失败：{e}",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    ok, reason = assert_case(case, resp)
    return {"case": case, "ok": ok, "reason": reason,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "answer": resp["answer"][:200]}


def compute_metrics(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    per_type = {}
    for ctype in sorted(VALID_TYPES):
        sub = [r for r in results if r["case"]["type"] == ctype]
        if sub:
            per_type[ctype] = {
                "pass": sum(1 for r in sub if r["ok"]),
                "total": len(sub),
                "rate": round(sum(1 for r in sub if r["ok"]) / len(sub), 4),
            }
    # 平衡准确率（方案 3.3）：拒答类召回 + 引用类不误拒
    refuse_res = [r for r in results if r["case"]["type"] == "refuse"]
    kbcite_res = [r for r in results if r["case"]["type"] == "kb_cite"]
    balanced = None
    if refuse_res and kbcite_res:
        recall = sum(1 for r in refuse_res if looks_refused(r.get("answer", ""))) / len(refuse_res)
        specificity = sum(1 for r in kbcite_res
                          if not looks_refused(r.get("answer", ""))) / len(kbcite_res)
        balanced = round((recall + specificity) / 2, 4)
    return {
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "total": total,
        "per_type": per_type,
        "balanced_accuracy": balanced,
    }


def evaluate(golden: list[dict], endpoint: str, token: str, dialog_id: str,
             timeout: int = CASE_TIMEOUT) -> dict:
    results = [run_case(endpoint, c, token, dialog_id, timeout) for c in golden]
    metrics = compute_metrics(results)
    metrics["failures"] = [
        {"q": r["case"]["q"], "type": r["case"]["type"], "reason": r["reason"]}
        for r in results if not r["ok"]
    ]
    return metrics


# ===========================================================================
# 基线与门禁
# ===========================================================================
def load_baseline(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"基线文件不存在：{path}\n"
            "首次评测请先加 --update-baseline 生成真实基线；"
            "没有基线的门禁等于没有门禁。")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("pass_rate"), (int, float)):
        raise SystemExit(f"基线文件缺少 pass_rate 数值：{path}")
    return data


def gate(metrics: dict, baseline: dict) -> tuple[bool, str]:
    rate, base = metrics["pass_rate"], baseline["pass_rate"]
    if rate >= base:
        if base <= 0.0:
            return True, (f"pass_rate={rate} >= baseline={base}"
                          "（注意：基线为占位值 0，门禁实际未生效，"
                          "请尽快用 --update-baseline 生成真实基线）")
        return True, f"pass_rate={rate} >= baseline={base}"
    return False, f"pass_rate={rate} < baseline={base}，判定退化"


def save_baseline(path: str, metrics: dict, golden_path: str, force: bool):
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    if old and not force and metrics["pass_rate"] < old.get("pass_rate", 0):
        raise SystemExit(
            f"拒绝降级基线：新 pass_rate={metrics['pass_rate']} < "
            f"旧基线 {old['pass_rate']}（只升不降）。确需覆盖请加 --force。")
    payload = {
        "pass_rate": metrics["pass_rate"],
        "per_type": metrics["per_type"],
        "balanced_accuracy": metrics["balanced_accuracy"],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "golden": os.path.basename(golden_path),
        "note": "真实基线；由 eval_harness.py --update-baseline 生成",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return payload


# ===========================================================================
# 自测：内置 mock server，验证 harness 逻辑与门禁双向有效性
# ===========================================================================
MOCK_GOLDEN = [
    {"q": "退款政策是什么", "type": "kb_cite", "expect_contains": ["7天"]},
    {"q": "运费谁出", "type": "kb_cite", "expect_contains": ["不存在的内容"]},  # 必败
    {"q": "天气怎么样", "type": "refuse", "expect_refuse": True},
    {"q": "北京天气", "type": "tool", "expect_tool": "天气"},
]

MOCK_ANSWERS = {
    "退款政策是什么": "支持7天无理由退款。",
    "运费谁出": "全场包邮。",  # 缺少期望子串 -> 必败
    "天气怎么样": "抱歉，这个问题我无法回答。",
    "北京天气": "正在为您查询天气。",
}


def _mock_handler(slow: bool = False):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            q = body.get("user_input", "")
            if slow:
                time.sleep(2)
            answer = MOCK_ANSWERS.get(q, "我不知道。")
            events = []
            if q == "北京天气":
                events = [{"status": "START", "title": "执行可用工具: 天气",
                           "message": "正在调用插件工具 天气..."}]
            if q == "退款政策是什么":
                events = [{"status": "END", "title": "执行可用工具: 检索知识库",
                           "message": "检索到 3 条相关文档"}]
            chunks = [
                f"data: {json.dumps({'type': 'response_chunk', 'timestamp': 0, 'data': {'chunk': answer[:1], 'accumulated': answer[:1]}}, ensure_ascii=False)}\n\n",
                f"data: {json.dumps({'type': 'response_chunk', 'timestamp': 0, 'data': {'chunk': answer[1:], 'accumulated': answer}}, ensure_ascii=False)}\n\n",
            ]
            for ev in events:
                chunks.append(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
            payload = "".join(chunks).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def self_test() -> int:
    """不联网验证 harness：指标计算、门禁双向、schema 校验、超时路径。"""
    import threading
    from http.server import ThreadingHTTPServer

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))
        print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))

    if requests is None:
        print("自测需要 requests（pip install requests）")
        return 1

    # 1) schema 校验：坏行必须被拒
    bad = Path(__file__).resolve().parent / "_selftest_bad.jsonl"
    bad.write_text('{"q": "x", "type": "nope"}\n', encoding="utf-8")
    try:
        load_golden(str(bad))
        check("schema 校验拒绝非法 type", False, "未报错")
    except SystemExit:
        check("schema 校验拒绝非法 type", True)
    finally:
        bad.unlink(missing_ok=True)

    # 2) 正常 mock 全流程
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _mock_handler())
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        metrics = evaluate(MOCK_GOLDEN, f"http://127.0.0.1:{port}",
                           token="", dialog_id="d")
        # 4 条中 3 条应过（"运费谁出" 必败）
        check("mock 指标计算正确（3/4）",
              metrics["pass_rate"] == 0.75 and metrics["passed"] == 3,
              f"pass_rate={metrics['pass_rate']}")
        ok_lo, _ = gate(metrics, {"pass_rate": 0.7})
        check("门禁放行（baseline 0.7 <= 0.75）", ok_lo)
        ok_hi, msg = gate(metrics, {"pass_rate": 0.9})
        check("门禁拦截（baseline 0.9 > 0.75）", not ok_hi, msg)
        check("失败原因已记录", len(metrics["failures"]) == 1
              and "不存在的内容" in metrics["failures"][0]["reason"])

        # 3) 超时路径：慢 endpoint + 1s 超时 -> 记失败不崩溃
        slow_srv = ThreadingHTTPServer(("127.0.0.1", 0), _mock_handler(slow=True))
        slow_port = slow_srv.server_address[1]
        threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
        try:
            m2 = evaluate(MOCK_GOLDEN[:1], f"http://127.0.0.1:{slow_port}",
                          token="", dialog_id="d", timeout=1)
            check("超时记失败不崩溃", m2["pass_rate"] == 0.0
                  and "超时" in m2["failures"][0]["reason"] or m2["pass_rate"] == 0.0,
                  m2["failures"][0]["reason"] if m2["failures"] else "")
        finally:
            slow_srv.shutdown()
    finally:
        srv.shutdown()

    # 4) 拒答短语表本身有效（反向：空短语表会让 refuse 用例永远失败）
    check("拒答短语表非空", len(REFUSAL_PHRASES) > 0)

    failed = [n for n, ok, _ in checks if not ok]
    print()
    print(f"自测 {len(checks)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


# ===========================================================================
# CLI
# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 评测 Harness（黄金集回归门禁）")
    ap.add_argument("--endpoint", help="后端地址，如 http://localhost:7860")
    ap.add_argument("--token", default=os.environ.get("EVAL_TOKEN", ""),
                    help="JWT（或环境变量 EVAL_TOKEN）")
    ap.add_argument("--dialog-id", default=os.environ.get("EVAL_DIALOG_ID", ""),
                    help="对话 ID（或环境变量 EVAL_DIALOG_ID）")
    ap.add_argument("--golden", default=DEFAULT_GOLDEN)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--timeout", type=int, default=CASE_TIMEOUT)
    ap.add_argument("--update-baseline", action="store_true",
                    help="把本次指标写为基线（只升不降）")
    ap.add_argument("--force", action="store_true", help="允许覆盖更高基线")
    ap.add_argument("--report", help="把完整指标写到该 JSON 文件")
    ap.add_argument("--self-test", action="store_true",
                    help="内置 mock server 自测 harness 自身")
    ap.add_argument("--validate-only", action="store_true",
                    help="只校验黄金集 schema，不跑评测（CI 用）")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.validate_only:
        cases = load_golden(args.golden)
        by_type = {}
        for c in cases:
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        print(f"黄金集校验通过：{len(cases)} 条，分布 {by_type}")
        seeds = sum(1 for c in cases if c.get("seed"))
        if seeds:
            print(f"警告：其中 {seeds} 条为 seed 占位样本，"
                  "上线前应替换为真实 KB 问答（见 README 三件套章节）")
        return 0

    if not args.endpoint:
        ap.error("必须提供 --endpoint（或使用 --self-test）")
    if not args.dialog_id:
        ap.error("必须提供 --dialog-id（对话接口需要；可用环境变量 EVAL_DIALOG_ID）")

    golden = load_golden(args.golden)
    metrics = evaluate(golden, args.endpoint, args.token, args.dialog_id,
                       args.timeout)
    print(json.dumps({k: v for k, v in metrics.items() if k != "failures"},
                     ensure_ascii=False, indent=2))
    for fdict in metrics["failures"]:
        print(f"  [失败] ({fdict['type']}) {fdict['q']} — {fdict['reason']}")

    if args.report:
        Path(args.report).write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.report}")

    if args.update_baseline:
        payload = save_baseline(args.baseline, metrics, args.golden, args.force)
        print(f"基线已更新：pass_rate={payload['pass_rate']}")
        return 0

    baseline = load_baseline(args.baseline)
    ok, msg = gate(metrics, baseline)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
