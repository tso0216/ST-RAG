"""8 個工具（spec 第 3、4 章）。每個工具是「`Env`、參數 dict → 文字」的函式，不依賴 Agent SDK。

參數不合法、地點找不到時丟 `ToolError`，訊息寫出哪個參數錯了、要怎麼改，由 `loop` 回給 LLM。
`find_place` 是 async（可能要地點解析），其他都是一般函式。

資料庫很小（經驗幾千筆），每次需要時整個讀進記憶體（`Index`）；`find_place` 新增地點後重讀。
"""
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from service import embedding
from v4.extract.data_schema import LOCATED_YES, Context, PlaceRef

from . import config

SPOT, AREA, STAY = "景點", "區域", "住宿"
WEEKDAY, HOLIDAY = "平日", "假日"
EARTH_KM = 6371.0


class ToolError(Exception):
    """參數不合法或找不到東西：回給 LLM，由它改好再呼叫。"""


# ---------- 編號 ----------

def id_key(row_id: str) -> tuple[int, int]:
    """「時間戳-第幾筆」照數字排（`-10` 在 `-2` 後面）。"""
    stamp, _, n = row_id.partition("-")
    return int(stamp), int(n or 0)


def day_no(day_id: str) -> int:
    return id_key(day_id)[1]


def number_ranges(numbers: list[int]) -> str:
    """[2, 3, 4, 6] → 2–4、6"""
    out, nums = [], sorted(set(numbers))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return "、".join(out)


# ---------- 時間 ----------

@dataclass(frozen=True)
class TimeRow:
    year: int | None = None
    month: int | None = None
    day_type: str | None = None
    hours: frozenset[int] | None = None
    time: str | None = None          # 原文的鐘點寫法（`9`、`9-11`），顯示用


EMPTY_ROW = TimeRow()


def hour_set(start: int, end: int) -> frozenset[int]:
    if start <= end:
        return frozenset(range(start, end + 1))
    return frozenset(range(start, 24)) | frozenset(range(0, end + 1))


def parse_hours(text: str | None) -> frozenset[int] | None:
    text = (text or "").strip()
    m = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?", text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not (0 <= a <= 23 and 0 <= b <= 23):
        return None
    return hour_set(a, b)


def parse_month(text: Any) -> int | None:
    try:
        m = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return m if 1 <= m <= 12 else None


def time_row(year, month, day_type, time) -> TimeRow:
    hours = parse_hours(time)
    return TimeRow(year if isinstance(year, int) else None, parse_month(month),
                   day_type if day_type in (WEEKDAY, HOLIDAY) else None, hours, time if hours else None)


def render_row(r: TimeRow) -> str:
    parts = []
    if r.year is not None:
        parts.append(f"{r.year} 年")
    if r.month is not None:
        parts.append(f"{r.month} 月")
    if r.day_type:
        parts.append(r.day_type)
    if r.time:
        parts.append(r.time.replace("-", "–") + " 點")
    return " ".join(parts)


def render_rows(rows: list[TimeRow]) -> str:
    return "；".join(t for t in map(render_row, rows) if t) or "沒寫時間"


@dataclass(frozen=True)
class TimeCond:
    year: int | None = None
    months: frozenset[int] | None = None
    day_type: str | None = None
    hours: frozenset[int] | None = None
    hours_text: str = ""

    @property
    def limited(self) -> bool:
        return any(x is not None for x in (self.year, self.months, self.day_type, self.hours))

    def label(self) -> str:
        parts = []
        if self.year is not None:
            parts.append(f"{self.year} 年以後")
        if self.months:
            parts.append("月份 " + "、".join(map(str, sorted(self.months))))
        if self.day_type:
            parts.append(self.day_type)
        if self.hours:
            parts.append("鐘點 " + self.hours_text)
        return "｜".join(parts)


NO_TIME = TimeCond()


def parse_time(arg: Any, *, only_year_month: bool = False) -> TimeCond:
    if arg is None:
        return NO_TIME
    if not isinstance(arg, dict):
        raise ToolError('time 要是物件，例如 {"months": [10, 11]}')
    allowed = ("year", "months") if only_year_month else ("year", "months", "day_type", "hours")
    extra = [k for k in arg if k not in allowed]
    if extra:
        raise ToolError(f"time 不能有 {'、'.join(extra)}；這個工具的 time 只能有 {'、'.join(allowed)}")
    year = arg.get("year")
    if year is not None and not (isinstance(year, int) and 1000 <= year <= 9999):
        raise ToolError("time.year 要是四位數的年，例如 2025")
    months = arg.get("months")
    if months is not None:
        if not isinstance(months, list) or not months or not all(isinstance(m, int) and 1 <= m <= 12 for m in months):
            raise ToolError("time.months 要是 1–12 的整數陣列，例如 [10, 11]；不限月份就省略")
        months = frozenset(months)
    day_type = arg.get("day_type")
    if day_type is not None and day_type not in (WEEKDAY, HOLIDAY):
        raise ToolError("time.day_type 只能是「平日」或「假日」；不限就省略")
    hours, texts = None, []
    if arg.get("hours") is not None:
        ranges = arg["hours"]
        if not isinstance(ranges, list) or not ranges:
            raise ToolError('time.hours 要是陣列，例如 [{"start": 22, "end": 2}]；不限就省略')
        hours = frozenset()
        for r in ranges:
            if not (isinstance(r, dict) and all(isinstance(r.get(k), int) and 0 <= r[k] <= 23 for k in ("start", "end"))):
                raise ToolError('time.hours 的每一項要是 {"start": 0–23, "end": 0–23}')
            hours |= hour_set(r["start"], r["end"])
            texts.append(f"{r['start']}–{r['end']}")
    return TimeCond(year, months, day_type, hours, "、".join(texts))


def match_row(r: TimeRow, c: TimeCond, posted_year: int) -> list[str] | None:
    """符合就回傳時間不明的維度（可能是空的），不符合回傳 None。"""
    unknown = []
    if c.year is not None:
        if r.year is None:
            if posted_year < c.year:
                return None
            unknown.append("年份")
        elif r.year < c.year:
            return None
    if c.months:
        if r.month is None:
            unknown.append("月份")
        elif r.month not in c.months:
            return None
    if c.day_type:
        if r.day_type is None:
            unknown.append("平日／假日")
        elif r.day_type != c.day_type:
            return None
    if c.hours:
        if r.hours is None:
            unknown.append("鐘點")
        elif not r.hours & c.hours:
            return None
    return unknown


def match_time(rows: list[TimeRow], c: TimeCond, posted_year: int) -> list[str] | None:
    """有一列符合就算符合；好幾列符合時取不明的維度最少的。一列都沒有時當成一列空的。"""
    best = None
    for r in rows or [EMPTY_ROW]:
        u = match_row(r, c, posted_year)
        if u is not None and (best is None or len(u) < len(best)):
            best = u
    return best


def distribution(rows_list: list[list[TimeRow]]) -> str:
    """不看時間的分布：每一筆經驗在每個維度的值各算一次；空的算不明。"""
    months, hours, years, kinds = Counter(), Counter(), Counter(), Counter()
    for rows in rows_list:
        rows = rows or [EMPTY_ROW]
        months.update({r.month if r.month is not None else "不明" for r in rows})
        years.update({r.year if r.year is not None else "不明" for r in rows})
        kinds.update({r.day_type or "不明" for r in rows})
        hs: set = set()
        for r in rows:
            hs |= set(r.hours) if r.hours is not None else {"不明"}
        hours.update(hs)

    def join(counter: Counter, keys: list, sep: str = ":") -> str:
        return "、".join(f"{k}{sep}{counter[k]}" if k != "不明" else f"不明 {counter[k]}"
                         for k in keys if counter[k])

    parts = [
        "月份 " + join(months, [*range(1, 13), "不明"]),
        "鐘點 " + join(hours, [*range(24), "不明"]),
        "年份 " + join(years, [*sorted(k for k in years if k != "不明"), "不明"]),
        "平日／假日 " + join(kinds, [WEEKDAY, HOLIDAY, "不明"], " "),
    ]
    return "不看時間的分布：" + "｜".join(parts)


