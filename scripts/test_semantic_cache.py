"""
SemanticCache 单元测试（离线可跑，不需要真实 Redis / embedding 服务）

运行：
    python scripts/test_semantic_cache.py
    # 或 python -m unittest scripts.test_semantic_cache -v

覆盖：命中 / 未命中 / 阈值边界 / prompt_hash 门槛 / kb_version 门槛 / TTL 过期 /
      命中后续期 / SCAN 竞态 / 残缺条目 / 维度不符 / 统计口径 / 异步 embed_fn /
      版本失效 / put->get 往返。

 FakeRedis 只实现 SemanticCache 用到的接口子集（见 semantic_cache.py docstring），
 行为对齐 redis-py 默认的 bytes 编解码。
"""
import asyncio
import fnmatch
import sys
import time
import unittest
from pathlib import Path

import numpy as np

# 核心实现在后端包内（镜像只打包 src/backend，后端不能依赖 scripts/ 目录）；
# 本脚本是离线单测工坊，通过 sys.path 注入复用同一实现。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from agentchat.services.semantic_cache import SemanticCache, prompt_hash


# ---------------------------------------------------------------------------
# FakeRedis：内存版 Redis，支持 TTL
# ---------------------------------------------------------------------------
class FakeRedis:
    def __init__(self):
        self._hash = {}   # name(bytes) -> {field(bytes): value(bytes)}
        self._expire_at = {}  # name(bytes) -> unix ts

    # -- 内部 ----------------------------------------------------------------
    def _key(self, name):
        return name if isinstance(name, bytes) else str(name).encode("utf-8")

    def _gc(self):
        now = time.time()
        for k in [k for k, ts in self._expire_at.items() if ts is not None and now >= ts]:
            self._hash.pop(k, None)
            self._expire_at.pop(k, None)

    # -- redis 接口子集 --------------------------------------------------------
    def hset(self, name, key=None, value=None, mapping=None, items=None):
        self._gc()
        n = self._key(name)
        h = self._hash.setdefault(n, {})
        written = 0
        if mapping:
            for k, v in mapping.items():
                h[self._key(k)] = self._key(v) if isinstance(v, (str, int)) else v
                written += 1
        if key is not None:
            h[self._key(key)] = self._key(value) if isinstance(value, (str, int)) else value
            written += 1
        return written

    def hgetall(self, name):
        self._gc()
        return dict(self._hash.get(self._key(name), {}))

    def expire(self, name, ttl):
        self._gc()
        n = self._key(name)
        if n not in self._hash:
            return False
        self._expire_at[n] = None if ttl is None else time.time() + int(ttl)
        return True

    def ttl(self, name):
        self._gc()
        ts = self._expire_at.get(self._key(name))
        if ts is None:
            return -1
        return max(0, int(round(ts - time.time())))

    def hincrby(self, name, key, amount=1):
        self._gc()
        h = self._hash.setdefault(self._key(name), {})
        k = self._key(key)
        cur = int(h.get(k, b"0"))
        cur += int(amount)
        h[k] = str(cur).encode("utf-8")
        return cur

    def delete(self, *names):
        self._gc()
        n = 0
        for name in names:
            if self._hash.pop(self._key(name), None) is not None:
                self._expire_at.pop(self._key(name), None)
                n += 1
        return n

    def scan_iter(self, match=None, count=None):
        self._gc()
        keys = [k for k in list(self._hash.keys())
                if match is None or fnmatch.fnmatchcase(k.decode("utf-8"), match)]
        for k in keys:
            yield k


class VanishingRedis(FakeRedis):
    """模拟 SCAN 已返回、但 HGETALL 之前 key 被清掉的竞态。"""

    def __init__(self):
        super().__init__()
        self._armed = True

    def hgetall(self, name):
        if self._armed:
            self._armed = False
            self._hash.clear()  # 在读取之前消失
        return super().hgetall(name)


# ---------------------------------------------------------------------------
# 确定性假 embedding：3 维词袋，语义相近的句子得到相同/相近向量
# ---------------------------------------------------------------------------
VOCAB = {"退款": 0, "天气": 1, "余额": 2}

def fake_embed(text: str):
    v = np.zeros(len(VOCAB), dtype=np.float32)
    for w, i in VOCAB.items():
        if w in text:
            v[i] = 1.0
    return v

async def fake_embed_async(text: str):
    return fake_embed(text)

PROMPT = "你是知识库助手"
PHASH = prompt_hash(PROMPT)
PHASH_OTHER = prompt_hash("你是客服")


