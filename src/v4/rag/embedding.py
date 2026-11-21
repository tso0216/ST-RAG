"""Embedding：文字列表進、向量列表出。內容照搬 `src/strag_v3/rag/embedding.py`（v4 不依賴 strag_v3），
另外加了：餘弦相似度用 numpy、記下送了幾段文字給 API（`sent`）、async 的 `aembed`。

所有呼叫都走 `Embedder` 介面，測試時換成假的實作。
`CachedEmbedder` 把結果存在 DB 的 `embedding` 表（第一次用到時才建），以（模型, 文字）為鍵，同樣的組合不會再呼叫 API。
"""
import array
import asyncio
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Protocol

import numpy as np

OPENAI_MODEL = "text-embedding-3-large"
OPENAI_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """OpenAI embeddings API；API key 從環境變數 `OPENAI_API_KEY` 讀取。"""

    def __init__(self, model: str = OPENAI_MODEL, batch_size: int = 128, retries: int = 5):
        self.model = model
        self.batch_size = batch_size
        self.retries = retries

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out += self._request(texts[i:i + self.batch_size])
        return out

    def _request(self, texts: list[str]) -> list[list[float]]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise EmbeddingError("沒有設定環境變數 OPENAI_API_KEY")
        body = json.dumps({"model": self.model, "input": texts}).encode()
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(OPENAI_URL, data=body, headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)["data"]
                return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code != 429 and e.code < 500 or attempt == self.retries:
                    raise EmbeddingError(f"OpenAI HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt == self.retries:
                    raise EmbeddingError(f"OpenAI 連線失敗：{e.reason}") from None
            time.sleep(2 ** attempt)
        raise EmbeddingError("OpenAI 重試失敗")


CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding (
    model  TEXT NOT NULL,
    text   TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (model, text)
)
"""


class CachedEmbedder:
    """包住另一個 Embedder，結果快取在 SQLite（float32）。sent 是這個物件送給 API 的文字段數。"""

    def __init__(self, inner: Embedder, conn: sqlite3.Connection):
        self.inner = inner
        self.model = inner.model
        self.conn = conn
        self.sent = 0
        conn.execute(CACHE_SCHEMA)

    def cached(self, texts: list[str]) -> dict[str, list[float]]:
        """已經在快取裡的那些：文字 → 向量。"""
        found: dict[str, list[float]] = {}
        for t in set(texts):
            row = self.conn.execute(
                "SELECT vector FROM embedding WHERE model = ? AND text = ?", (self.model, t)).fetchone()
            if row:
                found[t] = array.array("f", row[0]).tolist()
        return found

    def _store(self, texts: list[str], vectors: list[list[float]], found: dict[str, list[float]]) -> None:
        with self.conn:
            for t, v in zip(texts, vectors):
                packed = array.array("f", v)  # 第一次和之後讀快取都回傳同樣的 float32 值
                self.conn.execute("INSERT OR REPLACE INTO embedding (model, text, vector) VALUES (?, ?, ?)",
                                  (self.model, t, packed.tobytes()))
                found[t] = packed.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        found = self.cached(texts)
        missing = sorted(set(texts) - found.keys())
        if missing:
            self.sent += len(missing)
            self._store(missing, self.inner.embed(missing), found)
        return [found[t] for t in texts]

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """同 `embed`；呼叫 API 的那段在另一個 thread 跑，讀寫快取在原本的 thread（SQLite 連線不能跨 thread）。"""
        found = self.cached(texts)
        missing = sorted(set(texts) - found.keys())
        if missing:
            self.sent += len(missing)
            vectors = await asyncio.to_thread(self.inner.embed, missing)
            self._store(missing, vectors, found)
        return [found[t] for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    return float(va @ vb / (na * nb)) if na and nb else 0.0


def cosines(query: list[float], vectors: list[list[float]]) -> list[float]:
    """query 跟每個向量的餘弦相似度。"""
    if not vectors:
        return []
    q = np.asarray(query, dtype=float)
    m = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(m, axis=1) * np.linalg.norm(q)
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(norms > 0, m @ q / np.where(norms > 0, norms, 1), 0.0)
    return [float(x) for x in sims]
