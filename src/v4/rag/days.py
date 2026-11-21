"""天節點（規格 12.5）：有排法時挑出有相同站的天、沒有排法時從評語找；帶出每一天的站和 `day_note`；
天節點的其他條件下的筆數（12.7）。

相同的站：別人那天的一站跟排法的某一站是同一個地點。排法的站是景點時那一站要是同一個 place id，
是區域時那一站在區域範圍裡（6）；那一站解析不到時用名稱比對。不算距離，也不比順序。
"""
import logging
import sqlite3
from collections import Counter, defaultdict

from v4 import place as place_table

from . import config, retrieve, scope, summarize, timematch
from .data_schema import (AREA, DAY, DAY_NOTE, FLAG_UNKNOWN, HAS, HAS_NO_NOTE, NONE, DayView, Evidence, Node,
                          NodeResult, Stop)
from .gate import Item
from .nodes import Env, mark_differences, order
from .retrieve import UNKNOWN

log = logging.getLogger(__name__)


def day_no(day_id: str) -> int:
    tail = day_id.rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


class _Matcher:
    """排法的一站：撈到的地點和它們的繁中名稱（名稱比對用）。"""

    def __init__(self, conn: sqlite3.Connection, stop: Stop):
        found = place_table.get(conn, stop.where.place_id)
        self.label = stop.where.label
        self.ids = scope.places_in_area(conn, found) if found.kind == AREA else {found.id}
        self.names = scope.zh_names(conn, self.ids)

    def hit(self, poi_id: int | None, poi_name: str) -> bool:
        if poi_id is not None:
            return poi_id in self.ids
        return poi_name in self.names


def compared_stops(stops: tuple[Stop, ...]) -> list[Stop]:
    """拿來比的站：解析不到的不比，同一個地點只算一站。"""
    out, seen = [], set()
    for s in stops:
        if s.resolved and s.where.place_id not in seen:
            seen.add(s.where.place_id)
            out.append(s)
    return out


def _day_rows(conn: sqlite3.Connection, day_ids: list[str]) -> dict[str, tuple]:
    """day id →（article_id, month, title, posted_at）。"""
    out = {}
    for i in range(0, len(day_ids), 500):
        chunk = day_ids[i:i + 500]
        for r in conn.execute("SELECT d.id, d.article_id, d.month, a.title, a.posted_at FROM day d"
                              f" JOIN article a ON a.id = d.article_id WHERE d.id IN ({','.join('?' * len(chunk))})",
                              chunk):
            out[r[0]] = r[1:]
    return out


def view(conn: sqlite3.Connection, day_id: str, matchers: list[_Matcher] | None = None) -> DayView:
    """帶出一天：文章、第幾天、那篇有幾天、月份、照順序的站（標出相同的站）、對到這天的 day_note、整趟的 day_note。"""
    article_id, month, title, posted_at = _day_rows(conn, [day_id])[day_id]
    stops = []
    same: list[str] = []
    for poi_id, poi_name, type_ in conn.execute(
            "SELECT poi_id, poi_name, type FROM day_place WHERE day_id = ? ORDER BY seq", (day_id,)):
        hits = [m.label for m in matchers or [] if m.hit(poi_id, poi_name)]
        stops.append({"name": scope.display_name(conn, poi_id, poi_name), "type": type_, "place_id": poi_id,
                      "same": bool(hits)})
        same += [h for h in hits if h not in same]
    days_in_article = conn.execute("SELECT COUNT(*) FROM day WHERE article_id = ?", (article_id,)).fetchone()[0]
    notes = [{"id": i, "content": c} for i, c in conn.execute(
        "SELECT n.id, n.content FROM day_note n JOIN day_note_day nd ON nd.note_id = n.id WHERE nd.day_id = ?",
        (day_id,))]
    trip = [{"id": i, "content": c} for i, c in conn.execute(
        "SELECT n.id, n.content FROM day_note n WHERE n.article_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM day_note_day nd WHERE nd.note_id = n.id)", (article_id,))]
    notes.sort(key=lambda n: retrieve.id_key(n["id"]))
    trip.sort(key=lambda n: retrieve.id_key(n["id"]))
    return DayView(day_id, article_id, title, posted_at, day_no(day_id), days_in_article, month or "", stops,
                   notes, trip, same)


