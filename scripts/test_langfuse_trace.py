"""
Langfuse 埋点模块单测：双重降级 + 注入 seam + 异常安全（离线可跑）

运行：
    python scripts/test_langfuse_trace.py

用 FakeLangfuse 记录调用，不联网、不需要真实 Key。
"""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

import agentchat.services.langfuse_trace as lt  # noqa: E402
from agentchat.settings import app_settings  # noqa: E402


class FakeObs:
    """模拟 langfuse observation：记录 update 调用，可配置 update 抛错。"""

    def __init__(self, client, kwargs):
        self.client = client
        self.kwargs = kwargs
        self.updates = []
        self.update_raises = False

    def __enter__(self):
        self.client.entered.append(self.kwargs)
        self.client.stack.append(self)
        return self

    def __exit__(self, *exc):
        self.client.stack.pop()
        return False

    def update(self, **kwargs):
        if self.update_raises:
            raise RuntimeError("模拟上报失败")
        self.updates.append(kwargs)


class FakeLangfuse:
    def __init__(self):
        self.entered = []   # start_as_current_observation 的入参记录
        self.stack = []     # 当前 observation 栈（模拟上下文挂载）
        self.trace_ids = 0

    def create_trace_id(self):
        self.trace_ids += 1
        return f"trace-{self.trace_ids}"

    def start_as_current_observation(self, **kwargs):
        return FakeObs(self, kwargs)


class _Msg:
    def __init__(self, type_, content):
        self.type = type_
        self.content = content


class LangfuseTraceTest(unittest.TestCase):
    def setUp(self):
        lt._client = lt._UNSET
        self._had = hasattr(app_settings, "langfuse")
        self._old = getattr(app_settings, "langfuse", None)
        app_settings.langfuse = {}

    def tearDown(self):
        lt._client = lt._UNSET
        if self._had:
            app_settings.langfuse = self._old
        elif hasattr(app_settings, "langfuse"):
            del app_settings.langfuse

    # ---------------------------------------------------------- 降级路径
    def test_no_package_degrades(self):
        """langfuse 包缺失 -> get_langfuse None，两个上下文都 yield None。"""
        saved = sys.modules.get("langfuse")
        sys.modules["langfuse"] = None  # 强制 import 失败
        importlib.reload(lt)
        try:
            self.assertIsNone(lt.get_langfuse())
            with lt.trace_generation(name="g") as gen:
                self.assertIsNone(gen)
            with lt.trace_chat(name="c") as root:
                self.assertIsNone(root)
        finally:
            if saved is not None:
                sys.modules["langfuse"] = saved
            else:
                sys.modules.pop("langfuse", None)
            importlib.reload(lt)

    def test_disabled_degrades(self):
        app_settings.langfuse = {"enabled": False}
        self.assertIsNone(lt.get_langfuse())
        with lt.trace_generation(name="g") as gen:
            self.assertIsNone(gen)

    def test_missing_keys_degrades(self):
        app_settings.langfuse = {"enabled": True, "public_key": "", "secret_key": ""}
        self.assertIsNone(lt.get_langfuse())

    def test_client_init_failure_degrades(self):
        app_settings.langfuse = {
            "enabled": True, "public_key": "pk", "secret_key": "sk"}

        class Boom:
            def __init__(self, **kw):
                raise ConnectionError("模拟初始化失败")

        saved = lt._Langfuse
        lt._Langfuse = Boom
        try:
            self.assertIsNone(lt.get_langfuse())
        finally:
            lt._Langfuse = saved
            lt._client = lt._UNSET

    # ---------------------------------------------------------- 启用路径
    def test_generation_records_start_and_update(self):
        fake = FakeLangfuse()
        with lt.trace_generation(name="qwen_chat", model="qwen-plus",
                                 input="[human] 你好", metadata={"kb_version": "v1"},
                                 client=fake) as gen:
            self.assertIsNotNone(gen)
            gen.update(output="答案", model="qwen-plus",
                       usage_details={"input": 10, "output": 5, "total": 15},
                       metadata={"total_ms": 820})
        self.assertEqual(len(fake.entered), 1)
        start = fake.entered[0]
        self.assertEqual(start["name"], "qwen_chat")
        self.assertEqual(start["model"], "qwen-plus")
        self.assertEqual(start["input"], "[human] 你好")
        self.assertEqual(start["as_type"], "generation")
        self.assertEqual(start["metadata"]["kb_version"], "v1")
        self.assertEqual(len(gen._obs.updates), 1)
        upd = gen._obs.updates[0]
        self.assertEqual(upd["output"], "答案")
        self.assertEqual(upd["usage_details"]["total"], 15)
        self.assertEqual(upd["metadata"]["total_ms"], 820)

    def test_chat_creates_trace_and_nested_generation(self):
        fake = FakeLangfuse()
        with lt.trace_chat(name="agentchat", user_id="u1", session_id="d1",
                           input="[human] 你好", client=fake) as root:
            self.assertIsNotNone(root)
            self.assertEqual(root._obs.kwargs["as_type"], "span")
            self.assertEqual(root._obs.kwargs["trace_context"]["trace_id"], "trace-1")
            self.assertEqual(root._obs.kwargs["metadata"]["user_id"], "u1")
            with lt.trace_generation(name="qwen_chat", client=fake) as gen:
                self.assertIsNotNone(gen)
        # 退出后栈为空（上下文正确闭合）
        self.assertEqual(fake.stack, [])

    def test_trace_chat_disabled_by_config(self):
        """trace_chat=false 时连 trace 根都不建（配置项生效）。"""
        app_settings.langfuse = {"enabled": True, "trace_chat": False}
        fake = FakeLangfuse()
        # 显式传 client 也会被 trace_chat 配置挡住 —— 配置优先
        with lt.trace_chat(name="c", client=fake) as root:
            self.assertIsNone(root)

    def test_update_failure_swallowed(self):
        fake = FakeLangfuse()
        with lt.trace_generation(name="g", client=fake) as gen:
            gen._obs.update_raises = True
            gen.update(output="x")  # 不得抛异常
        # 主链路不受影响即通过

    def test_exception_propagates_and_marks_error(self):
        fake = FakeLangfuse()
        with self.assertRaises(ValueError):
            with lt.trace_generation(name="g", client=fake) as gen:
                raise ValueError("boom")
        self.assertEqual(gen._obs.updates[0]["level"], "ERROR")
        self.assertEqual(gen._obs.updates[0]["status_message"], "boom")

    def test_message_to_text(self):
        msgs = [_Msg("human", "你好"), _Msg("ai", "您好")]
        text = lt.message_to_text(msgs)
        self.assertIn("[human] 你好", text)
        self.assertIn("[ai] 您好", text)

    def test_message_to_text_truncates(self):
        msgs = [_Msg("human", "x" * 5000)]
        text = lt.message_to_text(msgs)
        self.assertLessEqual(len(text), 4010)
        self.assertTrue(text.endswith("…<截断>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