# ---------- 地點 ----------

@dataclass(frozen=True)
class Place:
    id: int
    kind: str
    name_ja: str
    name_zh: str | None
    city: str
    lat: float
    lon: float
    polygons: list | None      # [[外圈, 洞, …], …]，每圈是 [(lon, lat), …]
    size: float = 0.0          # 經緯度算的面積，只拿來比大小

    @property
    def name(self) -> str:
        return self.name_zh or self.name_ja


def km_between(a: Place, b: Place) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def _in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _ring_area(ring) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def _polygons(boundary: str | None) -> list | None:
    if not boundary:
        return None
    g = json.loads(boundary)
    if g.get("type") == "Polygon":
        polys = [g["coordinates"]]
    elif g.get("type") == "MultiPolygon":
        polys = g["coordinates"]
    else:
        return None
    return [[[(float(x), float(y)) for x, y, *_ in ring] for ring in poly] for poly in polys]


def inside(p: Place, area: Place) -> bool:
    return any(_in_ring(p.lon, p.lat, poly[0]) and not any(_in_ring(p.lon, p.lat, h) for h in poly[1:])
               for poly in area.polygons or [])


# ---------- 讀進記憶體的資料 ----------

@dataclass
class Note:
    id: str
    article_id: str
    poi_id: int | None
    poi_name: str
    content: str
    times: list[TimeRow]
    place: int | None = None       # 地點：poi_id，解析不到時用名稱比對
    by_name: bool = False


@dataclass
class MoveRow:
    id: str
    article_id: str
    from_id: int | None
    to_id: int | None
    from_name: str
    to_name: str
    content: str
    times: list[TimeRow]
    origin: int | None = None
    dest: int | None = None
    by_name: bool = False


@dataclass
class DayStop:
    poi_id: int | None
    poi_name: str
    type: str
    place: int | None = None


@dataclass
class DayRow:
    id: str
    article_id: str
    month: int | None
    stops: list[DayStop]


@dataclass
class DayNoteRow:
    id: str
    article_id: str
    content: str
    day_ids: list[str]            # 空的是講整趟


class Index:
    def __init__(self, conn: sqlite3.Connection):
        self.places: dict[int, Place] = {}
        for pid, kind, name_ja, name_zh, city, lat, lon, boundary in conn.execute(
                "SELECT id, kind, name_ja, name_zh, city, lat, lon, boundary FROM place"):
            polys = _polygons(boundary)
            size = sum(_ring_area(poly[0]) - sum(_ring_area(h) for h in poly[1:]) for poly in polys) if polys else 0.0
            self.places[pid] = Place(pid, kind, name_ja, name_zh, city or "", lat, lon, polys, size)
        # 名稱比對：繁中名稱和繁中別名 → id 小的
        self.zh_names: dict[str, int] = {}
        rows = [(p.name_zh, p.id) for p in self.places.values() if p.name_zh]
        rows += conn.execute("SELECT alias, id FROM place_alias_zh").fetchall()
        for name, pid in sorted(rows, key=lambda r: r[1]):
            self.zh_names.setdefault(name.strip(), pid)

        self.articles: dict[str, tuple[str, str]] = {
            a: (t, p) for a, t, p in conn.execute("SELECT id, title, posted_at FROM article")}

        note_times: dict[str, list[TimeRow]] = {}
        for nid, y, m, dt, t in conn.execute(
                "SELECT note_id, year, month, day_type, time FROM place_note_time ORDER BY rowid"):
            note_times.setdefault(nid, []).append(time_row(y, m, dt, t))
        self.notes = [Note(i, a, p, n, c or "", note_times.get(i, [])) for i, a, p, n, c in conn.execute(
            "SELECT id, article_id, poi_id, poi_name, content FROM place_note")]
        for n in self.notes:
            n.place, n.by_name = self.locate(n.poi_id, n.poi_name)

        move_times: dict[str, list[TimeRow]] = {}
        for mid, y, m, dt, t in conn.execute(
                "SELECT move_id, year, month, day_type, time FROM move_time ORDER BY rowid"):
            move_times.setdefault(mid, []).append(time_row(y, m, dt, t))
        self.moves = [MoveRow(*r, move_times.get(r[0], [])) for r in conn.execute(
            "SELECT id, article_id, from_id, to_id, from_name, to_name, content FROM move")]
        for mv in self.moves:
            mv.origin, a = self.locate(mv.from_id, mv.from_name)
            mv.dest, b = self.locate(mv.to_id, mv.to_name)
            mv.by_name = a or b

        self.days: dict[str, DayRow] = {
            i: DayRow(i, a, parse_month(m), []) for i, a, m in conn.execute("SELECT id, article_id, month FROM day")}
        for day_id, poi_id, name, type_ in conn.execute(
                "SELECT day_id, poi_id, poi_name, type FROM day_place ORDER BY day_id, seq"):
            if day_id in self.days:
                self.days[day_id].stops.append(DayStop(poi_id, name, type_, self.locate(poi_id, name)[0]))
        self.article_days: dict[str, list[str]] = {}
        for d in sorted(self.days.values(), key=lambda d: id_key(d.id)):
            self.article_days.setdefault(d.article_id, []).append(d.id)

        links: dict[str, list[str]] = {}
        for nid, did in conn.execute("SELECT note_id, day_id FROM day_note_day"):
            links.setdefault(nid, []).append(did)
        self.day_notes = [DayNoteRow(i, a, c, sorted(links.get(i, []), key=id_key)) for i, a, c in conn.execute(
            "SELECT id, article_id, content FROM day_note")]
        self.day_notes_by_id = {n.id: n for n in self.day_notes}

        # 每個地點直接有幾筆（算筆數用）
        self.note_count: Counter = Counter(n.place for n in self.notes if n.place is not None)
        self.move_ids: dict[int, set[str]] = {}
        for mv in self.moves:
            for pid in (mv.origin, mv.dest):
                if pid is not None:
                    self.move_ids.setdefault(pid, set()).add(mv.id)
        self.day_ids: dict[int, set[str]] = {}
        for d in self.days.values():
            for s in d.stops:
                if s.place is not None:
                    self.day_ids.setdefault(s.place, set()).add(d.id)
        self._members: dict[int, frozenset[int]] = {}

    def locate(self, poi_id: int | None, name: str) -> tuple[int | None, bool]:
        """經驗的地點：有 place id 用它；沒有時拿原文的名稱比對繁中名稱和別名。"""
        if poi_id is not None:
            return poi_id, False
        pid = self.zh_names.get((name or "").strip())
        return (pid, True) if pid is not None else (None, False)

    def place(self, pid: Any, param: str) -> Place:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid not in self.places:
            raise ToolError(f"{param}：place id {pid} 不存在；先用 find_place 找地點的 id")
        return self.places[pid]

    def members(self, pid: int) -> frozenset[int]:
        """地點條件 `地點` 撈到的地點：景點只有自己；區域是自己加上範圍裡的景點和比它小的區域。"""
        if pid in self._members:
            return self._members[pid]
        p = self.places[pid]
        found = {pid}
        if p.kind == AREA and p.polygons:
            for q in self.places.values():
                if q.id == pid:
                    continue
                if q.kind == SPOT or (q.polygons and q.size < p.size):
                    if inside(q, p):
                        found.add(q.id)
        self._members[pid] = frozenset(found)
        return self._members[pid]

    def around(self, pid: int, km: float) -> frozenset[int]:
        c = self.places[pid]
        return frozenset({pid} | {q.id for q in self.places.values()
                                  if q.kind == SPOT and km_between(c, q) <= km})

    def containing_areas(self, pid: int) -> list[Place]:
        """範圍包含這個地點位置的區域（不含它自己），小的在前。"""
        p = self.places[pid]
        return sorted((a for a in self.places.values()
                       if a.id != pid and a.kind == AREA and a.polygons and inside(p, a)), key=lambda a: a.size)

    def counts(self, ids: frozenset[int] | set[int]) -> tuple[int, int, int]:
        """點、段、天的筆數。"""
        points = sum(self.note_count[i] for i in ids)
        moves = set().union(*(self.move_ids.get(i, set()) for i in ids))
        days = set().union(*(self.day_ids.get(i, set()) for i in ids))
        return points, len(moves), len(days)

    def posted(self, article_id: str) -> str:
        return self.articles.get(article_id, ("", ""))[1]

    def posted_year(self, article_id: str) -> int:
        return int(self.posted(article_id)[:4] or 0)

    def title(self, article_id: str) -> str:
        return self.articles.get(article_id, ("", ""))[0]


