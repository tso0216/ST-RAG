"""一篇發問文的主流程（規格 7、16）：

    ① 拆題與解析（整篇一次）→ 每一題照順序：② 分流 → ③ 初始計畫 → ④ 驗證 → ⑤ 執行 → ⑥ 查詢狀態表
    →（⑦ 補規劃 → ④ → ⑤ → ⑥，最多 2 輪；⑦ 輸出空的就停）→ ⑧ 合成
    最後照順序合併各題的答案，組成紀錄（18）。

16.2 的錯誤（LLM、JEV、OpenAI embedding、國土地理院、行程解析檢查不過）發生時這篇停下，不改用別的方法補：
`run` 不丟出去，回傳的 `Result.error` 記著錯誤，紀錄裡留著已經做完的部分。本機地圖不存在由呼叫的人一開始就檢查。

模組的方向：這裡呼叫其他模組，其他模組不呼叫這裡。
"""
import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from service import llm
from service.gsi import GsiClient, GsiError
from service.jev import JEVClient, JEVError
from v4.extract import config as extract_config
from v4.extract.data_schema import Article
from v4.extract.geocoding import Geocoder
from v4.extract.local_map import LocalMap
from v4.extract.parse_line import ParseError

from . import answer, config, parse, plan, replan, route, run, status, trace
from .data_schema import ROUTE_FIXED, ROUTE_GENERAL, Batch, Question, QState
from .embedding import CachedEmbedder, Embedder, EmbeddingError
from .gate import Gate
from .nodes import Env
from .validate import Ctx, Resolver

log = logging.getLogger(__name__)

ERRORS = (llm.LLMError, JEVError, EmbeddingError, GsiError, ParseError)

STOP_EMPTY = "補規劃輸出空的（查完了）"
STOP_ROUNDS = f"補規劃滿 {config.REPLAN_ROUNDS} 輪"
STOP_NODES = f"整題的節點到 {config.MAX_NODES} 個"


@dataclass
class Services:
    """外部的東西：資料庫、本機地圖、國土地理院、JEV、embedding（測試換成假的）。"""
    conn: sqlite3.Connection
    local_map: LocalMap
    jev: JEVClient
    embedder: Embedder
    gsi: GsiClient | None = None


@dataclass
class Result:
    answer: str                    # 合併好的答案（Markdown）；出錯時是空的
    record: dict = field(default_factory=dict)
    error: str | None = None


def params() -> dict:
    return {
        "模型": config.MODEL, "effort": config.EFFORT,
        "整理重點的模型": config.SUMMARIZE_MODEL, "整理重點的 effort": config.SUMMARIZE_EFFORT,
        "行程解析、地點解析的模型": extract_config.MODEL, "行程解析、地點解析的 effort": extract_config.EFFORT,
        "行程解析的重問次數": extract_config.RETRIES, "地點解析的輪數": extract_config.GEOCODE_ROUNDS,
        "本機地圖": str(extract_config.LOCAL_MAP_PATH),
        "檢查不過時重問的次數": config.RETRIES, "③⑦ 格式不合法時重問的次數": config.PLAN_RETRIES,
        "JEV 模型": config.JEV_MODEL, "JEV 門檻": config.JEV_THRESHOLD, "JEV 同時請求數": config.JEV_CONCURRENCY,
        "LLM 同時請求數": config.LLM_CONCURRENCY, "embedding 模型": config.EMBEDDING_MODEL,
        "embedding 先取幾筆": config.EMBED_TOP, "天節點最多挑幾天": config.DAY_TOP,
        "補規劃最多幾輪": config.REPLAN_ROUNDS, "每輪最多加幾個節點": config.REPLAN_NODES,
        "一題最多幾個節點": config.MAX_NODES,
    }


def input_record(article: Article, question_text: str | None, db_path: Path | str) -> dict:
    if question_text is not None:
        source = {"問題文字": question_text, "文章 id": article.id, "發文時間": article.posted_at}
    else:
        source = {"文章 id": article.id, "標題": article.title, "發文時間": article.posted_at, "主文": article.text}
    return {**source, "資料庫": str(db_path)}


