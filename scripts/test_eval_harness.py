"""
eval_harness / calibrate_threshold 单元测试（离线可跑）

运行：
    python scripts/test_eval_harness.py

覆盖：黄金集 schema 校验、SSE 解析、三类断言、指标计算、
      基线门禁双向、只升不降、阈值建议的 separable/overlap 两个分支、CLI 参数校验。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import numpy as np  # noqa: E402

import eval_harness as eh  # noqa: E402
import calibrate_threshold as ct  # noqa: E402

PY = sys.executable


class GoldenValidationTest(unittest.TestCase):
    def test_real_golden_set_loads(self):
        cases = eh.load_golden(str(Path(eh.__file__).parent / "golden_set.jsonl"))
        self.assertEqual(len(cases), 24)
        types = {c["type"] for c in cases}
        self.assertEqual(types, {"kb_cite", "refuse", "tool"})

    def _write_tmp(self, text) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                        encoding="utf-8")
        f.write(text)
        f.close()
        return f.name

    def test_bad_type_rejected(self):
        p = self._write_tmp('{"q": "x", "type": "nope"}\n')
        with self.assertRaises(SystemExit):
            eh.load_golden(p)

    def test_missing_q_rejected(self):
        p = self._write_tmp('{"type": "refuse"}\n')
        with self.assertRaises(SystemExit):
            eh.load_golden(p)

    def test_kb_cite_without_expect_contains_rejected(self):
        p = self._write_tmp('{"q": "x", "type": "kb_cite"}\n')
        with self.assertRaises(SystemExit):
            eh.load_golden(p)

    def test_tool_without_expect_tool_rejected(self):
        p = self._write_tmp('{"q": "x", "type": "tool"}\n')
        with self.assertRaises(SystemExit):
            eh.load_golden(p)

    def test_empty_golden_rejected(self):
        p = self._write_tmp("# 只有注释\n\n")
        with self.assertRaises(SystemExit):
            eh.load_golden(p)


class ParseSseTest(unittest.TestCase):
    def test_accumulates_chunks_and_events(self):
        text = (
            'data: {"type": "response_chunk", "data": {"chunk": "你好"}}\n\n'
            'data: {"type": "event", "data": {"title": "执行可用工具: 天气"}}\n\n'
            'data: {"type": "response_chunk", "data": {"chunk": "世界"}}\n\n'
            'data: [DONE]\n\n'
        )
        resp = eh.parse_sse(text)
        self.assertEqual(resp["answer"], "你好世界")
        self.assertEqual(len(resp["events"]), 1)

    def test_invalid_json_lines_ignored(self):
        text = "data: not-json\n\ndata: \n\n"
        resp = eh.parse_sse(text)
        self.assertEqual(resp["answer"], "")
        self.assertEqual(resp["events"], [])


class AssertCaseTest(unittest.TestCase):
    def test_kb_cite_pass(self):
        case = {"q": "q", "type": "kb_cite", "expect_contains": ["7天"]}
        resp = {"answer": "支持7天无理由", "events": [{"title": "检索"}]}
        ok, reason = eh.assert_case(case, resp)
        self.assertTrue(ok, reason)

    def test_kb_cite_fail_missing_substring(self):
        case = {"q": "q", "type": "kb_cite", "expect_contains": ["7天"]}
        ok, reason = eh.assert_case({"q": "q", "type": "kb_cite",
                                     "expect_contains": ["7天"]},
                                    {"answer": "包邮", "events": []})
        self.assertFalse(ok)
        self.assertIn("7天", reason)

    def test_kb_cite_fail_without_citations(self):
        case = {"q": "q", "type": "kb_cite", "expect_contains": ["7天"]}
        ok, reason = eh.assert_case(case, {"answer": "支持7天", "events": []})
        self.assertFalse(ok)
        self.assertIn("检索", reason)

    def test_refuse_pass_and_fail(self):
        case = {"q": "q", "type": "refuse"}
        ok, _ = eh.assert_case(case, {"answer": "抱歉，我无法回答", "events": []})
        self.assertTrue(ok)
        ok, _ = eh.assert_case(case, {"answer": "答案是 42", "events": []})
        self.assertFalse(ok)

    def test_tool_match(self):
        case = {"q": "q", "type": "tool", "expect_tool": "天气"}
        ok, _ = eh.assert_case(case, {"answer": "", "events": [
            {"title": "执行可用工具: 天气"}]})
        self.assertTrue(ok)
        ok, _ = eh.assert_case(case, {"answer": "", "events": [
            {"title": "执行可用工具: 画图"}]})
        self.assertFalse(ok)


class MetricsTest(unittest.TestCase):
    def _res(self, q, ctype, ok, answer=""):
        return {"case": {"q": q, "type": ctype}, "ok": ok,
                "reason": "", "answer": answer}

    def test_metrics_and_balanced_accuracy(self):
        results = [
            self._res("a", "kb_cite", True, "支持7天无理由"),
            self._res("b", "kb_cite", True, "包邮"),
            self._res("c", "refuse", True, "抱歉，无法回答"),
            self._res("d", "refuse", False, "答案是 42"),
            self._res("e", "tool", True),
        ]
        m = eh.compute_metrics(results)
        self.assertEqual(m["pass_rate"], 0.8)
        self.assertEqual(m["per_type"]["refuse"]["rate"], 0.5)
        # 拒答召回 1/2，引用类不误拒 2/2 -> (0.5+1)/2 = 0.75
        self.assertEqual(m["balanced_accuracy"], 0.75)

    def test_empty_results(self):
        m = eh.compute_metrics([])
        self.assertEqual(m["pass_rate"], 0.0)


class GateBaselineTest(unittest.TestCase):
    def test_gate_pass_and_fail(self):
        ok, _ = eh.gate({"pass_rate": 0.8}, {"pass_rate": 0.7})
        self.assertTrue(ok)
        ok, msg = eh.gate({"pass_rate": 0.6}, {"pass_rate": 0.7})
        self.assertFalse(ok)
        self.assertIn("退化", msg)

    def test_gate_placeholder_baseline_warns(self):
        ok, msg = eh.gate({"pass_rate": 0.5}, {"pass_rate": 0.0})
        self.assertTrue(ok)
        self.assertIn("门禁实际未生效", msg)

    def test_save_baseline_refuses_downgrade(self):
        d = Path(tempfile.mkdtemp())
        p = d / "baseline.json"
        p.write_text(json.dumps({"pass_rate": 0.9}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            eh.save_baseline(str(p), {"pass_rate": 0.5, "per_type": {},
                                      "balanced_accuracy": None},
                             "golden_set.jsonl", force=False)

    def test_save_baseline_force_allows_downgrade(self):
        d = Path(tempfile.mkdtemp())
        p = d / "baseline.json"
        p.write_text(json.dumps({"pass_rate": 0.9}), encoding="utf-8")
        payload = eh.save_baseline(str(p), {"pass_rate": 0.5, "per_type": {},
                                            "balanced_accuracy": None},
                                   "golden_set.jsonl", force=True)
        self.assertEqual(payload["pass_rate"], 0.5)


class SuggestThresholdTest(unittest.TestCase):
    def test_separable(self):
        r = ct.suggest_threshold([0.95, 0.96, 0.97, 0.98],
                                 [0.3, 0.4, 0.5, 0.6])
        self.assertEqual(r["mode"], "separable")
        self.assertGreater(r["suggestion"], 0.6)
        self.assertLess(r["suggestion"], 0.95)
        self.assertIsNone(r["warning"])

    def test_overlap_uses_f1_and_warns(self):
        r = ct.suggest_threshold([0.7, 0.75, 0.8, 0.85],
                                 [0.6, 0.72, 0.78, 0.82])
        self.assertEqual(r["mode"], "overlap_f1")
        self.assertIn("best_f1", r)
        self.assertIsNotNone(r["warning"])

    def test_cosine_matrix(self):
        mat = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        sims = ct.cosine_matrix(mat)
        self.assertAlmostEqual(sims[0, 1], 0.0)
        self.assertAlmostEqual(sims[0, 0], 1.0)
        self.assertAlmostEqual(sims[0, 2], 0.7071, places=3)

    def test_zero_vector_safe(self):
        mat = np.zeros((2, 3))
        sims = ct.cosine_matrix(mat)
        self.assertEqual(float(sims[0, 1]), 0.0)

    def test_mock_embed_deterministic_and_normalized(self):
        a = ct.mock_embed(["退款政策", "退款政策"])
        norms = np.linalg.norm(a, axis=1)
        self.assertTrue((norms <= 1.0 + 1e-9).all())
        self.assertTrue((a[0] == a[1]).all())

    def test_load_pairs_min_count(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                        encoding="utf-8")
        f.write('{"a": "x", "b": "y", "same": true}\n')
        f.close()
        with self.assertRaises(SystemExit):
            ct.load_pairs(f.name)


class CliTest(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run([PY, str(Path(eh.__file__).parent / "eval_harness.py"),
                               *argv], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_no_endpoint_errors(self):
        p = self._run()
        self.assertEqual(p.returncode, 2)

    def test_endpoint_without_dialog_id_errors(self):
        p = self._run("--endpoint", "http://127.0.0.1:1")
        self.assertEqual(p.returncode, 2)

    def test_self_test_passes(self):
        p = self._run("--self-test")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
