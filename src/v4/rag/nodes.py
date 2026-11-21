"""⑤ 點、段節點（規格 12.2–12.4）和排法的計算（12.8）；天節點在 `days.py`。

點、段節點的步驟一樣：候選（符合地點條件）→ 時間比對 →（點節點的對象是不限、超過 50 筆時先用 embedding 取前 50）
→ 相關判斷 → 有證據就整理重點，沒有就算其他條件下的筆數。
"""
import asyncio
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from . import config, retrieve, scope, summarize
from .data_schema import (ANYWHERE, AREA, FLAG_BY_NAME, FLAG_DIFFERENT, FLAG_REVERSED, FLAG_UNKNOWN, HAS, MINUTE,
                          MOVE, MOVE_TIME, NODE_MOVE, NODE_POINT, NONE, STAY_TIME, Evidence, Node, NodeResult)
from .embedding import CachedEmbedder
from .gate import Gate, Item
from .retrieve import Exp

log = logging.getLogger(__name__)


@dataclass
class Env:
    """執行節點用到的東西（同一題共用）。"""
    conn: sqlite3.Connection
    gate: Gate
    embedder: CachedEmbedder
    llm_sem: asyncio.Semaphore
    question: str

    def query(self, node: Node) -> str:
        """12.3 查詢的文字：問句，換行，再接節點的面向。"""
        return f"{self.question}\n{node.aspect}"


def time_text(e: Exp) -> str:
    return "；".join(r.label() for r in e.rows) or "沒寫時間"


def year_text(e: Exp) -> str:
    row = e.match.row if e.match else None
    if row is not None and row.year:
        return str(row.year)
    years = sorted({r.year for r in e.rows if r.year})
    if years:
        return "、".join(map(str, years))
    return f"沒寫（發文 {e.posted_at[:4]}）" if e.posted_at else "沒寫"


def flags_of(e: Exp) -> list[str]:
    flags = []
    if e.match and e.match.unknown:
        flags.append(f"{FLAG_UNKNOWN}（{'、'.join(e.match.unknown)}）")
    if e.reversed:
        flags.append(FLAG_REVERSED)
    if e.by_name:
        flags.append(FLAG_BY_NAME)
    return flags


def evidence_of(conn: sqlite3.Connection, e: Exp, score: float) -> Evidence:
    if e.table == MOVE:
        a = scope.display_name(conn, e.from_id, e.from_name)
        b = scope.display_name(conn, e.to_id, e.to_name)
        place, places = f"{a} → {b}", e.ends
    else:
        place, places = scope.display_name(conn, e.place, e.poi_name), (e.place,)
    return Evidence(e.table, e.id, e.article_id, e.content, place, time=time_text(e), year=year_text(e),
                    score=score, flags=flags_of(e), places=places)


def order(evidence: list[Evidence]) -> list[Evidence]:
    """證據照分數由高到低、再照 id 排。"""
    return sorted(evidence, key=lambda x: (-(x.score or 0.0), retrieve.id_key(x.id)))


def group(evidence: list[Evidence]) -> list[tuple[str, list[str]]]:
    """依 Evidence.group 分組；只有一組時不分。地點照證據筆數由多到少排，筆數一樣照最高分排。"""
    groups: dict[str, list[Evidence]] = defaultdict(list)
    for e in evidence:
        groups[e.group].append(e)
    if len(groups) <= 1:
        return []
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), -max(x.score or 0.0 for x in kv[1]), kv[0]))
    return [(name, [x.ref for x in items]) for name, items in ranked]


def mark_differences(result: NodeResult) -> None:
    """「情況不同」列到的證據標記情況不同（不改分數）。"""
    if not result.summary:
        return
    by_ref = {e.ref: e for e in result.evidence}
    for d in result.summary.differences:
        e = by_ref.get(d["source"].strip())
        if e is not None:
            e.flags.append(f"{FLAG_DIFFERENT}：{d['what']}")


async def judge(env: Env, node: Node, pool: list[Exp]) -> list[tuple[Exp, float]]:
    scores = await env.gate.nouls(env.question, node.aspect, [Item(e.table, e.id, e.judge_text()) for e in pool])
    return [(e, s) for e, s in zip(pool, scores) if s >= config.JEV_THRESHOLD]