# ---------- 使用者的行程和工具的環境 ----------

@dataclass
class UserPlace:
    ref: str
    name_zh: str
    name_ja: str
    city: str
    kind: str
    located: str
    sentence: str
    place_id: int | None = None
    status: str = "還沒解析"        # 已解析／還沒解析／解析不到
    reason: str = ""

    def place_ref(self) -> PlaceRef:
        return PlaceRef(self.ref, self.name_zh, self.name_ja, self.city, self.kind, self.located,
                        sentence=self.sentence)


@dataclass
class UserStop:
    ref: str
    name: str
    type: str
    place_id: int | None


@dataclass
class UserDay:
    code: str          # D1、D2……
    label: str         # 原文的寫法
    month: str
    stops: list[UserStop]


@dataclass
class Env:
    """一篇發問文的各題共用：資料庫連線、使用者的行程、地點解析的快取。"""
    conn: sqlite3.Connection
    article_id: str
    places: dict[str, UserPlace] = field(default_factory=dict)
    days: dict[str, UserDay] = field(default_factory=dict)
    geocoder: Any = None             # v4.extract 的 Geocoder（`resolve(PlaceRef, Context)`）
    question: str = ""               # 現在這一題的問句（find_place 解析新地點時當原文那一行）
    embed: Any = None                # texts → 向量；None 用 `service.embedding`
    resolved: dict[tuple, int | None] = field(default_factory=dict)
    _index: Index | None = None

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = Index(self.conn)
        return self._index

    def reload(self) -> None:
        self._index = None


# ---------- 參數 ----------

def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    v = args.get(key, default)
    if v is None:
        return default
    if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
        raise ToolError(f"{key} 要是 {low}–{high} 的整數")
    return v


def paging(args: dict, default: int, most: int) -> tuple[int, int]:
    return _int(args, "limit", default, 1, most), _int(args, "offset", 0, 0, 10 ** 6)


@dataclass
class PlaceCond:
    ids: frozenset[int] | None     # None：不限
    label: str
    many: bool                     # 撈到不只一個地點

    def ok(self, pid: int | None) -> bool:
        return self.ids is None or (pid is not None and pid in self.ids)


def place_cond(idx: Index, arg: Any, param: str, with_areas: bool = False) -> PlaceCond:
    """`with_areas`：周圍另外收包含中心的區域（移動的起訖常寫成城市，見規格 3.2）。"""
    if not isinstance(arg, dict) or arg.get("mode") not in ("地點", "周圍", "不限"):
        raise ToolError(f'{param} 要是 {{"mode": "地點", "place_id": id}}、'
                        f'{{"mode": "周圍", "place_id": id, "km": 半徑}} 或 {{"mode": "不限"}}')
    mode = arg["mode"]
    if mode == "不限":
        return PlaceCond(None, "不限地點", True)
    p = idx.place(arg.get("place_id"), f"{param}.place_id")
    if mode == "地點":
        ids = idx.members(p.id)
        if p.kind == AREA and len(ids) > 1:
            return PlaceCond(ids, f"{p.name}範圍裡（{len(ids)} 個地點）", True)
        return PlaceCond(ids, p.name, False)
    km = arg.get("km")
    if not isinstance(km, (int, float)) or isinstance(km, bool) or km <= 0:
        raise ToolError(f"{param}.km 要是大於 0 的數字（公里）。走路到得了的通常 0.5–1，搭車或開車到得了的大得多")
    ids = idx.around(p.id, float(km))
    areas = idx.containing_areas(p.id) if with_areas else []
    extra = f"＋所在區域 {'、'.join(a.name for a in areas)}" if areas else ""
    return PlaceCond(ids | {a.id for a in areas}, f"{p.name}周圍 {km:g} 公里（{len(ids)} 個地點{extra}）", True)


def query_arg(args: dict, required: bool, why: str) -> str:
    q = args.get("query")
    if q is not None and not isinstance(q, str):
        raise ToolError("query 要是文字")
    q = (q or "").strip()
    if required and not q:
        raise ToolError(f"query 必填：{why}")
    return q


# ---------- embedding ----------

def query_text(query: str) -> str:
    """查詢送去算向量前的樣子（Qwen3-Embedding 要加任務指令，見 `service.embedding.query_text`）。"""
    return embedding.query_text(query, config.EMBEDDING_MODEL, config.QUERY_INSTRUCT)


def embed(env: Env, texts: list[str]) -> np.ndarray:
    """每段文字的向量（已正規化）。用 `service.embedding` 時先查 `config.EMBEDDING_CACHE`，沒有的才算，算完寫回去；
    `env.embed` 有給（測試用）就不快取。"""
    if env.embed:
        return _normalize(_compute(env.embed, texts))
    model = config.EMBEDDING_MODEL
    unique = list(dict.fromkeys(texts))
    config.EMBEDDING_CACHE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.EMBEDDING_CACHE, timeout=60)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS embedding (model TEXT NOT NULL, text TEXT NOT NULL,"
                     " vector BLOB NOT NULL, PRIMARY KEY (model, text))")
        found: dict[str, np.ndarray] = {}
        for i in range(0, len(unique), 500):
            chunk = unique[i:i + 500]
            for text, blob in conn.execute(
                    f"SELECT text, vector FROM embedding WHERE model = ? AND text IN ({','.join('?' * len(chunk))})",
                    (model, *chunk)):
                found[text] = np.frombuffer(blob, dtype=np.float32)
        missing = [t for t in unique if t not in found]
        for i in range(0, len(missing), config.EMBEDDING_BATCH):
            chunk = missing[i:i + config.EMBEDDING_BATCH]
            vectors = _compute(lambda c: embedding.embed(c, model), chunk)
            with conn:   # 每批算完就寫，中途斷掉已算的不會白算
                conn.executemany("INSERT OR REPLACE INTO embedding (model, text, vector) VALUES (?, ?, ?)",
                                 [(model, t, v.tobytes()) for t, v in zip(chunk, vectors)])
            found.update(zip(chunk, vectors))
    finally:
        conn.close()
    return _normalize([found[t] for t in texts])


def _compute(fn, texts: list[str]) -> list[np.ndarray]:
    out = []
    for i in range(0, len(texts), config.EMBEDDING_BATCH):
        out += [np.asarray(v, dtype=np.float32) for v in fn(texts[i:i + config.EMBEDDING_BATCH])]
    return out


