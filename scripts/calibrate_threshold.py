"""
语义缓存阈值校准工具（方案 2.3 / 第 8 章：threshold 必须在校准集上调，不能抄 0.90）

原理：
    准备「同义问法对」（same=true）与「不同问题对」（same=false），
    用 RAG 检索同一个 embedding 模型算余弦相似度：
      - same 组低了 -> 阈值过高，缓存形同虚设
      - diff 组高了 -> 阈值过低，答非所问
    建议阈值取 same 组 P05 与 diff 组 P95 的安全区间中值；
    若两组分布重叠，退而求其次：扫阈值取 F1 最大点，并明确警告分离度不足。

用法：
    # 离线演示（确定性假 embedding，验证流程）
    python scripts/calibrate_threshold.py --mock

    # 真实校准：embedding 必须复用 RAG 检索同一个模型（方案 2.4）
    python scripts/calibrate_threshold.py \
        --pairs my_pairs.jsonl \
        --embed-endpoint https://dashscope.aliyuncs.com/compatible-mode/v1 \
        --embed-model text-embedding-v4 \
        --api-key $DASHSCOPE_API_KEY

pairs.jsonl 每行一条：
    {"a": "退款政策是什么", "b": "怎么退款", "same": true}
    {"a": "退款政策是什么", "b": "今天天气怎么样", "same": false}
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# embedding 提供方
# ---------------------------------------------------------------------------
def openai_embed(endpoint: str, model: str, api_key: str, texts: list[str]) -> np.ndarray:
    """OpenAI 兼容 /embeddings 接口（DashScope compatible-mode 即此格式）。"""
    import requests
    r = requests.post(
        f"{endpoint.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": texts, "encoding_format": "float"},
        timeout=60,
    )
    r.raise_for_status()
    vecs = [d["embedding"] for d in r.json()["data"]]
    return np.asarray(vecs, dtype=np.float64)


def _mock_vocab():
    return {
        "退款": 0, "退钱": 0, "退货": 1, "包邮": 2, "运费": 2,
        "天气": 3, "下雨": 3, "股价": 4, "股票": 4, "发票": 5,
    }


def mock_embed(texts: list[str]) -> np.ndarray:
    """确定性词袋假 embedding：仅用于离线演示流程。"""
    vocab = _mock_vocab()
    mat = np.zeros((len(texts), len(set(vocab.values()))), dtype=np.float64)
    for i, t in enumerate(texts):
        for w, idx in vocab.items():
            if w in t:
                mat[i, idx] = 1.0
        norm = np.linalg.norm(mat[i])
        if norm > 0:
            mat[i] /= norm
    return mat


MOCK_PAIRS = [
    {"a": "退款政策是什么", "b": "怎么退钱", "same": True},
    {"a": "退货流程怎么走", "b": "退货要怎么操作", "same": True},
    {"a": "买东西要运费吗", "b": "可以包邮吗", "same": True},
    {"a": "可以开发票吗", "b": "能开增值税发票吗", "same": True},
    {"a": "退款政策是什么", "b": "今天天气怎么样", "same": False},
    {"a": "退货流程怎么走", "b": "现在股价多少", "same": False},
    {"a": "买东西要运费吗", "b": "明天会下雨吗", "same": False},
    {"a": "可以开发票吗", "b": "推荐几只股票", "same": False},
]


# ---------------------------------------------------------------------------
# 统计与建议
# ---------------------------------------------------------------------------
def cosine_matrix(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = mat / norms
    return normed @ normed.T


def suggest_threshold(same_scores: list[float], diff_scores: list[float]) -> dict:
    same_arr = np.asarray(same_scores, dtype=np.float64)
    diff_arr = np.asarray(diff_scores, dtype=np.float64)

    same_p05 = float(np.percentile(same_arr, 5))
    diff_p95 = float(np.percentile(diff_arr, 95))

    result = {
        "same_p05": round(same_p05, 4),
        "diff_p95": round(diff_p95, 4),
        "same_min": round(float(same_arr.min()), 4),
        "diff_max": round(float(diff_arr.max()), 4),
        "n_same": len(same_scores),
        "n_diff": len(diff_scores),
    }

    if same_p05 > diff_p95:
        # 两组完全分离：取安全区间中值
        suggestion = (same_p05 + diff_p95) / 2
        result["mode"] = "separable"
        result["suggestion"] = round(min(0.99, max(0.05, suggestion)), 4)
        result["warning"] = None
    else:
        # 分布重叠：扫阈值最大化 F1
        best_f1, best_t = 0.0, None
        for t in np.arange(0.50, 1.0, 0.01):
            tp = int((same_arr >= t).sum())
            fp = int((diff_arr >= t).sum())
            fn = int((same_arr < t).sum())
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            if f1 > best_f1:
                best_f1, best_t = f1, float(t)
        result["mode"] = "overlap_f1"
        result["suggestion"] = round(best_t, 4) if best_t else 0.90
        result["best_f1"] = round(best_f1, 4)
        result["warning"] = (
            "同义组与异义组分布重叠，该 embedding 模型无法完全区分；"
            "建议扩充校准集或更换 embedding 模型，不要直接把阈值当银弹。")
    return result


def load_pairs(path: str) -> list[dict]:
    pairs = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"pairs 第 {ln} 行不是合法 JSON：{e}")
            if not (isinstance(p.get("a"), str) and isinstance(p.get("b"), str)):
                raise SystemExit(f"pairs 第 {ln} 行缺少 a/b 字符串字段")
            if not isinstance(p.get("same"), bool):
                raise SystemExit(f"pairs 第 {ln} 行 same 必须是布尔值")
            pairs.append(p)
    if len(pairs) < 8:
        raise SystemExit(f"pairs 至少需要 8 对（当前 {len(pairs)}），否则统计没有意义")
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="语义缓存阈值校准")
    ap.add_argument("--pairs", help="同义/异义问法对 JSONL")
    ap.add_argument("--mock", action="store_true", help="用内置假 embedding 离线演示")
    ap.add_argument("--embed-endpoint", default=os.environ.get("EMBED_ENDPOINT", ""))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "text-embedding-v4"))
    ap.add_argument("--api-key", default=os.environ.get("DASHSCOPE_API_KEY", ""))
    ap.add_argument("--out", help="把建议结果写到该 JSON 文件")
    args = ap.parse_args()

    if args.mock:
        pairs = MOCK_PAIRS
        embed_fn = mock_embed
    else:
        if not args.pairs:
            ap.error("需要 --pairs（或 --mock）")
        if not (args.embed_endpoint and args.api_key):
            ap.error("真实校准需要 --embed-endpoint 与 --api-key"
                     "（或环境变量 EMBED_ENDPOINT / DASHSCOPE_API_KEY）")
        pairs = load_pairs(args.pairs)

        def embed_fn(texts, _ep=args.embed_endpoint, _m=args.embed_model,
                     _k=args.api_key):
            return openai_embed(_ep, _m, _k, texts)

    texts = []
    for p in pairs:
        texts.extend([p["a"], p["b"]])
    mat = embed_fn(texts)
    sims = cosine_matrix(mat)

    same_scores, diff_scores = [], []
    for i, p in enumerate(pairs):
        s = float(sims[2 * i, 2 * i + 1])
        (same_scores if p["same"] else diff_scores).append(s)

    result = suggest_threshold(same_scores, diff_scores)
    result["same_scores"] = [round(s, 4) for s in sorted(same_scores)]
    result["diff_scores"] = [round(s, 4) for s in sorted(diff_scores)]
    result["mock"] = bool(args.mock)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["warning"]:
        print(f"\n[警告] {result['warning']}")
    print(f"\n建议：semantic_cache.threshold = {result['suggestion']}"
          "（写入 docker_config.prod.template.yaml / .env 的 SEMANTIC_CACHE_THRESHOLD）")
    if args.mock:
        print("（本次为 --mock 演示，阈值不具业务意义；请用真实 pairs 校准）")

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