async def point(node: Node, env: Env) -> NodeResult:
    """點節點（12.2）。對象是景點時只取它自己的 place_note（不取包含它的區域的）。"""
    assert node.target is not None
    conn = env.conn
    exps = retrieve.place_notes(conn, scope.scope_places(conn, node.target))
    matched = retrieve.time_filter(exps, node.time)
    pool, embedded = matched, None
    anywhere = node.target.mode == ANYWHERE
    if anywhere and len(matched) > config.EMBED_TOP:
        keep = set(await retrieve.top_by_embedding(env.embedder, env.query(node),
                                                   [(e.id, e.judge_text()) for e in matched], config.EMBED_TOP))
        pool = [e for e in matched if e.id in keep]
        embedded = len(pool)
    passed = await judge(env, node, pool)
    result = NodeResult(NONE, candidates=len(exps), time_matched=len(matched), embedded=embedded,
                        judged=len(pool), passed=len(passed))
    log.info("%s：候選 %d、時間相符 %d%s、通過相關判斷 %d", node.describe(), len(exps), len(matched),
             f"、embedding 取 {embedded}" if embedded is not None else "", len(passed))
    if not passed:
        result.other_counts = retrieve.other_counts(exps, len(matched), only_matched=anywhere)
        return result
    evidence = []
    for e, s in passed:
        ev = evidence_of(conn, e, s)
        ev.group = ev.place
        evidence.append(ev)
    result.status, result.evidence = HAS, order(evidence)
    result.groups = group(result.evidence)
    result.summary = await summarize.summarize(env.question, node, result.evidence, [], env.llm_sem)
    mark_differences(result)
    return result


def _group_end(node: Node, origin: set[int] | None, dest: set[int] | None) -> str | None:
    """段節點依哪一端的地點分組：一端不限、或一端撈到不只一個地點時是那一端（origin／dest），否則不分。"""
    if origin is None:
        return "origin"
    if dest is None:
        return "dest"
    if len(origin) > 1:
        return "origin"
    if len(dest) > 1:
        return "dest"
    return None


async def move(node: Node, env: Env) -> NodeResult:
    """段節點（12.4）。"""
    assert node.origin is not None and node.dest is not None
    conn = env.conn
    origin = scope.scope_places(conn, node.origin)
    dest = scope.scope_places(conn, node.dest)
    exps, reverse_count = retrieve.moves(conn, origin, dest, node.both_ways)
    matched = retrieve.time_filter(exps, node.time)
    passed = await judge(env, node, matched)
    result = NodeResult(NONE, candidates=len(exps), time_matched=len(matched), judged=len(matched),
                        passed=len(passed))
    log.info("%s：候選 %d、時間相符 %d、通過相關判斷 %d", node.describe(), len(exps), len(matched), len(passed))
    if not passed:
        result.other_counts = retrieve.other_counts(exps, len(matched),
                                                    reverse=None if node.both_ways else reverse_count)
        return result
    end = _group_end(node, origin, dest)
    evidence = []
    for e, s in passed:
        ev = evidence_of(conn, e, s)
        if end is not None:
            # 節點的起點那一端：正方向是 move 的起點，方向相反時是 move 的終點
            use_from = (end == "origin") != e.reversed
            ev.group = (scope.display_name(conn, e.ends[0], e.from_name) if use_from
                        else scope.display_name(conn, e.ends[1], e.to_name))
        evidence.append(ev)
    result.status, result.evidence = HAS, order(evidence)
    result.groups = group(result.evidence)
    result.summary = await summarize.summarize(env.question, node, result.evidence, [], env.llm_sem)
    mark_differences(result)
    return result


# ---------- 排法的計算（12.8） ----------