def _normalize(vectors: list[np.ndarray]) -> np.ndarray:
    m = np.stack(vectors)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def rank(env: Env, query: str, items: list, text_of, id_of) -> list:
    """照跟 query 的餘弦相似度排；一樣時照 id。"""
    if not items:
        return []
    vectors = embed(env, [query_text(query), *map(text_of, items)])
    scores = vectors[1:] @ vectors[0]
    order = sorted(range(len(items)), key=lambda i: (-float(scores[i]), id_key(id_of(items[i]))))
    return [items[i] for i in order]


def plain_order(idx: Index, items: list, unknown_of, article_of, id_of) -> list:
    """沒有 query：時間不明的維度少的在前，再來發文時間新的在前，最後照 id。"""
    items = sorted(items, key=lambda x: id_key(id_of(x)))
    items = sorted(items, key=lambda x: idx.posted(article_of(x)), reverse=True)
    return sorted(items, key=lambda x: len(unknown_of(x)))


def page_text(total: int, limit: int, offset: int, unit: str) -> str:
    if total == 0:
        return "沒有資料"
    if offset >= total:
        return f"offset={offset} 超過總數，沒有資料"
    end = min(total, offset + limit)
    return f"第 {end} {unit}" if end == offset + 1 else f"第 {offset + 1}–{end} {unit}"


def more_line(total: int, limit: int, offset: int, unit: str) -> list[str]:
    rest = total - (offset + limit)
    return [f"還有 {rest} {unit}：offset={offset + limit}"] if rest > 0 else []


def spread(labels: list[str]) -> str:
    counter = Counter(labels)
    top = counter.most_common(config.PLACE_SPREAD)
    rest = counter.most_common()[config.PLACE_SPREAD:]
    text = "、".join(f"{k} {v}" for k, v in top)
    if rest:
        text += f"；另外 {len(rest)} 個地點 {sum(v for _, v in rest)} 筆"
    return "地點分布：" + text


def marks(unknown: list[str], *flags: str) -> str:
    out = [f"時間不明：{'、'.join(unknown)}"] if unknown else []
    return "".join(f"｜{x}" for x in [*out, *(f for f in flags if f)])


def head(*parts: str) -> str:
    return "｜".join(p for p in parts if p)


# ---------- find_place ----------

FIND_PLACE = """找地點的 id。題目或經驗裡出現開頭沒給 id 的地點時用。回傳候選地點和各自有幾筆經驗（點：地點的經驗；段：起點或終點是它的移動經驗；天：別人的行程有去它的天數）。"""

FIND_PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "開頭地點列表的編號（p3）。有給就不用填其他欄位"},
        "name_zh": {"type": "string", "description": "繁中名稱"},
        "name_ja": {"type": "string", "description": "日文名稱，寫地圖上最可能登記的寫法"},
        "city": {"type": "string", "description": "所在城市的日文名稱（東京、京都）；判斷不出來留空"},
        "kind": {"type": "string", "enum": [SPOT, AREA],
                 "description": "景點或區域。區域只能是行政區；車站、機場、飯店、商場、商店街都是景點"},
    },
}


def place_line(idx: Index, pid: int) -> str:
    p = idx.places[pid]
    base = f"id {p.id}｜{p.name_zh or '—'}｜{p.name_ja}｜{p.city or '城市不明'}"
    if p.kind == SPOT:
        a, b, c = idx.counts({pid})
        return f"{base}｜景點｜點 {a} 筆、段 {b} 筆、天 {c} 天"
    if not p.polygons:
        a, b, c = idx.counts({pid})
        return f"{base}｜區域（沒有範圍）｜點 {a}、段 {b}、天 {c}"
    a, b, c = idx.counts({pid})
    members = idx.members(pid)
    x, y, z = idx.counts(members)
    return f"{base}｜區域（有範圍）｜本身：點 {a}、段 {b}、天 {c}｜範圍裡 {len(members)} 個地點：點 {x}、段 {y}、天 {z}"


def _containing(idx: Index, names: list[str]) -> str:
    hits = [p for p in sorted(idx.places.values(), key=lambda p: p.id)
            if any(n and (n in (p.name_zh or "") or n in p.name_ja) for n in names)][:config.PLACE_CANDIDATES]
    if not hits:
        return "資料庫裡也沒有名稱包含它的地點。"
    return "資料庫裡名稱包含它的地點：\n" + "\n".join(place_line(idx, p.id) for p in hits)


async def _geocode(env: Env, p: PlaceRef) -> int | None:
    if env.geocoder is None:
        return None
    result = await env.geocoder.resolve(p, Context(article_id=env.article_id))
    env.reload()
    return result.place_id


async def find_place(env: Env, args: dict) -> str:
    ref = (args.get("ref") or "").strip()
    if ref:
        up = env.places.get(ref)
        if up is None:
            raise ToolError(f"ref：開頭地點列表沒有 {ref}（有的是 {'、'.join(env.places) or '（沒有）'}）")
        if up.status == "還沒解析":
            up.place_id = await _geocode(env, up.place_ref())
            up.status = "已解析" if up.place_id is not None else "解析不到"
        if up.place_id is None:
            idx = env.index
            raise ToolError(f"{ref} {up.name_zh} 解析不到。可以改用 name_zh、name_ja 換個寫法找。\n"
                            + _containing(idx, [up.name_zh, up.name_ja]))
        return f"{ref} → 1 個\n{place_line(env.index, up.place_id)}"

    name_zh, name_ja = (args.get("name_zh") or "").strip(), (args.get("name_ja") or "").strip()
    city, kind = (args.get("city") or "").strip(), args.get("kind")
    if not (name_zh or name_ja):
        raise ToolError("沒有 ref 時，name_zh、name_ja 至少要填一個")
    if kind not in (SPOT, AREA):
        raise ToolError("沒有 ref 時，kind 要填「景點」或「區域」")
    label = name_zh or name_ja
    idx = env.index
    names = {n for n in (name_zh, name_ja) if n}
    ids = {p.id for p in idx.places.values() if names & {p.name_zh, p.name_ja}}
    for table in ("place_alias_zh", "place_alias_ja"):
        ids |= {r[0] for r in env.conn.execute(
            f"SELECT id FROM {table} WHERE alias IN ({','.join('?' * len(names))})", tuple(names))}
    if city:
        ids = {i for i in ids if idx.places[i].city == city}
    if ids:
        shown = sorted(ids)[:config.PLACE_CANDIDATES]
        more = f"（另外 {len(ids) - len(shown)} 個沒列）" if len(ids) > len(shown) else ""
        return "\n".join([f"「{label}」→ {len(ids)} 個（資料庫裡同名的）{more}",
                          *(place_line(idx, i) for i in shown)])

    key = (name_zh, name_ja, city, kind)
    if key not in env.resolved:
        p = PlaceRef(ref=f"q{len(env.resolved) + 1}", name_zh=name_zh or name_ja, name_ja=name_ja or name_zh,
                     city=city, kind=kind, located=LOCATED_YES, sentence=env.question)
        env.resolved[key] = await _geocode(env, p)
    pid = env.resolved[key]
    if pid is None:
        raise ToolError(f"找不到「{label}」。\n" + _containing(env.index, list(names)))
    return f"「{label}」→ 1 個（新解析）\n{place_line(env.index, pid)}"


# ---------- list_places ----------

LIST_PLACES = """列出一個範圍裡的地點和各自有幾筆經驗。用來在區域裡或某地附近找地方，或在某個地點查不到資料時，找附近有資料的地點。"""

