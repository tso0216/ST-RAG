"""從資料庫撈候選：符合地點條件的 `place_note`、`move` 和它們的時間；用 embedding 先取前幾筆（12.3）；
其他條件下的筆數（12.7）。

一筆經驗符合地點條件：它的地點是撈到的地點之一，或地點解析不到、原文名稱跟撈到的地點名稱比對對到（標記名稱比對）。
地點條件是不限時每一筆都符合，包括解析不到的。
"""
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import scope as scope_mod
from . import timematch
from .data_schema import MOVE, PLACE_NOTE, TimeCond
from .embedding import CachedEmbedder, cosines
from .timematch import TimeRow

UNKNOWN = "不明"
_CHUNK = 500


@dataclass
class Exp:
    """一筆候選經驗（`place_note` 或 `move`）。"""
    table: str
    id: str
    article_id: str
    posted_at: str
    content: str
    rows: list[TimeRow] = field(default_factory=list)
    # place_note
    poi_id: int | None = None
    poi_name: str = ""
    # move
    from_id: int | None = None
    to_id: int | None = None
    from_name: str = ""
    to_name: str = ""
    # 比對的結果
    place: int | None = None          # 點：對到的地點（poi_id，或名稱比對對到的）
    ends: tuple = (None, None)        # 段：對到的（起點, 終點）
    by_name: bool = False             # 用名稱比對對到
    reversed: bool = False            # 段：方向相反
    match: timematch.Match | None = None

    @property
    def posted_year(self) -> int | None:
        return timematch.year_of(self.posted_at)

    def judge_text(self) -> str:
        """送相關判斷、算 embedding 的內容（12.2、12.3、12.4）。"""
        if self.table == MOVE:
            return f"{self.from_name} → {self.to_name}：{self.content}"
        return f"{self.poi_name}：{self.content}"


def id_key(item_id: str) -> tuple:
    """照 id 排：時間戳小的在前，再照第幾筆小的在前（`1767020760-10` 排在 `1767020760-2` 後面）。"""
    head, _, tail = item_id.rpartition("-")
    seq = int(tail) if tail.isdigit() else 0
    return (0, int(head), "", seq) if head.isdigit() else (1, 0, head or item_id, seq)


def _rows(conn: sqlite3.Connection, table: str, key: str, ids: list[str]) -> dict[str, list[TimeRow]]:
    out: dict[str, list[TimeRow]] = {i: [] for i in ids}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for item_id, year, month, day_type, time in conn.execute(
                f"SELECT {key}, year, month, day_type, time FROM {table} WHERE {key} IN ({marks}) ORDER BY rowid",
                chunk):
            out[item_id].append(TimeRow(year, month or None, day_type or None, time or None))
    return out


def place_notes(conn: sqlite3.Connection, ids: set[int] | None) -> list[Exp]:
    """符合地點條件的 `place_note`。ids 是地點條件撈到的地點，None 是不限。照 id 排。"""
    select = ("SELECT n.id, n.article_id, a.posted_at, COALESCE(n.content, ''), n.poi_id, n.poi_name"
              " FROM place_note n JOIN article a ON a.id = n.article_id")
    found: list[Exp] = []
    if ids is None:
        for r in conn.execute(select):
            found.append(Exp(PLACE_NOTE, r[0], r[1], r[2], r[3], poi_id=r[4], poi_name=r[5], place=r[4]))
    else:
        id_list = sorted(ids)
        for i in range(0, len(id_list), _CHUNK):
            chunk = id_list[i:i + _CHUNK]
            for r in conn.execute(f"{select} WHERE n.poi_id IN ({','.join('?' * len(chunk))})", chunk):
                found.append(Exp(PLACE_NOTE, r[0], r[1], r[2], r[3], poi_id=r[4], poi_name=r[5], place=r[4]))
        names = scope_mod.zh_names(conn, ids)
        if names:
            name_list = sorted(names)
            for i in range(0, len(name_list), _CHUNK):
                chunk = name_list[i:i + _CHUNK]
                for r in conn.execute(f"{select} WHERE n.poi_id IS NULL AND n.poi_name IN ({','.join('?' * len(chunk))})",
                                      chunk):
                    found.append(Exp(PLACE_NOTE, r[0], r[1], r[2], r[3], poi_id=None, poi_name=r[5],
                                     place=names[r[5]], by_name=True))
    found.sort(key=lambda e: id_key(e.id))
    rows = _rows(conn, "place_note_time", "note_id", [e.id for e in found])
    for e in found:
        e.rows = rows[e.id]
    return found


def _end(place_id: int | None, name: str, ids: set[int] | None, names: dict[str, int]) -> tuple[bool, int | None, bool]:
    """移動的一端符不符合地點條件：（符合, 對到的地點, 用名稱比對）。"""
    if ids is None:
        return True, place_id, False
    if place_id is not None:
        return place_id in ids, place_id, False
    if name in names:
        return True, names[name], True
    return False, None, False