def _numbers(results: list[tuple[Node, NodeResult]], kind: str) -> list[tuple[dict, Evidence]]:
    """這一題所有 kind（點、段）節點的整理重點裡的數字，和它出自的那筆證據；同一則只算一次。"""
    out: list[tuple[dict, Evidence]] = []
    seen: set[tuple] = set()
    for node, result in results:
        if node.kind != kind or not result.summary:
            continue
        by_ref = {e.ref: e for e in result.evidence}
        for n in result.summary.numbers:
            e = by_ref.get(n["source"].strip())
            key = (n["source"].strip(), n["kind"].strip(), n["min"], n["max"], n["unit"].strip())
            if e is None or key in seen:
                continue
            seen.add(key)
            out.append((n, e))
    return out


def _number_row(n: dict, e: Evidence) -> dict:
    return {"種類": n["kind"].strip(), "最少": n["min"], "最多": n["max"], "單位": n["unit"].strip(),
            "原文": n["quote"], "出處": e.ref}


def calc(node: Node, results: list[tuple[Node, NodeResult]], conn: sqlite3.Connection) -> dict | None:
    """排法的計算：排法至少兩站解析得到時才做（否則 None）。只做距離和加總，不判斷。

    results 是這一題目前所有節點和結果（用點節點的停留時間、段節點的移動時間）。
    同一個地點連續出現不算一段。
    """
    stops = list(node.stops)
    if sum(1 for s in stops if s.resolved) < 2:
        return None
    points = _numbers(results, NODE_POINT)
    moves = _numbers(results, NODE_MOVE)

    stop_rows = []
    missing: list[str] = []
    low = high = 0.0
    for s in stops:
        pid = s.where.place_id
        mine = [(n, e) for n, e in points if pid is not None and e.places and e.places[0] == pid]
        stay = [(n, e) for n, e in mine if n["kind"].strip() == STAY_TIME and n["unit"].strip() == MINUTE]
        other = [(n, e) for n, e in mine if n["kind"].strip() not in (STAY_TIME, MOVE_TIME)]
        row = {"站": s.where.label, "種類": s.type, "從哪一天": s.day or "自己寫的",
               "停留時間": [_number_row(n, e) for n, e in stay] or "沒有資料",
               "其他數字": [_number_row(n, e) for n, e in other]}
        if not s.resolved:
            row["座標"] = "解析不到"
        stop_rows.append(row)
        if stay:
            low += min(n["min"] for n, _ in stay)
            high += max(n["max"] for n, _ in stay)
        else:
            missing.append(f"停留時間：{s.where.label}")

    segments = []
    total_km = 0.0
    for a, b in zip(stops, stops[1:]):
        if a.key() == b.key():
            continue
        seg: dict = {"段": f"{a.where.label} → {b.where.label}"}
        ca, cb = scope.coordinates(conn, a.where.place_id), scope.coordinates(conn, b.where.place_id)
        if ca is None or cb is None:
            seg["直線距離"] = "沒有座標"
        elif ca[0] == AREA or cb[0] == AREA:
            seg["直線距離"] = "區域，不算距離"
        else:
            d = scope.km(ca[1], ca[2], cb[1], cb[2])
            seg["直線距離"] = f"{d:g} 公里"
            total_km += d
        pair = {a.where.place_id, b.where.place_id}
        mine = [(n, e) for n, e in moves
                if a.resolved and b.resolved and len(e.places) == 2 and set(e.places) == pair]
        move_times = [(n, e) for n, e in mine if n["kind"].strip() == MOVE_TIME and n["unit"].strip() == MINUTE]
        seg["移動時間"] = [_number_row(n, e) for n, e in move_times] or "沒有資料"
        seg["其他數字"] = [_number_row(n, e) for n, e in mine if n["kind"].strip() not in (STAY_TIME, MOVE_TIME)]
        segments.append(seg)
        if move_times:
            low += min(n["min"] for n, _ in move_times)
            high += max(n["max"] for n, _ in move_times)
        else:
            missing.append(f"移動時間：{seg['段']}")

    total = f"最少 {low:g} 分鐘到最多 {high:g} 分鐘"
    if missing:
        total += f"（部分加總，缺 {len(missing)} 項）"
    return {"站數": len(stops), "每站": stop_rows, "每段": segments, "直線距離加總": f"{round(total_km, 1):g} 公里",
            "合計": total, "合計最少": low, "合計最多": high, "缺": missing}