def _stops_text(d: DayView) -> str:
    return " → ".join(f"{s['name']}（{s['type']}）" for s in d.stops) or "（沒有站）"


def _evidence(days: list[DayView], with_days: bool, notes_only: set[str] | None = None) -> list[Evidence]:
    """天和它們帶出的 day_note 變成證據。notes_only 給了時只收這些 day_note（沒有排法：通過相關判斷的）。"""
    out: list[Evidence] = []
    seen: set[str] = set()
    for d in days:
        if with_days:
            flags = [f"{FLAG_UNKNOWN}（{'、'.join(d.unknown)}）"] if d.unknown else []
            out.append(Evidence(DAY, d.id, d.article_id, _stops_text(d), f"第 {d.no} 天",
                                time=f"{d.month} 月" if d.month else "沒寫月份", year=f"發文 {d.posted_at[:4]}",
                                flags=flags))
        for n in [*d.notes, *d.trip_notes]:
            if n["id"] in seen or (notes_only is not None and n["id"] not in notes_only):
                continue
            seen.add(n["id"])
            out.append(Evidence(DAY_NOTE, n["id"], d.article_id, n["content"], "整趟" if n in d.trip_notes
                                else f"第 {d.no} 天", time=f"{d.month} 月" if d.month else "沒寫月份",
                                year=f"發文 {d.posted_at[:4]}"))
    return out


async def day(node: Node, env: Env) -> NodeResult:
    if node.has_arrangement:
        return await _arranged(node, env)
    return await _free(node, env)


async def _arranged(node: Node, env: Env) -> NodeResult:
    """有排法：挑出至少一站相同、時間相符的天，相同的站數多的在前；一樣多時發文時間新的在前，再照第幾天小的在前。"""
    conn = env.conn
    compared = compared_stops(node.stops)
    matchers = [_Matcher(conn, s) for s in compared]
    same: dict[str, set[int]] = defaultdict(set)    # day id → 相同的排法站（matchers 的 index）
    for day_id, poi_id, poi_name in conn.execute("SELECT day_id, poi_id, poi_name FROM day_place"):
        for i, m in enumerate(matchers):
            if m.hit(poi_id, poi_name):
                same[day_id].add(i)
    info = _day_rows(conn, sorted(same))
    matched: list[tuple[str, timematch.Match]] = []
    for day_id in same:
        article_id, month, title, posted_at = info[day_id]
        m = timematch.match(timematch.day_rows(month), node.time, timematch.year_of(posted_at))
        if m.ok:
            matched.append((day_id, m))

    def rank(item: tuple[str, timematch.Match]) -> tuple:
        day_id = item[0]
        article_id, _, _, posted_at = info[day_id]
        return (-len(same[day_id]), _desc(posted_at), day_no(day_id), article_id)

    matched.sort(key=rank)
    picked = matched[:config.DAY_TOP]
    result = NodeResult(NONE, candidates=len(same), time_matched=len(matched), same_days=len(matched))
    log.info("%s：至少一站相同 %d 天、時間相符 %d 天、挑 %d 天", node.describe(), len(same), len(matched), len(picked))
    if not picked:
        years: Counter = Counter()
        months: Counter = Counter()
        for day_id in same:
            _, month, _, posted_at = info[day_id]
            years[posted_at[:4] or UNKNOWN] += 1
            months[month or UNKNOWN] += 1
        each = {}
        for s in node.stops:
            if not s.resolved:
                each[s.where.label] = "解析不到，沒比"
            elif s.where.label not in each:
                i = next(i for i, c in enumerate(compared) if c.where.place_id == s.where.place_id)
                each[s.where.label] = sum(1 for hit in same.values() if i in hit)
        result.other_counts = {"至少一站相同的天數（不看時間）": len(same), "年份": dict(sorted(years.items())),
                               "月份": dict(sorted(months.items(), key=lambda kv: (kv[0] == UNKNOWN, _int(kv[0])))),
                               "每一站單獨去過的天數": each}
        return result
    days = []
    for day_id, m in picked:
        d = view(conn, day_id, matchers)
        d.unknown = list(m.unknown)
        days.append(d)
    result.days = days
    result.evidence = _evidence(days, with_days=True)
    result.status = HAS if any(e.table == DAY_NOTE for e in result.evidence) else HAS_NO_NOTE
    result.summary = await summarize.summarize(env.question, node, result.evidence, days, env.llm_sem)
    mark_differences(result)
    return result