def run(coro):
    return asyncio.run(coro)


class SemanticCacheTest(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        self.cache = SemanticCache(self.r, embed_fn=fake_embed,
                                   threshold=0.90, ttl=3600, kb_version="v1")

    # ---------------------------------------------------------------- 命中
    def test_exact_query_hit(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        sim, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNotNone(ans)
        self.assertGreaterEqual(sim, 0.90)
        self.assertEqual(ans, "7天无理由退款")

    def test_paraphrase_hit(self):
        """换种问法、embedding 相同 -> 必须命中（语义缓存的核心价值）。"""
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        sim, ans = run(self.cache.get("怎么退款", PHASH))
        self.assertIsNotNone(ans)
        self.assertEqual(ans, "7天无理由退款")

    def test_threshold_boundary_hit(self):
        """sim 恰等于 threshold 时算命中（判据是 >=）。相同向量 sim≈1。"""
        cache = SemanticCache(self.r, embed_fn=fake_embed,
                              threshold=0.5, ttl=3600, kb_version="v1")
        run(cache.put("退款和天气", "混合答案", PHASH))
        sim, ans = run(cache.get("退款和天气", PHASH))
        self.assertAlmostEqual(sim, 1.0, places=6)
        self.assertEqual(ans, "混合答案")

    def test_answer_read_from_redis_not_memory(self):
        """反向保险：get 必须真的从 Redis 读，而不是返回进程内缓存的值。
        若实现偷偷加了内存缓存，本用例会失败。"""
        run(self.cache.put("退款政策是什么", "原始答案", PHASH))
        # 直接改底层存储，模拟另一个进程更新了答案
        for k in self.r._hash:
            if k.startswith(b"semcache:v1:"):
                self.r._hash[k][b"answer"] = "被更新的答案".encode("utf-8")
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertEqual(ans, "被更新的答案")

    # ---------------------------------------------------------------- 未命中
    def test_below_threshold_miss(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        sim, ans = run(self.cache.get("今天天气怎么样", PHASH))
        self.assertIsNone(ans)
        self.assertLess(sim, 0.90)

    def test_partial_overlap_below_default_threshold(self):
        """部分重叠（sim≈0.707）在默认 0.90 阈值下不得命中。"""
        run(self.cache.put("退款和天气", "混合答案", PHASH))
        sim, ans = run(self.cache.get("退款", PHASH))
        self.assertIsNone(ans)
        self.assertGreater(sim, 0.5)   # 确实有语义关联
        self.assertLess(sim, 0.90)     # 但不够近

    def test_prompt_hash_mismatch_miss(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        _, ans = run(self.cache.get("退款政策是什么", PHASH_OTHER))
        self.assertIsNone(ans)

    def test_kb_version_mismatch_miss(self):
        """kb_version 不同 -> 即使问题一模一样也不命中（方案 2.6 失效机制）。"""
        run(self.cache.put("退款政策是什么", "v1 答案", PHASH))
        cache_v2 = SemanticCache(self.r, embed_fn=fake_embed,
                                 threshold=0.90, ttl=3600, kb_version="v2")
        sim, ans = run(cache_v2.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)
        self.assertEqual(sim, -1.0)  # v2 下没有任何条目

    def test_ttl_expiry_miss(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        self.r._expire_at[next(iter(self.r._hash))] = time.time() - 1  # 强制已过期
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)

    def test_empty_cache_returns_minus_one(self):
        sim, ans = run(self.cache.get("任意问题", PHASH))
        self.assertEqual(sim, -1.0)
        self.assertIsNone(ans)

    # ---------------------------------------------------------------- 健壮性
    def test_ttl_refresh_on_hit(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        key = next(iter(self.r._hash))
        self.r._expire_at[key] = time.time() + 10
        before = self.r.ttl(key)
        _, ans = run(self.cache.get("怎么退款", PHASH))
        self.assertIsNotNone(ans)
        after = self.r.ttl(key)
        self.assertGreater(after, before)  # 命中后续期

    def test_race_key_disappears_after_scan(self):
        """SCAN 返回后、HGETALL 前 key 被删 -> 不得崩溃，按未命中处理。"""
        self.r = VanishingRedis()
        self.cache = SemanticCache(self.r, embed_fn=fake_embed,
                                   threshold=0.90, ttl=3600, kb_version="v1")
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        sim, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)
        self.assertEqual(sim, -1.0)  # 没有任何可读条目

    def test_entry_missing_field_skipped(self):
        """半截条目（缺 prompt_hash）应被跳过，不得 KeyError。"""
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        key = next(iter(self.r._hash))
        del self.r._hash[key][b"prompt_hash"]
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)

    def test_corrupt_vec_skipped(self):
        """vec 字节数不是 4 的倍数 -> 跳过。"""
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        key = next(iter(self.r._hash))
        self.r._hash[key][b"vec"] = b"\x01\x02\x03"
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)

    def test_dimension_mismatch_skipped(self):
        """embedding 模型更换导致维度不同 -> 跳过，不得崩溃。
        注意：替换向量必须非零 —— 零向量会在 _cos 的 denom<=0 分支提前返回，
        根本走不到 np.dot，用零向量会掩盖维度校验是否生效（反向测试踩过）。"""
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        key = next(iter(self.r._hash))
        self.r._hash[key][b"vec"] = np.full(8, 1.0, dtype=np.float32).tobytes()
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)

    def test_zero_vector_no_nan(self):
        """空串 embedding（零向量）不得产生 nan 泄漏进比较。"""
        cache = SemanticCache(self.r, embed_fn=lambda t: np.zeros(3, dtype=np.float32),
                              threshold=0.90, ttl=3600, kb_version="v1")
        run(cache.put("", "空问题答案", PHASH))
        sim, ans = run(cache.get("", PHASH))
        self.assertIsNone(ans)  # 零向量 cos=0，低于阈值

    # ---------------------------------------------------------------- 统计
    def test_stats_hit_rate(self):
        run(self.cache.put("退款政策是什么", "7天无理由退款", PHASH))
        run(self.cache.get("退款政策是什么", PHASH))   # hit
        run(self.cache.get("怎么退款", PHASH))          # hit
        run(self.cache.get("今天天气怎么样", PHASH))     # miss
        s = self.cache.stats()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["hit"], 2)
        self.assertEqual(s["miss"], 1)
        self.assertAlmostEqual(s["hit_rate"], 0.6667, places=3)

    # ---------------------------------------------------------------- 其他
    def test_async_embed_fn(self):
        cache = SemanticCache(self.r, embed_fn=fake_embed_async,
                              threshold=0.90, ttl=3600, kb_version="v1")
        run(cache.put("余额怎么提现", "绑定银行卡后提现", PHASH))
        sim, ans = run(cache.get("余额怎么提现", PHASH))
        self.assertEqual(ans, "绑定银行卡后提现")

    def test_put_get_roundtrip_vec_bytes(self):
        run(self.cache.put("余额怎么提现", "绑定银行卡后提现", PHASH))
        key = next(iter(self.r._hash))
        vec_bytes = self.r._hash[key][b"vec"]
        self.assertEqual(len(vec_bytes), len(VOCAB) * 4)  # float32 x 3 维
        vec = np.frombuffer(vec_bytes, dtype=np.float32)
        self.assertEqual(vec[2], 1.0)  # "余额" 那一维

    def test_invalidate_all(self):
        run(self.cache.put("退款政策是什么", "a", PHASH))
        run(self.cache.put("怎么退款", "b", PHASH))
        cache_v2 = SemanticCache(self.r, embed_fn=fake_embed,
                                 threshold=0.90, ttl=3600, kb_version="v2")
        run(cache_v2.put("天气", "c", PHASH))
        self.assertEqual(self.cache.invalidate_all(), 2)  # 只删 v1 的
        _, ans = run(self.cache.get("退款政策是什么", PHASH))
        self.assertIsNone(ans)
        _, ans_v2 = run(cache_v2.get("天气", PHASH))
        self.assertIsNotNone(ans_v2)  # v2 不受影响

    def test_invalid_threshold_rejected(self):
        with self.assertRaises(ValueError):
            SemanticCache(self.r, embed_fn=fake_embed, threshold=1.5)
        with self.assertRaises(ValueError):
            SemanticCache(self.r, embed_fn=fake_embed, threshold=0.0)
        with self.assertRaises(ValueError):
            # 1.0 会因浮点误差永远不命中，必须拒绝
            SemanticCache(self.r, embed_fn=fake_embed, threshold=1.0)

    def test_prompt_hash_stable(self):
        self.assertEqual(prompt_hash("abc"), prompt_hash("abc"))
        self.assertNotEqual(prompt_hash("abc"), prompt_hash("abd"))
        self.assertEqual(len(prompt_hash("abc")), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
