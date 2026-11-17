"""相關判斷：每個候選片段各問一次「是否和問題相關」。gate=jev 問 JEV，回傳 noul；gate=llm 問一般 LLM，回傳 1 或 0。

一次只送一組問題 × 一個片段：`state` 是原問題、aspect（沒有就不放這個 key）和片段內容，不送時間標籤。
結果快取在 DB 的 `jev_cache` 表，以（模型, 判準 hash, 原問題, aspect, 片段 id）為鍵；有快取就只讀快取，不重問——
JEV 同樣的輸入結果不固定，而且模型版本會停用，重現實驗靠快取。

`LLMGate`（消融）用同一句問法和判準、同樣的 `state`，只是改問 `service/llm.py` 的模型，structured output 回是／否。
結果和用量快取在 `llm_gate_cache` 表（鍵多了 effort）；讀快取時用量照當初那次呼叫的算，所以重跑時成本數字不變。
"""
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from service.jev import JEVClient
from service.llm import LLMError, Usage, call_detailed

from .store import Content

INSTRUCTIONS = "片段是否和問題相關？不用判斷時間是否符合。"
CRITERIA = {"true": "片段提到和問題相關的經驗。", "false": "片段和問題無關。"}
CRITERIA_HASH = hashlib.sha256(json.dumps({"instructions": INSTRUCTIONS, "criteria": CRITERIA},
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

# aspect 沒有時存空字串；answered_model 是回應裡實際回答的模型版本
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_cache (
    model          TEXT    NOT NULL,
    criteria_hash  TEXT    NOT NULL,
    question       TEXT    NOT NULL,
    aspect         TEXT    NOT NULL,
    content_id     INTEGER NOT NULL,
    noul           REAL    NOT NULL,
    answered_model TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (model, criteria_hash, question, aspect, content_id)
)
"""


class JEVGate:
    """最多同時 concurrency 個請求在等 JEV（同一個 gate 的所有子查詢共用）。"""

    def __init__(self, conn: sqlite3.Connection, client: JEVClient, model: str, concurrency: int):
        self.conn = conn
        self.client = client
        self.model = model
        self.sem = asyncio.Semaphore(concurrency)
        conn.execute(CACHE_SCHEMA)

    async def nouls(self, question: str, aspect: str | None, candidates: list[Content]) -> list[float]:
        """其中一個請求失敗時，取消其他還沒問完的請求，再把錯誤往上丟。"""
        tasks = [asyncio.ensure_future(self._one(question, aspect, c)) for c in candidates]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for t in tasks:
                t.cancel()
            raise

    async def _one(self, question: str, aspect: str | None, c: Content) -> float:
        key = (self.model, CRITERIA_HASH, question, aspect or "", c.id)
        if (cached := self._cached(key)) is not None:
            return cached
        state = {"question": question, **({"aspect": aspect} if aspect else {}), "passage": c.text}
        async with self.sem:
            a = await self.client.noul(self.model, state, INSTRUCTIONS, CRITERIA)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO jev_cache (model, criteria_hash, question, aspect, content_id, noul,"
                " answered_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, a.noul, a.model, datetime.now().astimezone().isoformat(timespec="seconds")))
        # 同一個鍵同時被問兩次時，以先寫進快取的為準，之後重跑才會一致
        cached = self._cached(key)
        return a.noul if cached is None else cached

    def _cached(self, key: tuple) -> float | None:
        row = self.conn.execute(
            "SELECT noul FROM jev_cache WHERE model = ? AND criteria_hash = ? AND question = ? AND aspect = ?"
            " AND content_id = ?", key).fetchone()
        return row[0] if row else None


LLM_SYSTEM = (f"{INSTRUCTIONS}\n\n"
              f"- relevant = true：{CRITERIA['true']}\n"
              f"- relevant = false：{CRITERIA['false']}\n\n"
              "輸入是一個 JSON：question 是旅客的問題，aspect 是這次要找的面向（可能沒有），passage 是片段。")
LLM_SCHEMA = {"type": "object", "properties": {"relevant": {"type": "boolean"}}, "required": ["relevant"],
              "additionalProperties": False}
LLM_CRITERIA_HASH = hashlib.sha256(LLM_SYSTEM.encode()).hexdigest()[:16]

# 一次呼叫等多久算逾時、最多試幾次（CLI 偶爾會掛住不回）
LLM_TIMEOUT = 180
LLM_TRIES = 3

LLM_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_gate_cache (
    model          TEXT    NOT NULL,
    effort         TEXT    NOT NULL,
    criteria_hash  TEXT    NOT NULL,
    question       TEXT    NOT NULL,
    aspect         TEXT    NOT NULL,
    content_id     INTEGER NOT NULL,
    relevant       INTEGER NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_usd       REAL    NOT NULL,
    duration_ms    INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (model, effort, criteria_hash, question, aspect, content_id)
)
"""


@dataclass
class GateUsage:
    """一個 gate 累計的用量。calls 含讀快取的；讀快取的用量照當初那次呼叫的算。"""
    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class LLMGate:
    """跟 `JEVGate` 同一個介面；`nouls` 回傳 1.0（相關）或 0.0。`usage` 累計這個 gate 的用量。"""

    def __init__(self, conn: sqlite3.Connection, model: str, effort: str, concurrency: int, call=None):
        self.conn = conn
        self.model = model
        self.effort = effort
        self.sem = asyncio.Semaphore(concurrency)
        self.call = call or call_detailed  # 測試時換成假的
        self.usage = GateUsage()
        conn.execute(LLM_CACHE_SCHEMA)

    async def nouls(self, question: str, aspect: str | None, candidates: list[Content]) -> list[float]:
        """其中一個請求失敗時，取消其他還沒問完的請求，再把錯誤往上丟。"""
        tasks = [asyncio.ensure_future(self._one(question, aspect, c)) for c in candidates]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for t in tasks:
                t.cancel()
            raise

    async def _one(self, question: str, aspect: str | None, c: Content) -> float:
        key = (self.model, self.effort, LLM_CRITERIA_HASH, question, aspect or "", c.id)
        hit = self._cached(key)
        if hit is None:
            state = {"question": question, **({"aspect": aspect} if aspect else {}), "passage": c.text}
            async with self.sem:
                out, u = await self._ask(json.dumps(state, ensure_ascii=False))
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO llm_gate_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, int(bool(out["relevant"])), u.input_tokens, u.output_tokens, u.cost_usd, u.duration_ms,
                     datetime.now().astimezone().isoformat(timespec="seconds")))
            # 同一個鍵同時被問兩次時，以先寫進快取的為準，之後重跑才會一致
            hit = self._cached(key)
            assert hit is not None
        else:
            self.usage.cached += 1
        relevant, usage = hit
        self.usage.calls += 1
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cost_usd += usage.cost_usd
        self.usage.duration_ms += usage.duration_ms
        return 1.0 if relevant else 0.0

    async def _ask(self, user: str) -> tuple[dict, Usage]:
        for _ in range(LLM_TRIES):
            try:
                return await asyncio.wait_for(
                    self.call(LLM_SYSTEM, user, LLM_SCHEMA, self.model, self.effort), LLM_TIMEOUT)
            except TimeoutError:
                continue
        raise LLMError(f"相關判斷逾時（{LLM_TIMEOUT} 秒，試了 {LLM_TRIES} 次）")

    def _cached(self, key: tuple) -> tuple[bool, Usage] | None:
        row = self.conn.execute(
            "SELECT relevant, input_tokens, output_tokens, cost_usd, duration_ms FROM llm_gate_cache"
            " WHERE model = ? AND effort = ? AND criteria_hash = ? AND question = ? AND aspect = ?"
            " AND content_id = ?", key).fetchone()
        return (bool(row[0]), Usage(*row[1:])) if row else None