async def run_article(article: Article, services: Services, *, db_path: Path | str,
                      question_text: str | None = None) -> Result:
    """回答一篇發問文。出錯時不丟出去，記在 `Result.error` 和紀錄裡。"""
    record: dict = {"輸入": input_record(article, question_text, db_path), "參數": params()}
    calls = trace.Calls()
    gate = Gate(services.conn, services.jev, config.JEV_MODEL, config.JEV_CONCURRENCY)
    embedder = CachedEmbedder(services.embedder, services.conn)
    parts: list[tuple[Question, str]] = []
    error = None
    t0 = time.monotonic()
    try:
        with llm.track(calls):
            await _run(article, services, gate, embedder, record, parts)
    except ERRORS as e:
        error = f"{type(e).__name__}: {e}"
        record["錯誤"] = error
        log.info("出錯，這篇停下：%s", error)
    finally:
        record["用量"] = {"LLM": calls.summary(), "JEV": {"問了幾次": gate.asked, "讀快取幾次": gate.cached},
                        "embedding": {"送了幾段文字": embedder.sent},
                        "時間（秒）": round(time.monotonic() - t0, 1)}
    text = "" if error else answer.merge(parts)
    if not error:
        record["答案"] = text
    return Result(text, record, error)


async def _run(article: Article, services: Services, gate: Gate, embedder: CachedEmbedder, record: dict,
               parts: list[tuple[Question, str]]) -> None:
    first: dict = {}
    record["拆題與解析"] = first
    split_record: dict = {}
    first["拆題"] = split_record
    questions = await parse.split(article, split_record)
    geocoder = Geocoder(services.conn, services.local_map, services.gsi)
    if parse.needs_itinerary(questions):
        line_record: dict = {"做了嗎": True}
        first["行程解析"] = line_record
        itinerary = await parse.itinerary(article, geocoder, line_record)
    else:
        first["行程解析"] = {"做了嗎": False, "原因": "拆出的題目都是其他、無法處理"}
        itinerary = parse.Itinerary()
        log.info("題目都是其他、無法處理，不解析行程和站")

    places = {p.ref: p for p in itinerary.places}
    resolver = Resolver(geocoder, article.id, places, itinerary.resolved)
    llm_sem = asyncio.Semaphore(config.LLM_CONCURRENCY)
    posted_year = int(article.posted_at[:4])
    record["每一題"] = []
    for q in questions:
        q_record: dict = {"第幾題": q.no, "問句": q.text, "題型": q.qtype}
        record["每一題"].append(q_record)
        ctx = Ctx(places, itinerary.days, posted_year, q.text, resolver)
        env = Env(services.conn, gate, embedder, llm_sem, q.text)
        text = await question(article, q, ctx, env, q_record)
        parts.append((q, text))
    first["節點裡的地點解析"] = resolver.log


async def question(article: Article, q: Question, ctx: Ctx, env: Env, q_record: dict) -> str:
    """一題：② →（③ → ④ → ⑤ → ⑥）→（⑦ → ④ → ⑤ → ⑥）* → ⑧。回傳這一題的答案。"""
    path = route.route(q.qtype)
    q_record["走哪一條路"] = path
    log.info("第 %d 題 [%s] %s：%s", q.no, q.qtype, q.text, path)
    if path == ROUTE_FIXED:
        q_record["答案"] = route.fixed_answer()
        return q_record["答案"]

    state = QState()
    rounds: list[dict] = []
    q_record["每一輪"] = rounds

    batch = await plan.initial(article, q, ctx)
    status.add_batch(state, batch, initial=True)
    await _round(batch, env, state, rounds, 0)
    while True:
        if not status.should_replan(state):
            stop = STOP_ROUNDS if state.replans >= config.REPLAN_ROUNDS else STOP_NODES
            break
        batch = await replan.replan(article, q, ctx, state)
        state.replans += 1
        status.add_batch(state, batch, initial=False)
        if batch.empty:
            rounds.append({"第幾輪": state.replans, "計畫": batch.record})
            stop = STOP_EMPTY
            break
        await _round(batch, env, state, rounds, state.replans)
    q_record["為什麼停"] = stop
    log.info("第 %d 題：%s，共 %d 個節點", q.no, stop, len(state.nodes))

    text, answer_record = await answer.answer(q, ctx, state, stop, general=path == ROUTE_GENERAL)
    q_record["合成"] = answer_record
    q_record["答案"] = text
    return text


async def _round(batch: Batch, env: Env, state: QState, rounds: list[dict], round_no: int) -> None:
    """⑤ 跑這一輪通過的節點、⑥ 重建查詢狀態表，記進紀錄。"""
    r: dict = {"第幾輪": round_no, "計畫": batch.record}
    rounds.append(r)
    await run.run_round(batch.accepted, env, state)
    r["節點"] = [{"節點": trace.node_record(n), "結果": trace.result_record(state.results[n.no])}
                for n in batch.accepted]
    r["排法的計算（這一輪結束時）"] = {n.no: res.calc for n, res in state.pairs() if res.calc is not None}
    r.update(status.snapshot(state))
