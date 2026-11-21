"""相關判斷（規格 12.2 第 4 步）：每筆經驗問 JEV 一次「跟問題相不相關」，讀寫 `jev_cache`，限制同時請求數。

問法、判準照 v3（`strag_v3.rag.relevance`）。`state` 是問句、節點的面向和經驗本身，不送時間。
結果快取在 `jev_cache`（第一次用到時才建），以（模型, 判準雜湊, 問題, 面向, 經驗所在的表, 經驗 id）為鍵；
有快取就只讀快取，不重問（JEV 同樣的輸入結果不固定，重現靠快取）。三張表的 id 會重複，所以鍵裡有表名。
"""
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from service.jev import JEVClient

INSTRUCTIONS = "片段是否和問題相關？不用判斷時間是否符合。"
CRITERIA = {"true": "片段提到和問題相關的經驗。", "false": "片段和問題無關。"}
CRITERIA_HASH = hashlib.sha256(json.dumps({"instructions": INSTRUCTIONS, "criteria": CRITERIA},
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

# answered_model 是回應裡實際回答的模型版本
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_cache (
    model          TEXT NOT NULL,
    criteria_hash  TEXT NOT NULL,
    question       TEXT NOT NULL,
    aspect         TEXT NOT NULL,
    source_table   TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    noul           REAL NOT NULL,
    answered_model TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (model, criteria_hash, question, aspect, source_table, item_id)
)
"""


@dataclass(frozen=True)
class Item:
    """送相關判斷的一筆經驗。text 是送出的內容（點：`poi_name：content`；段：`from → to：content`；day_note：content）。"""
    table: str
    id: str
    text: str


class Gate:
    """最多同時 concurrency 個請求在等 JEV（同一題的所有節點共用一個 gate）。
    asked、cached 是問了 JEV 幾次、讀快取幾次。"""

    def __init__(self, conn: sqlite3.Connection, client: JEVClient, model: str, concurrency: int):
        self.conn = conn
        self.client = client
        self.model = model
        self.sem = asyncio.Semaphore(concurrency)
        self.asked = 0
        self.cached = 0
        conn.execute(CACHE_SCHEMA)

    async def nouls(self, question: str, aspect: str, items: list[Item]) -> list[float]:
        """每一筆的機率。其中一個請求失敗時，取消其他還沒問完的請求，再把錯誤往上丟（`JEVError`）。"""
        tasks = [asyncio.ensure_future(self._one(question, aspect, item)) for item in items]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for t in tasks:
                t.cancel()
            raise

    async def _one(self, question: str, aspect: str, item: Item) -> float:
        key = (self.model, CRITERIA_HASH, question, aspect or "", item.table, item.id)
        if (hit := self._read(key)) is not None:
            self.cached += 1
            return hit
        state = {"question": question, **({"aspect": aspect} if aspect else {}), "passage": item.text}
        async with self.sem:
            # 等的時候可能別的節點已經問過同一筆
            if (hit := self._read(key)) is not None:
                self.cached += 1
                return hit
            self.asked += 1
            a = await self.client.noul(self.model, state, INSTRUCTIONS, CRITERIA)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO jev_cache (model, criteria_hash, question, aspect, source_table, item_id,"
                " noul, answered_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, a.noul, a.model, datetime.now().astimezone().isoformat(timespec="seconds")))
        # 同一個鍵同時被問兩次時，以先寫進快取的為準，之後重跑才會一致
        hit = self._read(key)
        return a.noul if hit is None else hit

    def _read(self, key: tuple) -> float | None:
        row = self.conn.execute(
            "SELECT noul FROM jev_cache WHERE model = ? AND criteria_hash = ? AND question = ? AND aspect = ?"
            " AND source_table = ? AND item_id = ?", key).fetchone()
        return row[0] if row else None