LIST_PLACES_SCHEMA = {
    "type": "object",
    "properties": {
        "within": {"type": "object", "description":
                   '三選一：{"area": id}（區域範圍裡）、{"around": id, "km": 半徑}（周圍的景點；走路到得了的通常 0.5–1 公里，'
                   '搭車或開車到得了的大得多）、{"city": "京都"}（城市名稱完全相同）',
                   "properties": {"area": {"type": "integer"}, "around": {"type": "integer"},
                                  "km": {"type": "number"}, "city": {"type": "string"}}},
        "kind": {"type": "string", "enum": [SPOT, AREA], "description": "只列景點或區域；省略是都要"},
        "has": {"type": "string", "enum": ["點", "段", "天", "任一", "不限"],
                "description": "至少有哪種經驗：點、段、天、任一（預設）、不限（沒有經驗的也列）"},
        "sort": {"type": "string", "enum": ["經驗數", "距離"],
                 "description": "經驗數（預設，點＋段＋天多的在前）或距離（只有 around 能用）"},
        "limit": {"type": "integer", "description": f"回幾個，預設 {config.LIST_LIMIT}、最多 {config.LIST_MAX}"},
        "offset": {"type": "integer", "description": "從第幾個開始，預設 0"},
    },
    "required": ["within"],
}


def list_places(env: Env, args: dict) -> str:
    idx = env.index
    within = args.get("within")
    keys = [k for k in ("area", "around", "city") if isinstance(within, dict) and k in within]
    if len(keys) != 1:
        raise ToolError('within 要是 {"area": id}、{"around": id, "km": 半徑} 或 {"city": "京都"} 其中一個')
    kind, has, sort = args.get("kind"), args.get("has") or "任一", args.get("sort") or "經驗數"
    if kind not in (None, SPOT, AREA):
        raise ToolError("kind 只能是「景點」「區域」或省略")
    if has not in ("點", "段", "天", "任一", "不限"):
        raise ToolError("has 只能是 點、段、天、任一、不限")
    if sort not in ("經驗數", "距離"):
        raise ToolError("sort 只能是 經驗數 或 距離")
    if sort == "距離" and keys[0] != "around":
        raise ToolError("sort=距離 只有 within 用 around 時能用")
    limit, offset = paging(args, config.LIST_LIMIT, config.LIST_MAX)

    note, center = "", None
    if keys[0] == "area":
        p = idx.place(within["area"], "within.area")
        if p.kind != AREA:
            raise ToolError(f'within.area：id {p.id} {p.name} 是景點不是區域；可以改用 {{"around": {p.id}, "km": 半徑}}')
        ids = idx.members(p.id)
        scope = f"{p.name}範圍裡"
        if not p.polygons:
            note = f'{p.name}沒有範圍，只算它自己；可以改用 {{"city": "{p.city}"}}'
    elif keys[0] == "around":
        center = idx.place(within["around"], "within.around")
        km = within.get("km")
        if not isinstance(km, (int, float)) or isinstance(km, bool) or km <= 0:
            raise ToolError("within.km 要是大於 0 的數字（公里）")
        ids = idx.around(center.id, float(km))
        scope = f"{center.name}周圍 {km:g} 公里"
    else:
        city = within["city"]
        if not isinstance(city, str) or not city.strip():
            raise ToolError("within.city 要是城市名稱")
        ids = frozenset(p.id for p in idx.places.values() if p.city == city.strip())
        scope = f"城市「{city.strip()}」裡"

    rows = []
    for pid in ids:
        p = idx.places[pid]
        if kind and p.kind != kind:
            continue
        counts = idx.counts(idx.members(pid))
        dist = km_between(center, p) if center else None
        rows.append((p, counts, dist))
    in_range, with_exp = len(rows), sum(1 for _, c, _ in rows if any(c))
    pick = {"點": lambda c: c[0] > 0, "段": lambda c: c[1] > 0, "天": lambda c: c[2] > 0,
            "任一": any, "不限": lambda c: True}[has]
    rows = [r for r in rows if pick(r[1])]
    if sort == "距離":
        rows.sort(key=lambda r: (r[2], r[0].id))
    else:
        rows.sort(key=lambda r: (-sum(r[1]), r[0].id))
    total = len(rows)
    filt = {"任一": "", "不限": "（含沒有經驗的）", "點": "（有點的經驗）", "段": "（有段的經驗）", "天": "（有天的經驗）"}[has]
    lines = [f"{scope} {in_range} 個{kind or '地點'}，有經驗的 {with_exp} 個；"
             f"列出{filt} {total} 個，照{sort}排，{page_text(total, limit, offset, '個')}"]
    if note:
        lines.append(note)
    for p, (a, b, c), dist in rows[offset:offset + limit]:
        line = f"id {p.id}｜{p.name}｜{p.kind}｜點 {a}、段 {b}、天 {c}"
        if dist is not None:
            line += f"｜{round(dist, 1)} 公里"
        lines.append(line)
    lines += more_line(total, limit, offset, "個")
    return "\n".join(lines)


# ---------- 站（measure、search_days 共用） ----------

@dataclass
class Stop:
    place_id: int | None
    name: str
    stay: bool = False


def expand_stops(env: Env, stops: Any, least: int) -> tuple[list[Stop], list[str]]:
    """place id 或使用者行程的天（"D3"）展開成站；回傳站和展開了哪些天。"""
    if not isinstance(stops, list) or not stops:
        raise ToolError(f'stops 要是陣列，每個是 place id 或使用者行程的天（"D3"），至少 {least} 個')
    idx, out, codes = env.index, [], []
    for s in stops:
        if isinstance(s, str) and re.fullmatch(r"[Dd]\d+", s.strip()):
            code = s.strip().upper()
            day = env.days.get(code)
            if day is None:
                raise ToolError(f"stops：使用者的行程沒有 {code}（有的是 {'、'.join(env.days) or '（沒有按天排的行程）'}）")
            codes.append(code)
            out += [Stop(st.place_id, idx.places[st.place_id].name if st.place_id in idx.places else st.name,
                         st.type == STAY) for st in day.stops]
        elif isinstance(s, int) and not isinstance(s, bool):
            out.append(Stop(s, idx.place(s, "stops").name))
        else:
            raise ToolError(f'stops：{json.dumps(s, ensure_ascii=False)} 不是 place id，也不是使用者行程的天（"D3"）')
    if len(out) < least:
        raise ToolError(f"stops 展開後只有 {len(out)} 站，至少要 {least} 站")
    return out, codes


def stop_name(s: Stop) -> str:
    return s.name + ("（住宿）" if s.stay else "") + ("（解析不到）" if s.place_id is None else "")


# ---------- measure ----------

MEASURE = """量一個排法：照順序每兩站的直線距離和合計，以及每站、每段在資料庫裡有幾筆經驗。評估行程時先用它看哪裡有資料、哪一段特別遠。它不查經驗內容；停留時間、移動時間要用 search_notes、search_moves 查，加總自己算。"""

MEASURE_SCHEMA = {
    "type": "object",
    "properties": {"stops": {"type": "array", "items": {"type": ["integer", "string"]},
                             "description": '照順序的站，每個是 place id 或使用者行程的天（"D3"，展開成那天照順序的站）。展開後至少 2 站'}},
    "required": ["stops"],
}


