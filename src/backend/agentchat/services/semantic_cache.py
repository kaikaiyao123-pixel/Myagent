"""
语义缓存（Semantic Cache）—— 核心类 + 后端服务层

本文件是唯一实现（single source of truth）。方案原稿写在 scripts/ 下，
但生产镜像只打包 src/backend（见 docker/Dockerfile.prod 的 COPY），
后端 import scripts/ 目录在容器内不可用，因此核心类放包内；
scripts/test_semantic_cache.py 与 scripts/calibrate_threshold.py
通过 sys.path 注入 src/backend 复用本实现。

定位（来自《稳健型三件套实施方案》2.1）：
    只服务「单轮、自包含、答案可复现」的场景 —— RAG 知识库问答、高频 FAQ。
    多轮个性化对话、实时数据查询不要走本缓存。

命中判据（三道门槛，缺一不可，见方案 2.3）：
    1. 语义相似度 >= threshold（余弦，embedding 必须复用 RAG 检索同一个模型）
    2. kb_version 一致（知识库版本，版本不同直接判未命中 —— 旧版本答案不串味）
    3. prompt_hash 一致（系统提示词模板哈希，防止不同模板下的答案互相污染）

存储：复用 Redis Hash，向量以 float32.tobytes() 落库，TTL 原生过期。
     条目量小（百~千级）时 SCAN + 暴力余弦足够；上万条目再考虑 ANN（见方案 2.2）。

相对方案 2.4 骨架的修正（均为实测会踩的坑）：
    1. SCAN 与 HGETALL 之间 key 可能已过期 -> hgetall 返回空 dict，必须跳过而非崩溃
    2. 旧版本/半截条目可能缺字段 -> 用 .get() 判空跳过，不允许 KeyError
    3. vec 字节数不是 4 的倍数、或维度与当前 query 不一致 -> 跳过该条目
    4. 命中后刷新 TTL（热门问题从最后一次命中起算），否则早写入的热问题会被提前清掉
    5. 全局统计 total/hit/miss 落到独立 stats key，供评测 Harness 读命中率；
       另外 cid 用 sha256（md5 在 FIPS 模式下不可用）

对 Redis 客户端的最低接口要求（duck-typed，真 redis.StrictRedis 与测试桩均可）：
    scan_iter(match=...) / hgetall(name) / hset(name, mapping=...) /
    expire(name, ttl) / hincrby(name, key, amount=1) / delete(*names) / ttl(name)
注意：redis-py 默认 bytes 编解码，本模块同时兼容 decode_responses=True 的客户端。
"""
import hashlib
import inspect
import json
import time

import numpy as np

__all__ = ["SemanticCache", "prompt_hash"]

# SCAN 每批游标数量：够小以避免阻塞，够大以减少往返
_SCAN_COUNT = 200
# 批量删除的分片大小，避免单条 DEL 命令过长
_DELETE_BATCH = 100
# stats key 的 TTL：7 天，保证长期采样窗口内不丢
_STATS_TTL = 7 * 86400