async def _free(node: Node, env: Env) -> NodeResult:
    """沒有排法：全部 day_note（含講整趟的）做時間比對 →（超過 50 筆用 embedding 取前 50）→ 相關判斷；
    通過的是證據，帶出它講到的天（講整趟的帶出那篇所有天）。"""
    conn = env.conn
    article_days: dict[str, list[tuple[str, str | None]]] = defaultdict(list)   # 文章 → [(day id, month)]
    for day_id, article_id, month in conn.execute("SELECT id, article_id, month FROM day"):
        article_days[article_id].append((day_id, month))
    for days_ in article_days.values():
        days_.sort(key=lambda x: day_no(x[0]))
    linked: dict[str, list[str]] = defaultdict(list)
    for note_id, day_id in conn.execute("SELECT note_id, day_id FROM day_note_day"):
        linked[note_id].append(day_id)
    month_of = {day_id: month for days_ in article_days.values() for day_id, month in days_}

    notes = []
    for note_id, article_id, content, posted_at in conn.execute(
            "SELECT n.id, n.article_id, n.content, a.posted_at FROM day_note n JOIN article a ON a.id = n.article_id"):
        ids = linked.get(note_id) or [d for d, _ in article_days.get(article_id, [])]
        m = timematch.match(timematch.month_rows([month_of.get(d) for d in ids]), node.time,
                            timematch.year_of(posted_at))
        notes.append((note_id, article_id, content, m, sorted(ids, key=day_no), not linked.get(note_id)))
    notes.sort(key=lambda n: retrieve.id_key(n[0]))
    matched = [n for n in notes if n[3].ok]
    pool, embedded = matched, None
    if len(matched) > config.EMBED_TOP:
        keep = set(await retrieve.top_by_embedding(env.embedder, env.query(node), [(n[0], n[2]) for n in matched],
                                                   config.EMBED_TOP))
        pool = [n for n in matched if n[0] in keep]
        embedded = len(pool)
    scores = await env.gate.nouls(env.question, node.aspect, [Item(DAY_NOTE, n[0], n[2]) for n in pool])
    passed = [(n, s) for n, s in zip(pool, scores) if s >= config.JEV_THRESHOLD]
    result = NodeResult(NONE, candidates=len(notes), time_matched=len(matched), embedded=embedded,
                        judged=len(pool), passed=len(passed))
    log.info("%s：day_note %d、時間相符 %d%s、通過相關判斷 %d", node.describe(), len(notes), len(matched),
             f"、embedding 取 {embedded}" if embedded is not None else "", len(passed))
    if not passed:
        result.other_counts = {"時間相符": len(matched)}
        return result
    passed.sort(key=lambda x: (-x[1], retrieve.id_key(x[0][0])))
    scores_of = {n[0]: s for n, s in passed}
    unknown_of = {n[0]: n[3].unknown for n, _ in passed}
    day_ids: list[str] = []
    for n, _ in passed:
        for d in n[4]:
            if d not in day_ids:
                day_ids.append(d)
    days = [view(conn, d) for d in day_ids]
    result.days = days
    evidence = _evidence(days, with_days=False, notes_only=set(scores_of))
    # 講整趟的評語、而那篇文章一天都沒有時，帶不出任何天，`_evidence` 收不到它，這裡另外補成證據
    listed = {e.id for e in evidence}
    for n, s in passed:
        if n[0] not in listed:
            evidence.append(Evidence(DAY_NOTE, n[0], n[1], n[2], "整趟"))
    for e in evidence:
        e.score = scores_of.get(e.id)
        if unknown_of.get(e.id):
            e.flags.append(f"{FLAG_UNKNOWN}（{'、'.join(unknown_of[e.id])}）")
    result.status, result.evidence = HAS, order(evidence)
    result.summary = await summarize.summarize(env.question, node, result.evidence, days, env.llm_sem)
    mark_differences(result)
    return result


def _desc(text: str) -> tuple:
    """字串由大到小排用的鍵（較長的字串排在它的前綴前面）。"""
    return (*(-ord(c) for c in text), 1)


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 99
