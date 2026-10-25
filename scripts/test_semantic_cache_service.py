"""
SemanticCache 服务层单测：门控函数 + 配置单例（离线可跑）

运行：
    python scripts/test_semantic_cache_service.py

覆盖 cache_eligible 门控全部分支、get_semantic_cache 的
禁用/缺配/非法阈值/Redis 不可用降级、注入 seam 的成功路径与单例语义。
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from agentchat.services import semantic_cache as sc_mod
from agentchat.settings import app_settings
from test_semantic_cache import FakeRedis, fake_embed, PHASH  # noqa: E402


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class CacheEligibleTest(unittest.TestCase):
    """门控是纯函数：任一条件不满足都不能走缓存（方案 2.1 适用边界）。"""

    def test_all_clear_eligible(self):
        self.assertTrue(sc_mod.cache_eligible(
            file_url=None, enable_memory=False, has_short_history=False))

    def test_empty_file_url_still_eligible(self):
        self.assertTrue(sc_mod.cache_eligible(
            file_url="", enable_memory=False, has_short_history=False))

    def test_file_url_blocks(self):
        self.assertFalse(sc_mod.cache_eligible(
            file_url="http://minio/x.pdf", enable_memory=False, has_short_history=False))

    def test_memory_enabled_blocks(self):
        self.assertFalse(sc_mod.cache_eligible(
            file_url=None, enable_memory=True, has_short_history=False))

    def test_history_blocks(self):
        self.assertFalse(sc_mod.cache_eligible(
            file_url=None, enable_memory=False, has_short_history=True))

    def test_all_conditions_blocked(self):
        self.assertFalse(sc_mod.cache_eligible(
            file_url="u", enable_memory=True, has_short_history=True))


class GetSemanticCacheTest(unittest.TestCase):
    def setUp(self):
        sc_mod._cache_init_done = False
        sc_mod._cache_singleton = None
        self._had = hasattr(app_settings, "semantic_cache")
        self._old = getattr(app_settings, "semantic_cache", None)

    def tearDown(self):
        sc_mod._cache_init_done = False
        sc_mod._cache_singleton = None
        if self._had:
            app_settings.semantic_cache = self._old
        elif hasattr(app_settings, "semantic_cache"):
            del app_settings.semantic_cache

    def _set_cfg(self, **kw):
        app_settings.semantic_cache = kw

    def test_no_config_returns_none(self):
        if hasattr(app_settings, "semantic_cache"):
            del app_settings.semantic_cache
        self.assertIsNone(sc_mod.get_semantic_cache())

    def test_non_dict_config_returns_none(self):
        app_settings.semantic_cache = "oops"
        self.assertIsNone(sc_mod.get_semantic_cache())

    def test_disabled_returns_none(self):
        self._set_cfg(enabled=False, threshold=0.9, ttl=100, kb_version="v1")
        # 注入假件：确保即使依赖齐备，disabled 也必须返回 None
        # （不注入的话会被 import 失败掩盖，反向测试打不到这个分支）
        self.assertIsNone(sc_mod.get_semantic_cache(
            embed_fn=fake_embed, redis_conn=FakeRedis()))

    def test_invalid_threshold_returns_none(self):
        """threshold=1.5 触发 SemanticCache 的 ValueError，必须被吞掉降级为 None。"""
        self._set_cfg(enabled=True, threshold=1.5, ttl=100, kb_version="v1")
        self.assertIsNone(sc_mod.get_semantic_cache(
            embed_fn=fake_embed, redis_conn=FakeRedis()))

    def test_redis_unavailable_returns_none(self):
        """redis_client 没有 .connection（未正确初始化）-> 降级 None，不崩溃。
        必须注入 embed_fn：否则会先在 rag.embedding 的 import 上失败返回 None，
        根本摸不到 redis 路径（反向测试工坊踩过这个假阳性）。"""
        self._set_cfg(enabled=True, threshold=0.9, ttl=100, kb_version="v1")
        fake_mod = types.ModuleType("agentchat.services.redis")

        class _NoConn:
            pass

        fake_mod.redis_client = _NoConn()
        saved = sys.modules.get("agentchat.services.redis")
        sys.modules["agentchat.services.redis"] = fake_mod
        try:
            self.assertIsNone(sc_mod.get_semantic_cache(embed_fn=fake_embed))
        finally:
            if saved is not None:
                sys.modules["agentchat.services.redis"] = saved
            else:
                sys.modules.pop("agentchat.services.redis", None)

    def test_enabled_with_injected_deps(self):
        """注入 seam：假 embed + 假 redis，验证配置正确传入且缓存可用。"""
        self._set_cfg(enabled=True, threshold=0.9, ttl=100, kb_version="v9")
        r = FakeRedis()
        cache = sc_mod.get_semantic_cache(embed_fn=fake_embed, redis_conn=r)
        self.assertIsInstance(cache, sc_mod.SemanticCache)
        self.assertEqual(cache.threshold, 0.9)
        self.assertEqual(cache.ttl, 100)
        self.assertEqual(cache.kb_version, "v9")
        _run(cache.put("退款政策是什么", "7天无理由", PHASH))
        sim, ans = _run(cache.get("怎么退款", PHASH))
        self.assertEqual(ans, "7天无理由")
        self.assertGreaterEqual(sim, 0.9)

    def test_singleton_same_object(self):
        self._set_cfg(enabled=True, threshold=0.9, ttl=100, kb_version="v1")
        c1 = sc_mod.get_semantic_cache(embed_fn=fake_embed, redis_conn=FakeRedis())
        c2 = sc_mod.get_semantic_cache()
        self.assertIs(c1, c2)

    def test_defaults_when_fields_missing(self):
        """只给 enabled，其余字段缺省 -> 用默认 threshold/ttl/kb_version。"""
        self._set_cfg(enabled=True)
        cache = sc_mod.get_semantic_cache(embed_fn=fake_embed, redis_conn=FakeRedis())
        self.assertEqual(cache.threshold, 0.90)
        self.assertEqual(cache.ttl, 86400)
        self.assertEqual(cache.kb_version, "v1")


class InitAppSettingsTest(unittest.TestCase):
    """init_app_settings 必须能加载含 semantic_cache 的配置。

    背景：pydantic v1 BaseSettings 禁止 setattr 未声明字段。
    Settings 漏声明 semantic_cache 时，整个 YAML 加载会被 except 吞掉、
    只留一行日志 —— 线上表现为「配置改了没生效」且极难排查。
    """

    def test_yaml_with_semantic_cache_loads(self):
        import asyncio
        import tempfile
        from pathlib import Path

        from agentchat.settings import init_app_settings

        yaml_text = (
            "server:\n  env: test\n"
            "semantic_cache:\n  enabled: true\n  threshold: 0.93\n"
            "  ttl: 3600\n  kb_version: \"v2\"\n"
            "langfuse:\n  enabled: false\n  public_key: \"\"\n"
        )
        with tempfile.NamedTemporaryFile(
                "w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(yaml_text)
            path = f.name
        try:
            asyncio.run(init_app_settings(path))
        finally:
            Path(path).unlink(missing_ok=True)

        cfg = app_settings.semantic_cache
        self.assertIsInstance(cfg, dict)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["threshold"], 0.93)
        self.assertEqual(cfg["kb_version"], "v2")
        # 不影响既有 langfuse 字段
        self.assertFalse(app_settings.langfuse.get("enabled"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