def prompt_hash(text: str) -> str:
    """系统提示词模板的内容哈希（方案 2.3 第 3 道门槛）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class SemanticCache:
    def __init__(self, r, embed_fn, prefix: str = "semcache:",
                 threshold: float = 0.90, ttl: int = 86400,
                 kb_version: str = "v1"):
        """
        参数：
            r          : Redis 客户端（接口见模块 docstring）
            embed_fn   : 文本 -> 向量的函数，同步或 async 均可；
                         必须复用 RAG 检索使用的同一个 embedding 模型
            prefix     : key 前缀，默认 "semcache:"
            threshold  : 余弦相似度阈值，必须在校准集上标定后填写（见方案 8）
            ttl        : 条目 TTL（秒），命中后刷新
            kb_version : 知识库版本，KB 更新后由调用方递增（方案 2.6）
        """
        if not 0.0 < threshold < 1.0:
            # 1.0 看似「只信完全相同的问题」，但浮点误差会使相同向量的相似度
            # 永远差一点点小于 1.0 -> 缓存永远不命中。配置层就要拦住这个陷阱。
            raise ValueError(
                f"threshold 必须在 (0, 1) 开区间，收到 {threshold!r}；"
                "1.0 会因浮点误差导致永远不命中，语义相同请用 0.99 以下")
        self.r = r
        self.embed_fn = embed_fn
        self.prefix = prefix
        self.threshold = float(threshold)
        self.ttl = int(ttl)
        self.kb_version = kb_version

    # ------------------------------------------------------------------ 内部
    def _entry_prefix(self) -> str:
        """条目 key 前缀带 kb_version —— 版本一变，旧条目即逻辑失效（方案 2.6）。"""
        return f"{self.prefix}{self.kb_version}:"

    def _stats_key(self) -> str:
        return f"{self.prefix}stats:{self.kb_version}"

    async def _embed(self, text: str) -> np.ndarray:
        vec = self.embed_fn(text)
        if inspect.isawaitable(vec):
            vec = await vec
        return np.asarray(vec, dtype=np.float32).ravel()

    @staticmethod
    def _cos(a: np.ndarray, b: np.ndarray) -> float:
        # 必须用 float64 累加：float32 下相同向量的相似度会算出 1.0000001
        # （NumPy 2.x 的 NEP 50 弱标量规则会让 np.float32 / float 退回 float32 运算），
        # 导致 >= 边界判断反直觉。
        a64, b64 = a.astype(np.float64), b.astype(np.float64)
        denom = float(np.linalg.norm(a64)) * float(np.linalg.norm(b64))
        if denom <= 0.0:
            return 0.0  # 零向量视为不相似，不让 nan 泄漏进比较
        sim = float(np.dot(a64, b64) / denom)
        return min(1.0, max(-1.0, sim))  # 夹住浮点噪声，理论上余弦不超过 1

    @staticmethod
    def _field(raw: dict, name: str):
        """hgetall 取字段，同时兼容 bytes / str 两种编解码模式。"""
        v = raw.get(name.encode("utf-8"))
        if v is None:
            v = raw.get(name)
        return v

    def _stat_incr(self, field: str, amount: int = 1):
        try:
            self.r.hincrby(self._stats_key(), field, amount)
            self.r.expire(self._stats_key(), _STATS_TTL)
        except Exception:
            pass  # 统计失败绝不能影响主链路

    # ------------------------------------------------------------------ 查询
    async def get(self, query: str, prompt_hash: str):
        """
        查缓存。返回 (相似度, 答案|None)：
            命中 -> (sim, answer)，sim >= threshold
            未命中 -> (best_sim, None)，best_sim 为本次扫描中的最高相似度
                      （无任何同版本条目时为 -1.0，可供阈值校准采样）
        """
        want_hash = prompt_hash.encode("utf-8")
        qvec = await self._embed(query)
        self._stat_incr("total")

        best, best_key, best_ans = -1.0, None, None
        for key in self.r.scan_iter(match=f"{self._entry_prefix()}*", count=_SCAN_COUNT):
            raw = self.r.hgetall(key)
            if not raw:
                # key 在 SCAN 与 HGETALL 之间过期（或已被删）—— 跳过
                continue
            if self._field(raw, "prompt_hash") != want_hash:
                continue  # 第 3 道门槛：模板不一致，答案可能串味
            vec_bytes = self._field(raw, "vec")
            if not vec_bytes or len(vec_bytes) % 4 != 0:
                continue  # 残缺条目
            vec = np.frombuffer(vec_bytes, dtype=np.float32)
            if vec.size != qvec.size:
                continue  # embedding 模型换过，向量维度不同，不可比
            sim = self._cos(qvec, vec)
            if sim > best:
                ans = self._field(raw, "answer")
                if ans is None:
                    continue
                best, best_key, best_ans = sim, key, ans

        if best_ans is not None and best >= self.threshold:
            # 命中：刷新 TTL（热门问题从最后命中起算）+ 累加条目命中数
            try:
                self.r.expire(best_key, self.ttl)
                self.r.hincrby(best_key, "hits", 1)
            except Exception:
                pass  # 续期/计数失败不影响返回，最坏情况是提前过期
            self._stat_incr("hit")
            return best, best_ans.decode("utf-8") if isinstance(best_ans, bytes) else str(best_ans)
        self._stat_incr("miss")
        return best, None

    # ------------------------------------------------------------------ 写入
    async def put(self, query: str, answer: str, prompt_hash: str, meta: dict = None):
        """写入缓存（未命中真实链路后由调用方回填）。"""
        qvec = await self._embed(query)
        # cid 用 sha256：FIPS 模式下 hashlib.md5 不可用
        cid = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        key = f"{self._entry_prefix()}{cid}"
        self.r.hset(key, mapping={
            "vec": qvec.tobytes(),
            "answer": answer,
            "prompt_hash": prompt_hash,
            "meta": json.dumps(meta or {}, ensure_ascii=False),
            "ts": int(time.time()),
            "hits": 0,
        })
        self.r.expire(key, self.ttl)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict:
        """读取当前 kb_version 的累计命中率（评测 Harness / 运营口径）。"""
        try:
            raw = self.r.hgetall(self._stats_key())
        except Exception:
            return {"total": 0, "hit": 0, "miss": 0, "hit_rate": 0.0}

        def num(name):
            v = self._field(raw, name)
            return int(v) if v is not None else 0

        total, hit = num("total"), num("hit")
        return {
            "total": total,
            "hit": hit,
            "miss": num("miss"),
            "hit_rate": round(hit / total, 4) if total else 0.0,
        }

    # ------------------------------------------------------------------ 失效
    def invalidate_all(self) -> int:
        """
        物理删除当前 kb_version 的全部条目（KB 重建索引后的兜底清理）。
        常规失效不需要调它 —— kb_version 递增即逻辑失效（方案 2.6）。
        """
        keys = list(self.r.scan_iter(match=f"{self._entry_prefix()}*", count=_SCAN_COUNT))
        deleted = 0
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i:i + _DELETE_BATCH]
            deleted += self.r.delete(*batch) or 0
        return deleted


# ===========================================================================
# 服务层：配置读取 + 单例 + 接入门控
# ===========================================================================
from loguru import logger

from agentchat.settings import app_settings

_cache_singleton = None
_cache_init_done = False


def cache_eligible(*, file_url, enable_memory: bool, has_short_history: bool) -> bool:
    """
    接入门控（方案 2.1 的适用边界，任一条不满足就走真实链路）：
      1. 无附件        —— 带附件的问答答案依赖附件内容，不可跨请求复用
      2. 未开长期记忆  —— 开了记忆就是多轮个性化场景，缓存会让答案过期
      3. 会话首轮      —— 有短期历史即多轮对话，答案依赖上下文
    纯函数，无 IO，可单测。
    """
    if file_url:
        return False
    if enable_memory:
        return False
    if has_short_history:
        return False
    return True


def current_kb_version() -> str:
    """当前知识库版本（Langfuse 埋点 metadata 用；未配置返回 unknown）。"""
    cfg = getattr(app_settings, "semantic_cache", None) or {}
    return str(cfg.get("kb_version", "unknown"))


def get_semantic_cache(*, embed_fn=None, redis_conn=None):
    """
    按配置返回语义缓存单例；未启用 / 配置缺失 / Redis 不可用一律返回 None。
    失败只降级不抛异常 —— 缓存是优化项，不能拖垮主链路。

    embed_fn / redis_conn 是测试注入 seam：生产调用不传（走默认的 RAG
    embedding + 全局 redis_client）；单测传假件，避免拖入 langchain 全链依赖。
    """
    global _cache_singleton, _cache_init_done
    if _cache_init_done:
        return _cache_singleton
    _cache_init_done = True
    _cache_singleton = None

    cfg = getattr(app_settings, "semantic_cache", None)
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None

    try:
        # 惰性导入：rag.embedding 会拉起 ModelManager 等重依赖，
        # 缓存未启用时不应付出这个 import 成本
        if embed_fn is None:
            from agentchat.services.rag.embedding import get_embedding as embed_fn
        if redis_conn is None:
            from agentchat.services.redis import redis_client
            redis_conn = redis_client.connection
        threshold = float(cfg.get("threshold", 0.90))
        ttl = int(cfg.get("ttl", 86400))
        kb_version = str(cfg.get("kb_version", "v1"))
        # embed_fn 必须复用 RAG 检索同一个 embedding 模型（方案 2.4）
        _cache_singleton = SemanticCache(
            redis_conn,
            embed_fn=embed_fn,
            threshold=threshold,
            ttl=ttl,
            kb_version=kb_version,
        )
        logger.info(
            f"语义缓存已启用：threshold={threshold} ttl={ttl}s kb_version={kb_version}")
    except Exception as e:
        logger.error(f"语义缓存初始化失败，本进程降级为不缓存：{e}")
        _cache_singleton = None
    return _cache_singleton