def measure(env: Env, args: dict) -> str:
    idx = env.index
    stops, codes = expand_stops(env, args.get("stops"), 2)
    order = " → ".join(stop_name(s) for s in stops)
    lines = [f"{'、'.join(codes)} 展開：{order}（{len(stops)} 站）" if codes else f"排法：{order}（{len(stops)} 站）", "站"]
    for i, s in enumerate(stops, 1):
        if s.place_id is None:
            lines.append(f"{i} {stop_name(s)}")
            continue
        p = idx.places[s.place_id]
        members = idx.members(p.id)
        extra = f"（範圍裡 {len(members)} 個地點）" if len(members) > 1 else ""
        lines.append(f"{i} id {p.id} {stop_name(s)}｜{p.kind}｜點 {idx.counts(members)[0]}{extra}")
    lines.append("段")
    total, skipped = 0.0, 0
    same = lambda a, b: (a.place_id == b.place_id) if a.place_id is not None or b.place_id is not None else a.name == b.name
    i = 0
    while i < len(stops) - 1:
        j = i + 1
        while j < len(stops) and same(stops[i], stops[j]):
            j += 1
        if j == len(stops):
            break
        a, b = stops[i], stops[j]
        text = f"{i + 1}→{j + 1} {a.name} → {b.name}"
        if a.place_id is None or b.place_id is None:
            lines.append(f"{text}｜沒有座標")
            skipped += 1
        else:
            pa, pb = idx.places[a.place_id], idx.places[b.place_id]
            if AREA in (pa.kind, pb.kind):
                dist = "區域，不算距離"
                skipped += 1
            else:
                km = round(km_between(pa, pb), 1)
                total += km
                dist = f"直線 {km} 公里"
            ma, mb = idx.members(pa.id), idx.members(pb.id)
            fwd = sum(1 for m in idx.moves if m.origin in ma and m.dest in mb)
            back = sum(1 for m in idx.moves if m.origin in mb and m.dest in ma)
            lines.append(f"{text}｜{dist}｜段：正方向 {fwd}、反方向 {back}")
        i = j
    lines.append(f"直線距離合計 {round(total, 1)} 公里" + (f"（有 {skipped} 段沒算）" if skipped else ""))
    return "\n".join(lines)


# ---------- search_notes ----------

PLACE_COND_DESC = ('地點條件。{"mode": "地點", "place_id": id}：景點只取它自己，區域取區域範圍裡的地點。'
                   '{"mode": "周圍", "place_id": id, "km": 半徑}：中心周圍半徑幾公里內的景點，加上中心本身；'
                   '走路到得了的通常 0.5–1 公里，搭車或開車到得了的大得多。{"mode": "不限"}：全部地點，包括解析不到的經驗')
PLACE_COND_SCHEMA = {"type": "object", "description": PLACE_COND_DESC, "properties": {
    "mode": {"type": "string", "enum": ["地點", "周圍", "不限"]},
    "place_id": {"type": "integer"}, "km": {"type": "number"}}, "required": ["mode"]}

TIME_DESC = ("時間條件，每個欄位都可以省略，省略就是不限。只填題目說了、或從日期推得出來的條件，題目沒說的不要自己加。"
             "year：只要這一年以後的經驗；months：月份 1–12；day_type：平日或假日（週末和國定假日都算假日）；"
             "hours：鐘點範圍 0–23，start > end 是跨夜（22 到 2）")
