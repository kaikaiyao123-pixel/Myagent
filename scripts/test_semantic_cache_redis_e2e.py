"""
语义缓存 —— 真 Redis 协议端到端测试（fakeredis 作为 Redis-API 兼容的内存服务）

为什么需要这个测试（相对 scripts/test_semantic_cache*.py 的 dict-mock）：
    dict-mock 直接存 Python 对象，不经过 Redis 的「bytes 上线路 / hgetall 解码」
    序列化。生产 get_semantic_cache 注入的是 services/redis.py 的
    redis.StrictRedis（**同步、bytes 模式**，未开 decode_responses），
    缓存把 float32 向量以 tobytes() 二进制落库。float32 字节往返、scan_iter 游标、
    哈希字段编解码这些只有在「真 Redis 协议」后端上才会暴露的问题，dict-mock 永远测不到。

本测试用 fakeredis.FakeStrictRedis（redis.StrictRedis 的 drop-in 替身，无需装服务端），
sync + bytes 模式，与生产 client 一致。

运行（在仓库根目录，环境已装 fakeredis —— CI 中单独 pip install，不进生产镜像）：
    python scripts/test_semantic_cache_redis_e2e.py

注意：切勿误用 fakeredis.aioredis.FakeRedis。它的 scan_iter 是 async generator，
      需 async for；而生产 semantic_cache.get() 是同步 for 迭代（redis.StrictRedis
      的 Cursor 在 for 内部惰性 await）。若在此用 aioredis 变体，会得到
      ``'async_generator' object is not iterable`` —— 那不是生产 bug，是测试替身选错。
"""
import asyncio
import sys
import unittest
from pathlib import Path

# 核心实现在后端包内（镜像只打包 src/backend，后端不能依赖 scripts/ 目录）；
# 本脚本是离线单测工坊，通过 sys.path 注入复用同一实现。
HERE = Path(__file__).resolve().parents[0]            # scripts/
BACKEND = Path(__file__).resolve().parents[1] / "src" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))                          # 复用 test_semantic_cache 的 fake_embed / PHASH

import fakeredis
from agentchat.services import semantic_cache as sc_mod
from test_semantic_cache import fake_embed, PHASH     # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _new_redis():
    # 生产等价：同步 StrictRedis + bytes 模式（无 decode_responses）
    return fakeredis.FakeStrictRedis(decode_responses=False)


class FakeredisE2ETest(unittest.TestCase):
    """miss -> put -> get 命中；并验证 kb_version / prompt_hash 两道门槛。"""

    def test_miss_then_put_then_hit(self):
        r = _new_redis()
        cache = sc_mod.SemanticCache(
            r, embed_fn=fake_embed, threshold=0.90, ttl=60, kb_version="v1")

        q = "退款政策是什么"
        # 首轮：未命中
        sim0, ans0 = _run(cache.get(q, PHASH))
        self.assertIsNone(ans0, "首轮应未命中")
        # 真实链路回填
        _run(cache.put(q, "7天无理由退换货", PHASH))
        # 同问题再来：应命中缓存、跳过 LLM
        sim1, ans1 = _run(cache.get(q, PHASH))
        self.assertGreaterEqual(sim1, 0.90)
        self.assertEqual(ans1, "7天无理由退换货", "应直接返回缓存答案")

        # 统计口径：total=2, hit=1
        st = cache.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["hit"], 1)
        self.assertAlmostEqual(st["hit_rate"], 0.5, places=4)

    def test_prompt_hash_gate_blocks(self):
        """prompt_hash 不一致 -> 即便向量相似也不命中（防不同模板答案串味）。"""
        r = _new_redis()
        cache = sc_mod.SemanticCache(
            r, embed_fn=fake_embed, threshold=0.90, ttl=60, kb_version="v1")
        _run(cache.put("退换货政策", "答案A", PHASH))
        other = "0" * 16  # 与 PHASH 明显不同的 hash
        sim, ans = _run(cache.get("退换货政策", other))
        self.assertIsNone(ans, "模板 hash 不同必须未命中")

    def test_kb_version_gate_blocks(self):
        """不同 kb_version -> 旧条目逻辑失效，未命中。"""
        r = _new_redis()
        c_old = sc_mod.SemanticCache(
            r, embed_fn=fake_embed, threshold=0.90, ttl=60, kb_version="v1")
        c_new = sc_mod.SemanticCache(
            r, embed_fn=fake_embed, threshold=0.90, ttl=60, kb_version="v2")
        _run(c_old.put("退款", "旧答案", PHASH))
        sim, ans = _run(c_new.get("退款", PHASH))   # 用 v2 查 -> 看不到 v1 条目
        self.assertIsNone(ans, "kb_version 不同必须未命中")

    def test_hit_refreshes_ttl(self):
        """命中后 TTL 从最后一次命中起算（验证 expire 真打到 Redis）。"""
        r = _new_redis()
        cache = sc_mod.SemanticCache(
            r, embed_fn=fake_embed, threshold=0.90, ttl=60, kb_version="v1")
        _run(cache.put("查询", "结果", PHASH))
        _run(cache.get("查询", PHASH))              # 命中
        # 命中后该 key 的 TTL 应被刷新为 60（而非剩余很少）
        keys = list(r.scan_iter(match="semcache:v1:*"))
        self.assertTrue(keys)
        ttl = r.ttl(keys[0])
        self.assertGreater(ttl, 30, "命中后续期应接近 60s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