def moves(conn: sqlite3.Connection, origin: set[int] | None, dest: set[int] | None,
          both_ways: bool) -> tuple[list[Exp], int]:
    """符合起點、終點的 `move`（None 是不限）。both_ways 時反過來的也取，標成方向相反。

    回傳（候選，反方向的筆數）；both_ways 是否時反方向的不取，只算筆數。照 id 排，正方向的在前。
    """
    origin_names = scope_mod.zh_names(conn, origin) if origin is not None else {}
    dest_names = scope_mod.zh_names(conn, dest) if dest is not None else {}
    forward: list[Exp] = []
    backward: list[Exp] = []
    for r in conn.execute("SELECT m.id, m.article_id, a.posted_at, m.content, m.from_id, m.to_id, m.from_name, m.to_name"
                          " FROM move m JOIN article a ON a.id = m.article_id"):
        item_id, article_id, posted_at, content, from_id, to_id, from_name, to_name = r
        ok_from, p_from, n_from = _end(from_id, from_name, origin, origin_names)
        ok_to, p_to, n_to = _end(to_id, to_name, dest, dest_names)
        if ok_from and ok_to:
            forward.append(Exp(MOVE, item_id, article_id, posted_at, content, from_id=from_id, to_id=to_id,
                               from_name=from_name, to_name=to_name, ends=(p_from, p_to), by_name=n_from or n_to))
            continue
        # 反方向：從終點到起點
        ok_from, p_from, n_from = _end(from_id, from_name, dest, dest_names)
        ok_to, p_to, n_to = _end(to_id, to_name, origin, origin_names)
        if ok_from and ok_to:
            backward.append(Exp(MOVE, item_id, article_id, posted_at, content, from_id=from_id, to_id=to_id,
                                from_name=from_name, to_name=to_name, ends=(p_from, p_to), by_name=n_from or n_to,
                                reversed=True))
    forward.sort(key=lambda e: id_key(e.id))
    backward.sort(key=lambda e: id_key(e.id))
    found = forward + (backward if both_ways else [])
    rows = _rows(conn, "move_time", "move_id", [e.id for e in found])
    for e in found:
        e.rows = rows[e.id]
    return found, len(backward)


def time_filter(exps: list[Exp], cond: TimeCond) -> list[Exp]:
    """留下時間相符的，記下比對結果（時間不明的維度）。"""
    kept = []
    for e in exps:
        m = timematch.match(e.rows, cond, e.posted_year)
        if m.ok:
            e.match = m
            kept.append(e)
    return kept


async def top_by_embedding(embedder: CachedEmbedder, query: str, texts: list[tuple[str, str]], k: int) -> list[str]:
    """12.3：texts 是（id, 經驗的文字），回傳餘弦相似度最高的 k 個 id；分數一樣時取 id 的時間戳小的、再取第幾筆小的。"""
    vectors = await embedder.aembed([query] + [t for _, t in texts])
    sims = cosines(vectors[0], vectors[1:])
    ranked = sorted(zip(texts, sims), key=lambda x: (-x[1], id_key(x[0][0])))
    return [item_id for (item_id, _), _ in ranked[:k]]


def other_counts(exps: list[Exp], time_matched: int, *, only_matched: bool = False,
                 reverse: int | None = None) -> dict:
    """12.7 點、段節點：同樣的地點條件、不看時間條件，各年份、月份、鐘點、平日／假日各有幾筆。

    time_matched 是時間相符的筆數。only_matched（點節點的對象是不限）時只算這一項。
    reverse 給了（段的反方向也要是否）就另外記反方向有幾筆。
    """
    counts: dict = {"時間相符": time_matched}
    if only_matched:
        return counts
    years: Counter = Counter()
    months: Counter = Counter({str(m): 0 for m in range(1, 13)} | {UNKNOWN: 0})
    hours: Counter = Counter({str(h): 0 for h in range(24)} | {UNKNOWN: 0})
    day_types: Counter = Counter({"平日": 0, "假日": 0, UNKNOWN: 0})
    for e in exps:
        rows = e.rows or [TimeRow()]
        years.update({str(r.year) if r.year else UNKNOWN for r in rows})
        months.update({str(int(r.month)) if r.month and r.month.isdigit() else UNKNOWN for r in rows})
        seen_hours: set[str] = set()
        for r in rows:
            hs = timematch.hours_of(r.time)
            seen_hours |= {str(h) for h in hs} if hs else {UNKNOWN}
        hours.update(seen_hours)
        day_types.update({r.day_type if r.day_type in ("平日", "假日") else UNKNOWN for r in rows})
    counts["全部"] = len(exps)
    counts["年份"] = dict(sorted(years.items(), key=lambda kv: (kv[0] == UNKNOWN, kv[0])))
    counts["月份"] = dict(months)
    counts["鐘點"] = dict(hours)
    counts["平日／假日"] = dict(day_types)
    if reverse is not None:
        counts["反方向"] = reverse
    return counts