TIME_SCHEMA = {"type": "object", "description": TIME_DESC, "properties": {
    "year": {"type": "integer"}, "months": {"type": "array", "items": {"type": "integer"}},
    "day_type": {"type": "string", "enum": [WEEKDAY, HOLIDAY]},
    "hours": {"type": "array", "items": {"type": "object", "properties": {
        "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["start", "end"]}}}}
TIME_YM_DESC = "時間條件，只能有 year（只要這一年以後的）、months（1–12），省略就是不限。只填題目說了、或從日期推得出來的條件"
TIME_YM_SCHEMA = {"type": "object", "description": TIME_YM_DESC, "properties": {
    "year": {"type": "integer"}, "months": {"type": "array", "items": {"type": "integer"}}}}


def _paging_schema(default: int, most: int) -> dict:
    return {"limit": {"type": "integer", "description": f"回幾筆，預設 {default}、最多 {most}"},
            "offset": {"type": "integer", "description": "從第幾筆開始，預設 0"}}


SEARCH_NOTES = """查地點的經驗（人潮、排隊、停留多久、設施、值不值得……）。回傳經驗原文和時間；第一行有筆數，查不到時看「不看時間的分布」決定要不要放寬。"""

SEARCH_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "place": PLACE_COND_SCHEMA,
        "time": TIME_SCHEMA,
        "query": {"type": "string", "description": "想找什麼（「排隊時間」「帶嬰兒車方不方便」），用來排序。place 是不限時必填"},
        **_paging_schema(config.LIST_LIMIT, config.LIST_MAX),
    },
    "required": ["place"],
}


def note_line(idx: Index, n: Note, unknown: list[str]) -> str:
    name = idx.places[n.place].name if n.poi_id is not None else n.poi_name
    return (f"[place_note {n.id}] {name}｜{idx.posted_year(n.article_id)} 發文｜{render_rows(n.times)}"
            f"{marks(unknown, '名稱比對' if n.by_name else '')}｜{n.content}")


def search_notes(env: Env, args: dict) -> str:
    idx = env.index
    cond = place_cond(idx, args.get("place"), "place")
    time = parse_time(args.get("time"))
    query = query_arg(args, cond.ids is None, 'place 是 {"mode": "不限"} 時要給 query')
    limit, offset = paging(args, config.LIST_LIMIT, config.LIST_MAX)

    pool = [n for n in idx.notes if cond.ok(n.place)]
    unknown = {}
    for n in pool:
        u = match_time(n.times, time, idx.posted_year(n.article_id))
        if u is not None:
            unknown[n.id] = u
    hits = [n for n in pool if n.id in unknown]
    if query:
        hits = rank(env, query, hits, lambda n: f"{n.poi_name}：{n.content}", lambda n: n.id)
    else:
        hits = plain_order(idx, hits, lambda n: unknown[n.id], lambda n: n.article_id, lambda n: n.id)
    lines = [head(cond.label, time.label(), f"「{query}」" if query else "",
                  f"時間相符 {len(hits)} 筆（不看時間 {len(pool)} 筆）；{page_text(len(hits), limit, offset, '筆')}")]
    if time.limited and len(pool) > len(hits):
        lines.append(distribution([n.times for n in pool]))
    if cond.many and hits:
        lines.append(spread([idx.places[n.place].name if n.place is not None else n.poi_name for n in hits]))
    lines += [note_line(idx, n, unknown[n.id]) for n in hits[offset:offset + limit]]
    lines += more_line(len(hits), limit, offset, "筆")
    return "\n".join(lines)


# ---------- search_moves ----------

SEARCH_MOVES = """查從一地到另一地的移動經驗（怎麼去、要多久、轉乘、路況、擁擠、帶行李）。起點、終點可以有一端不限。"""

MOVE_AROUND_DESC = "。這裡的周圍另外包含中心所在的區域（例如車站所在的城市），因為移動的起訖常寫成城市"

SEARCH_MOVES_SCHEMA = {
    "type": "object",
    "properties": {
        "origin": {**PLACE_COND_SCHEMA, "description": "起點的" + PLACE_COND_DESC + MOVE_AROUND_DESC},
        "dest": {**PLACE_COND_SCHEMA, "description": "終點的" + PLACE_COND_DESC + MOVE_AROUND_DESC},
        "both_ways": {"type": "boolean", "description":
                      "true 時，從終點到起點的經驗也回傳，標「方向相反」；預設 false。移動時間、交通方式通常兩個方向差不多；人潮、上下坡、塞車就要看方向"},
        "time": TIME_SCHEMA,
        "query": {"type": "string", "description": "想找什麼，用來排序。起點、終點都是不限時必填"},
        **_paging_schema(config.LIST_LIMIT, config.LIST_MAX),
    },
    "required": ["origin", "dest"],
}


def move_name(idx: Index, pid: int | None, raw_id: int | None, raw: str) -> str:
    return idx.places[pid].name if raw_id is not None else raw


def move_line(idx: Index, m: MoveRow, unknown: list[str], reverse: bool) -> str:
    route = f"{move_name(idx, m.origin, m.from_id, m.from_name)} → {move_name(idx, m.dest, m.to_id, m.to_name)}"
    return (f"[move {m.id}] {route}｜{idx.posted_year(m.article_id)} 發文｜{render_rows(m.times)}"
            f"{marks(unknown, '名稱比對' if m.by_name else '', '方向相反' if reverse else '')}｜{m.content}")


def search_moves(env: Env, args: dict) -> str:
    idx = env.index
    origin = place_cond(idx, args.get("origin"), "origin", with_areas=True)
    dest = place_cond(idx, args.get("dest"), "dest", with_areas=True)
    both = args.get("both_ways", False)
    if not isinstance(both, bool):
        raise ToolError("both_ways 要是 true 或 false")
    time = parse_time(args.get("time"))
    query = query_arg(args, origin.ids is None and dest.ids is None, "起點、終點都是不限時要給 query")
    limit, offset = paging(args, config.LIST_LIMIT, config.LIST_MAX)

    forward = [m for m in idx.moves if origin.ok(m.origin) and dest.ok(m.dest)]
    fwd_ids = {m.id for m in forward}
    backward = [m for m in idx.moves if m.id not in fwd_ids and origin.ok(m.dest) and dest.ok(m.origin)]
    if origin.ids is None and dest.ids is None:
        backward = []
    reverse_ids = {m.id for m in backward}
    pool = forward + (backward if both else [])

    def timed(items):
        out = {}
        for m in items:
            u = match_time(m.times, time, idx.posted_year(m.article_id))
            if u is not None:
                out[m.id] = u
        return out

    unknown = timed(pool)
    hits = [m for m in pool if m.id in unknown]
    if query:
        hits = rank(env, query, hits, lambda m: f"{m.from_name} → {m.to_name}：{m.content}", lambda m: m.id)
    else:
        hits = plain_order(idx, hits, lambda m: unknown[m.id], lambda m: m.article_id, lambda m: m.id)
    first = head(f"{origin.label} → {dest.label}", time.label(), f"「{query}」" if query else "",
                 f"時間相符 {len(hits)} 筆（不看時間 {len(pool)} 筆）；{page_text(len(hits), limit, offset, '筆')}")
    if not both and backward:
        first += f"；反方向另有 {len(timed(backward))} 筆（both_ways=true 可以一起看）"
    lines = [first]
    if time.limited and len(pool) > len(hits):
        lines.append(distribution([m.times for m in pool]))
    if (origin.many or dest.many) and hits:
        lines.append(spread([f"{move_name(idx, m.origin, m.from_id, m.from_name)} → "
                             f"{move_name(idx, m.dest, m.to_id, m.to_name)}" for m in hits]))
    lines += [move_line(idx, m, unknown[m.id], m.id in reverse_ids) for m in hits[offset:offset + limit]]
    lines += more_line(len(hits), limit, offset, "筆")
    return "\n".join(lines)


# ---------- search_days ----------

SEARCH_DAYS = """找別人有去這些地點的那幾天，列出照順序的站（標出住宿）、對那天和對整趟的評語。用來看別人怎麼排、同一天還去哪、住哪、覺得趕不趕。相同的站越多排越前面；不比距離，也不比順序。"""

SEARCH_DAYS_SCHEMA = {
    "type": "object",
    "properties": {
        "stops": {"type": "array", "items": {"type": ["integer", "string"]},
                  "description": '拿來比的站，每個是 place id 或使用者行程的天（"D3"），至少 1 個'},
        "time": TIME_YM_SCHEMA,
        **_paging_schema(config.DAY_LIMIT, config.DAY_MAX),
    },
    "required": ["stops"],
}


def compare_sets(idx: Index, ids: list[int]) -> list[tuple[int, frozenset[int]]]:
    """拿來比的每一站 → 算它相同的地點（景點是自己，區域是範圍裡的）。"""
    return [(pid, idx.members(pid)) for pid in ids]


def day_months(idx: Index, day_ids: list[str]) -> list[TimeRow]:
    return [TimeRow(month=idx.days[d].month) for d in day_ids if d in idx.days]


def day_stop_line(idx: Index, d: DayRow, hit: set[int] | None = None) -> str:
    parts = []
    for s in d.stops:
        name = idx.places[s.place].name if s.poi_id is not None else s.poi_name
        star = "*" if hit and s.place in hit else ""
        parts.append(f"{name}{star}" + ("（住宿）" if s.type == STAY else ""))
    return " → ".join(parts)


def search_days(env: Env, args: dict) -> str:
    idx = env.index
    stops, _ = expand_stops(env, args.get("stops"), 1)
    time = parse_time(args.get("time"), only_year_month=True)
    limit, offset = paging(args, config.DAY_LIMIT, config.DAY_MAX)
    dropped = [s.name for s in stops if s.place_id is None]
    ids = list(dict.fromkeys(s.place_id for s in stops if s.place_id is not None))
    if not ids:
        raise ToolError("stops 裡沒有解析到的站可以比；先用 find_place 找地點的 id")
    sets = compare_sets(idx, ids)
    names = [idx.places[i].name for i in ids]

    scored = []        # (day, 相同幾站, 標星的地點, 時間不明)
    any_same = 0
    for d in idx.days.values():
        places = {s.place for s in d.stops if s.place is not None}
        same = [pid for pid, members in sets if places & members]
        if not same:
            continue
        any_same += 1
        u = match_time([TimeRow(year=idx.posted_year(d.article_id), month=d.month)], time,
                       idx.posted_year(d.article_id))
        if u is None:
            continue
        hit = set().union(*(members for pid, members in sets if pid in same)) & places
        scored.append((d, len(same), hit, u))
    scored.sort(key=lambda x: (day_no(x[0].id), id_key(x[0].id)))
    scored.sort(key=lambda x: idx.posted(x[0].article_id), reverse=True)
    scored.sort(key=lambda x: -x[1])

    first = head(f"排法：{'、'.join(names)}（{len(names)} 站）", time.label(),
                 f"至少一站相同、時間相符 {len(scored)} 天（不看時間 {any_same} 天）；{page_text(len(scored), limit, offset, '天')}")
    lines = [first]
    if dropped:
        lines.append(f"解析不到、不比：{'、'.join(dropped)}")
    per = [f"{idx.places[pid].name} {len(set().union(*(idx.day_ids.get(i, set()) for i in members)))}"
           for pid, members in sets]
    lines.append("每站有幾天去過（不看時間）：" + "、".join(per))

    seen: set[str] = set()
    for d, n, hit, u in scored[offset:offset + limit]:
        a = d.article_id
        total = len(idx.article_days.get(a, []))
        month = f"{d.month} 月" if d.month else "沒寫月份"
        lines += ["", f"[day {d.id}] 〈{idx.title(a)}〉{idx.posted(a)[:10]} 發文｜第 {day_no(d.id)} 天／共 {total} 天｜"
                      f"{month}{marks(u)}｜相同 {n} 站",
                  f"  {day_stop_line(idx, d, hit)}"]
        today = [x for x in idx.day_notes if d.id in x.day_ids]
        whole = [x for x in idx.day_notes if x.article_id == a and not x.day_ids]
        lines += _day_note_lines(idx, "這天的評語", today, seen)
        lines += _day_note_lines(idx, "整趟的評語", whole, seen)
    lines += more_line(len(scored), limit, offset, "天")
    return "\n".join(lines)


def _day_note_lines(idx: Index, label: str, notes: list[DayNoteRow], seen: set[str]) -> list[str]:
    if not notes:
        return [f"  {label}：（沒有）"]
    out = []
    for x in sorted(notes, key=lambda x: id_key(x.id)):
        span = f"（第 {number_ranges([day_no(i) for i in x.day_ids])} 天）" if len(x.day_ids) > 1 else ""
        body = "見上" if x.id in seen else x.content
        seen.add(x.id)
        out.append(f"  {label}：[day_note {x.id}]{span} {body}")
    return out


# ---------- search_day_notes ----------

SEARCH_DAY_NOTES = """用語意找對行程的評語（太趕、太累、節奏、天數夠不夠、某種安排的心得）。跟特定地點無關、或想找某種感想時用。"""

SEARCH_DAY_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "想找什麼，必填"},
        "places": {"type": "array", "items": {"type": "integer"}, "description":
                   "place id 的陣列，可以省略。有給時，只留講到的天裡有其中一個地點的評語（講整趟的看那篇所有的天）"},
        "time": TIME_YM_SCHEMA,
        **_paging_schema(config.LIST_LIMIT, config.LIST_MAX),
    },
    "required": ["query"],
}


