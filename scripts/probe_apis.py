"""临时探针:验证百炼三个关键接口(chat / embedding / rerank)是否可用。

用纯 urllib 实现,不依赖任何第三方包,避免 shell 编码问题。
验证通过后本文件可删除。
"""

import json
import urllib.request
import urllib.error

API_KEY = "sk-ws-H.PIXRLLI.vXqZ.MEUCIQDhtOgiRuUx6FYIVic4nI6oT76UUaCgwfY6vzHRE6gnIwIgHJyqP73imX4EkwEW2eJI4EdUcwZRwcbfzsIjX9QIq3g"
BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
NATIVE = "https://dashscope.aliyuncs.com/api/v1/services"


def post(url, payload, extra_headers=None):
    """发一个 JSON POST,返回 (status, 解析后的 body 或原始文本)。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # 网络层异常
        return None, f"{type(e).__name__}: {e}"


def show(title, status, body):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    print("HTTP:", status)
    print(json.dumps(body, ensure_ascii=False, indent=2)[:800] if isinstance(body, dict) else body[:800])


# ---------- 1. Embedding(中文 + 批量 + 维度) ----------
st, bd = post(f"{BASE}/embeddings", {
    "model": "qwen3.7-text-embedding",
    "input": ["商品支持七天无理由退货", "这款手机电池容量是5000mAh"],
})
if isinstance(bd, dict) and "data" in bd:
    print("\n[1] Embedding 中文批量: OK")
    print("    返回条数 =", len(bd["data"]))
    print("    向量维度 =", len(bd["data"][0]["embedding"]))
    print("    usage    =", bd.get("usage"))
    dim = len(bd["data"][0]["embedding"])
else:
    show("[1] Embedding 中文批量: 失败", st, bd)
    dim = None

# ---------- 2. Rerank(百炼原生接口) ----------
st, bd = post(f"{NATIVE}/rerank/text-rerank/text-rerank", {
    "model": "qwen3.7-text-rerank",
    "input": {
        "query": "手机电池容量多大",
        "documents": [
            "这款手机电池容量是5000mAh,支持67W快充",
            "商品支持七天无理由退货,运费卖家承担",
            "手机屏幕为6.7英寸AMOLED,刷新率120Hz",
        ],
    },
    "parameters": {"return_documents": True, "top_n": 3},
})
show("[2] Rerank 原生接口", st, bd)

# ---------- 3. 兼容模式下是否也有 rerank ----------
st, bd = post(f"{BASE}/rerank", {
    "model": "qwen3.7-text-rerank",
    "query": "手机电池容量多大",
    "documents": ["电池5000mAh", "七天无理由退货"],
})
show("[3] Rerank 兼容模式尝试", st, bd)

print(f"\n\n>>> 结论: 向量维度 = {dim}")
