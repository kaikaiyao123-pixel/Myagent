"""
接入点结构回归测试：completion.py / general_agent.py 的三件套接线必须在位。

单测覆盖了服务层逻辑，但「接线被静默删掉」这类回归只有结构检查能抓。
本文件用 AST 断言关键调用存在，不执行重依赖（langchain 等装不全也不影响）。

运行：
    python scripts/test_integration_wiring.py
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "src" / "backend"


def _parse(rel: str) -> ast.Module:
    return ast.parse((BACKEND / rel).read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"函数 {name} 不存在")


def _calls_in(func) -> list[str]:
    """函数体内出现的全部调用名（含属性调用，如 cache.get）。"""
    names = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.append(f.id)
            elif isinstance(f, ast.Attribute):
                names.append(f.attr)
    return names


def _source_of(func) -> str:
    return ast.get_source_segment(
        (BACKEND / "agentchat/api/v1/completion.py").read_text(encoding="utf-8"),
        func) or ""


class CompletionWiringTest(unittest.TestCase):
    """completion.py：语义缓存门控/查询/写回 + Langfuse trace 根。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = _parse("agentchat/api/v1/completion.py")

    def test_imports_present(self):
        src = (BACKEND / "agentchat/api/v1/completion.py").read_text(encoding="utf-8")
        for frag in ("cache_eligible", "get_semantic_cache",
                     "prompt_hash as compute_prompt_hash",
                     "trace_chat", "message_to_text"):
            self.assertIn(frag, src, f"completion.py 缺少导入：{frag}")

    def test_completion_calls_cache_gate(self):
        func = _func(self.tree, "completion")
        calls = _calls_in(func)
        self.assertIn("cache_eligible", calls)
        self.assertIn("get_semantic_cache", calls)
        self.assertIn("get", calls)     # cache.get(...)
        self.assertIn("put", calls)     # stream() finally 里的 cache.put(...)

    def test_completion_wraps_trace_chat(self):
        func = _func(self.tree, "completion")
        calls = _calls_in(func)
        self.assertIn("trace_chat", calls)

    def test_fallback_marker_guard_on_writeback(self):
        """兜底文案不得写回缓存（否则错误答案被长期复用）。"""
        module_src = (BACKEND / "agentchat/api/v1/completion.py").read_text(encoding="utf-8")
        # 常量定义在模块级且值正确
        self.assertIn('_FALLBACK_MARKER = "知识盲区"', module_src)
        # completion() 内写回条件引用了该常量
        func_src = ast.get_source_segment(module_src, _func(self.tree, "completion"))
        self.assertIn("_FALLBACK_MARKER not in response_content", func_src)

    def test_cached_response_helper_exists(self):
        func = _func(self.tree, "_respond_from_cache")
        calls = _calls_in(func)
        self.assertIn("save_chat_history", calls)
        self.assertIn("update_dialog_summary", calls)


class GeneralAgentWiringTest(unittest.TestCase):
    """general_agent.py：模型调用中间件必须包 Langfuse generation。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = _parse("agentchat/core/agents/general_agent.py")

    def test_middleware_wraps_trace_generation(self):
        func = _func(self.tree, "awrap_model_call")
        calls = _calls_in(func)
        self.assertIn("trace_generation", calls)
        self.assertIn("update", calls)  # gen.update(output/usage_details/...)

    def test_usage_and_text_helpers_exist(self):
        for name in ("_extract_usage", "_response_to_text", "_model_display_name"):
            _func(self.tree, name)  # 不存在会 AssertionError


if __name__ == "__main__":
    unittest.main(verbosity=2)