def search_day_notes(env: Env, args: dict) -> str:
    idx = env.index
    query = query_arg(args, True, "寫想找什麼樣的評語")
    time = parse_time(args.get("time"), only_year_month=True)
    limit, offset = paging(args, config.LIST_LIMIT, config.LIST_MAX)
    places = args.get("places")
    sets = None
    if places is not None:
        if not isinstance(places, list) or not places:
            raise ToolError("places 要是 place id 的陣列；不限地點就省略")
        sets = compare_sets(idx, [idx.place(p, "places").id for p in places])

    unknown: dict[str, list[str]] = {}
    pool = []
    for x in idx.day_notes:
        days = x.day_ids or idx.article_days.get(x.article_id, [])
        if sets is not None:
            here = {s.place for d in days if d in idx.days for s in idx.days[d].stops if s.place is not None}
            if not any(here & members for _, members in sets):
                continue
        pool.append(x)
        u = match_time(day_months(idx, days), time, idx.posted_year(x.article_id))
        if u is not None:
            unknown[x.id] = u
    hits = [x for x in pool if x.id in unknown]
    hits = rank(env, query, hits, lambda x: x.content, lambda x: x.id)
    where = "地點 " + "、".join(idx.places[p].name for p, _ in sets) if sets else ""
    lines = [head(f"「{query}」", where, time.label(),
                  f"時間相符 {len(hits)} 筆" + (f"（不看時間 {len(pool)} 筆）" if time.limited else "")
                  + f"；照相關程度{page_text(len(hits), limit, offset, '筆')}")]
    for x in hits[offset:offset + limit]:
        lines.append(day_note_line(idx, x, unknown[x.id]))
    lines += more_line(len(hits), limit, offset, "筆")
    return "\n".join(lines)


def day_note_span(idx: Index, x: DayNoteRow, with_stops: bool) -> str:
    if not x.day_ids:
        return f"整趟（共 {len(idx.article_days.get(x.article_id, []))} 天）"
    if len(x.day_ids) == 1 and with_stops and x.day_ids[0] in idx.days:
        return f"第 {day_no(x.day_ids[0])} 天：{day_stop_line(idx, idx.days[x.day_ids[0]])}"
    return f"第 {number_ranges([day_no(i) for i in x.day_ids])} 天"


def day_note_line(idx: Index, x: DayNoteRow, unknown: list[str]) -> str:
    days = x.day_ids or idx.article_days.get(x.article_id, [])
    months = sorted({idx.days[d].month for d in days if d in idx.days and idx.days[d].month})
    month = "、".join(map(str, months)) + " 月" if months else "沒寫月份"
    return (f"[day_note {x.id}] 〈{idx.title(x.article_id)}〉{idx.posted_year(x.article_id)} 發文｜"
            f"{day_note_span(idx, x, True)}｜{month}{marks(unknown)}｜{x.content}")


# ---------- read_article ----------

READ_ARTICLE = """讀一篇文章在資料庫裡的全部內容：每一天的站、所有評語、地點和移動的經驗。某筆經驗要看前後文（作者跟誰去、整趟幾天、那天前後怎麼排）時用。"""

READ_ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {"ref": {"type": "string", "description":
                           "文章 id（M.1767434058.A.E80），或文章裡任何一筆的編號（place_note 1767434058-3、day 1767434058-3）"}},
    "required": ["ref"],
}

_TABLES = {"place_note": "place_note", "move": "move", "day": "day", "day_note": "day_note"}


def read_article(env: Env, args: dict) -> str:
    idx = env.index
    ref = (args.get("ref") or "").strip().strip("[]").strip()
    if not ref:
        raise ToolError("ref 必填：文章 id 或一筆的編號（place_note 1767434058-3）")
    if ref in idx.articles:
        article_id = ref
    else:
        m = re.fullmatch(r"(place_note|move|day|day_note)\s+(\d+-\d+)", ref)
        if not m:
            raise ToolError(f"ref：看不懂「{ref}」；寫文章 id（M.1767434058.A.E80）或「表名 編號」（day 1767434058-3）")
        row = env.conn.execute(f"SELECT article_id FROM {_TABLES[m.group(1)]} WHERE id = ?", (m.group(2),)).fetchone()
        if row is None:
            raise ToolError(f"ref：{m.group(1)} {m.group(2)} 不存在")
        article_id = row[0]
    day_ids = idx.article_days.get(article_id, [])
    lines = [f"〈{idx.title(article_id)}〉{article_id}｜{idx.posted(article_id)[:10]} 發文｜{len(day_ids)} 天", "天"]
    for d in day_ids:
        day = idx.days[d]
        month = f"{day.month} 月" if day.month else "沒寫月份"
        lines.append(f"[day {d}] 第 {day_no(d)} 天｜{month}｜{day_stop_line(idx, day)}")
    lines.append("評語")
    for x in sorted((x for x in idx.day_notes if x.article_id == article_id), key=lambda x: id_key(x.id)):
        lines.append(f"[day_note {x.id}] {day_note_span(idx, x, False)}｜{x.content}")
    lines.append("地點的經驗")
    for n in sorted((n for n in idx.notes if n.article_id == article_id), key=lambda n: id_key(n.id)):
        name = idx.places[n.place].name if n.poi_id is not None else n.poi_name
        lines.append(f"[place_note {n.id}] {name}｜{render_rows(n.times)}｜{n.content}")
    lines.append("移動的經驗")
    for m in sorted((m for m in idx.moves if m.article_id == article_id), key=lambda m: id_key(m.id)):
        route = f"{move_name(idx, m.origin, m.from_id, m.from_name)} → {move_name(idx, m.dest, m.to_id, m.to_name)}"
        lines.append(f"[move {m.id}] {route}｜{render_rows(m.times)}｜{m.content}")
    return "\n".join(lines)


# ---------- 工具清單 ----------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    run: Any          # (Env, dict) → str；find_place 是 async


TOOLS = [
    ToolSpec("find_place", FIND_PLACE, FIND_PLACE_SCHEMA, find_place),
    ToolSpec("list_places", LIST_PLACES, LIST_PLACES_SCHEMA, list_places),
    ToolSpec("measure", MEASURE, MEASURE_SCHEMA, measure),
    ToolSpec("search_notes", SEARCH_NOTES, SEARCH_NOTES_SCHEMA, search_notes),
    ToolSpec("search_moves", SEARCH_MOVES, SEARCH_MOVES_SCHEMA, search_moves),
    ToolSpec("search_days", SEARCH_DAYS, SEARCH_DAYS_SCHEMA, search_days),
    ToolSpec("search_day_notes", SEARCH_DAY_NOTES, SEARCH_DAY_NOTES_SCHEMA, search_day_notes),
    ToolSpec("read_article", READ_ARTICLE, READ_ARTICLE_SCHEMA, read_article),
]
