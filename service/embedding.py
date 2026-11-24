"""Embedding 服務：把一批文字轉成向量，照模型名稱選後端。

- `text-embedding-` 開頭：OpenAI 的 `/v1/embeddings`，金鑰從環境變數 `OPENAI_API_KEY` 讀。
- 其他：Ollama 的 `/api/embed`，位址從環境變數 `OLLAMA_URL` 讀，沒有就用 `OLLAMA_URL`（區網 IP 可能會變）。
  `truncate: false`，超過模型長度直接報錯，不默默截斷。

Embedding 的 HTTP 呼叫只在這裡；不快取、不分批、不正規化，那些在呼叫的那一邊（`v4.rag.tools.embed`）。
"""
import os

import requests

OLLAMA_URL = "http://192.168.0.104:11434"
CONNECT_TIMEOUT = 5      # 秒；連不上很快報錯（IP 可能變了）
READ_TIMEOUT = 600       # 秒；本機大模型一批可能算很久


class EmbeddingError(Exception):
    """Embedding 呼叫失敗（沒有金鑰、連不上、HTTP 錯誤、回傳格式不對）。"""


def is_openai(model: str) -> bool:
    return model.startswith("text-embedding-")


def query_text(query: str, model: str, instruct: str) -> str:
    """查詢送去算向量前的樣子：Qwen3-Embedding 要在前面加任務指令，OpenAI 不用；文件都不加。"""
    return query if is_openai(model) else f"Instruct: {instruct}\nQuery:{query}"


def embed(texts: list[str], model: str) -> list[list[float]]:
    """每段文字的向量，順序跟 `texts` 一樣。"""
    return _openai(texts, model) if is_openai(model) else _ollama(texts, model)


def _post(url: str, body: dict, headers: dict | None = None) -> dict:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as e:
        raise EmbeddingError(f"連不上 {url}：{e}") from e
    if r.status_code != 200:
        raise EmbeddingError(f"{url} HTTP {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise EmbeddingError(f"{url} 回的不是 JSON：{r.text[:200]}") from e


def _check(vectors, texts: list[str], url: str) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingError(f"{url} 回傳的向量數不對：送 {len(texts)} 段")
    return vectors


def _openai(texts: list[str], model: str) -> list[list[float]]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise EmbeddingError("沒有 OPENAI_API_KEY（.env）")
    url = "https://api.openai.com/v1/embeddings"
    data = _post(url, {"model": model, "input": texts}, {"Authorization": f"Bearer {key}"})
    return _check([d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d["index"])], texts, url)


def _ollama(texts: list[str], model: str) -> list[list[float]]:
    base = os.environ.get("OLLAMA_URL", OLLAMA_URL).rstrip("/")
    url = f"{base}/api/embed"
    try:
        data = _post(url, {"model": model, "input": texts, "truncate": False})
    except EmbeddingError as e:
        raise EmbeddingError(f"Ollama 呼叫失敗（IP 可能變了，用環境變數 OLLAMA_URL 指定）：{e}") from e
    return _check(data.get("embeddings"), texts, url)
