"""問答系統測試用的假實作和資料：都不打網路。

- `World`：往記憶體資料庫寫地點、文章、經驗（place_note、move、day、day_note）。
- `FakeJEV`：相關判斷；內容有 `★` 的機率 0.9（通過），其他 0.1。
- `FakeEmbedder`：內容有 `●` 的向量是 (1, 0)，其他是 (0, 1)；查詢文字有 `●` 時，有 `●` 的經驗排在前面。
- `FakeLLM`：看輸出格式分辨拆題、③、⑦、整理重點、⑧；行程解析和地點解析交給 v4 抽取測試的假 llm。
- 節點的 LLM 輸出：`point`、`move`、`day`，地點條件 `at`、`around`、`ANY`，地點 `ref`、`new`，排法的站 `on`、`stop`。
"""
import json
import re
from dataclasses import dataclass, field
from itertools import count

from service import llm
from service.jev import JEVError, NoulAnswer
from tests.v4.extract import fakes as extract_fakes
from v4 import place as place_table
from v4.rag.data_schema import (AREA, NO, PLAN_SCHEMA, REPLAN_SCHEMA, SPLIT_SCHEMA, SPOT, SUMMARY_SCHEMA,
                                TODO_QUESTION)

USAGE = extract_fakes.USAGE
RELEVANT = "★"
NEAR = "●"
BASE_TS = 1767000000


def square(lat: float, lon: float, half: float, hole: float | None = None) -> dict:
    """以 (lat, lon) 為中心、半邊長 half 度的正方形範圍；hole 給了就在中間挖一個半邊長 hole 的洞。"""
    def ring(h):
        w, s, e, n = lon - h, lat - h, lon + h, lat + h
        return [[w, s], [e, s], [e, n], [w, n], [w, s]]
    return {"type": "Polygon", "coordinates": [ring(half)] + ([ring(hole)] if hole else [])}


@dataclass
class World:
    """往資料庫寫測試資料。文章 id 是 `M.<時間戳>.A.AAA`，經驗的 id 是 `<時間戳>-<第幾筆>`。"""
    conn: object
    _ids: count = field(default_factory=lambda: count(1))
    _ts: dict = field(default_factory=dict)
    _seq: dict = field(default_factory=dict)

    def place(self, name_zh: str, name_ja: str | None = None, *, lat: float = 35.7, lon: float = 139.8,
              kind: str = SPOT, boundary: dict | None = None, city: str = "東京", aliases=()) -> int:
        pid = place_table.add(self.conn, source="osm", source_id=f"test/{next(self._ids)}", kind=kind,
                              name_ja=name_ja or name_zh, name_zh=name_zh, city=city, lat=lat, lon=lon,
                              boundary=boundary)
        for a in aliases:
            place_table.add_alias(self.conn, pid, name_zh=a)
        return pid

    def area(self, name_zh: str, lat: float, lon: float, half: float, hole: float | None = None, **kw) -> int:
        return self.place(name_zh, lat=lat, lon=lon, kind=AREA, boundary=square(lat, lon, half, hole), **kw)

    def article(self, n: int, posted_at: str = "2025-10-01T10:00:00", title: str = "") -> str:
        ts = BASE_TS + n
        article_id = f"M.{ts}.A.AAA"
        with self.conn:
            self.conn.execute("INSERT INTO article (id, title, posted_at) VALUES (?, ?, ?)",
                              (article_id, title or f"文章 {n}", posted_at))
        self._ts[article_id] = ts
        self._seq[article_id] = count(1)
        return article_id

    def _next(self, article_id: str) -> str:
        return f"{self._ts[article_id]}-{next(self._seq[article_id])}"

    def _name(self, poi: int | str) -> tuple[int | None, str]:
        """地點：place id（名稱用它的繁中名稱），或解析不到的原文名稱。"""
        if isinstance(poi, int):
            return poi, place_table.get(self.conn, poi).name_zh
        return None, poi

    def note(self, article_id: str, poi: int | str, content: str, times=()) -> str:
        """place_note；times 每筆是 (year, month, day_type, time)，空的寫 None。"""
        note_id = self._next(article_id)
        pid, name = self._name(poi)
        with self.conn:
            self.conn.execute("INSERT INTO place_note (id, article_id, poi_id, poi_name, content) VALUES (?, ?, ?, ?, ?)",
                              (note_id, article_id, pid, name, content))
            self.conn.executemany("INSERT INTO place_note_time (note_id, year, month, day_type, time)"
                                  " VALUES (?, ?, ?, ?, ?)", [(note_id, *t) for t in times])
        return note_id

    def move(self, article_id: str, origin: int | str, dest: int | str, content: str, times=()) -> str:
        move_id = self._next(article_id)
        (a, an), (b, bn) = self._name(origin), self._name(dest)
        with self.conn:
            self.conn.execute("INSERT INTO move (id, article_id, from_id, to_id, from_name, to_name, content)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?)", (move_id, article_id, a, b, an, bn, content))
            self.conn.executemany("INSERT INTO move_time (move_id, year, month, day_type, time) VALUES (?, ?, ?, ?, ?)",
                                  [(move_id, *t) for t in times])
        return move_id

    def day(self, article_id: str, no: int, stops, month: str | None = None) -> str:
        """一天；stops 每筆是地點（place id 或原文名稱），或（地點, 景點／住宿）。"""
        day_id = f"{self._ts[article_id]}-{no}"
        with self.conn:
            self.conn.execute("INSERT INTO day (id, article_id, month) VALUES (?, ?, ?)", (day_id, article_id, month))
            for seq, s in enumerate(stops, 1):
                poi, type_ = s if isinstance(s, tuple) else (s, SPOT)
                pid, name = self._name(poi)
                self.conn.execute("INSERT INTO day_place (day_id, seq, poi_id, poi_name, type) VALUES (?, ?, ?, ?, ?)",
                                  (day_id, seq, pid, name, type_))
        return day_id

    def day_note(self, article_id: str, content: str, days=()) -> str:
        """day_note；days 是它講到的 day id，空的是講整趟。id 跟 place_note、move 用不同的編號（從 100 起）。"""
        n = sum(1 for _ in self.conn.execute("SELECT 1 FROM day_note WHERE article_id = ?", (article_id,)))
        note_id = f"{self._ts[article_id]}-{100 + n}"
        with self.conn:
            self.conn.execute("INSERT INTO day_note (id, article_id, content) VALUES (?, ?, ?)",
                              (note_id, article_id, content))
            self.conn.executemany("INSERT INTO day_note_day (note_id, day_id) VALUES (?, ?)",
                                  [(note_id, d) for d in days])
        return note_id


class FakeJEV:
    """跟 `service.jev.JEVClient` 一樣的介面。rule：送出的內容 → 機率；fail 給了就每次丟它。"""

    def __init__(self, rule=None, fail: Exception | None = None):
        self.rule = rule or (lambda passage: 0.9 if RELEVANT in passage else 0.1)
        self.fail = fail
        self.passages: list[str] = []

    async def noul(self, model, state, instructions, criteria, name="relevance") -> NoulAnswer:
        self.passages.append(state["passage"])
        if self.fail:
            raise self.fail
        return NoulAnswer(self.rule(state["passage"]), "fake-jev")

    async def aclose(self) -> None:
        pass


class FakeEmbedder:
    model = "fake-embedding"

    def __init__(self):
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts += texts
        return [[1.0, 0.0] if NEAR in t else [0.0, 1.0] for t in texts]


# ---------- 假的 LLM ----------

def cite_first(user: str) -> dict:
    """整理重點的預設輸出：一則重點，引用輸入裡第一筆證據（沒有就引用第一天）。"""
    refs = re.findall(r'<evidence id="([^"]+)">', user) or re.findall(r'<day id="([^"]+)"', user)
    return summary(points=[{"text": "重點", "sources": refs[:1]}] if refs else [])


def summary(points=(), numbers=(), differences=(), todo=()) -> dict:
    return {"points": list(points), "numbers": list(numbers), "differences": list(differences), "todo": list(todo)}


def number(kind: str, lo: float, hi: float, source: str, unit: str = "分鐘", quote: str = "") -> dict:
    return {"kind": kind, "min": lo, "max": hi, "unit": unit, "quote": quote or f"{lo:g}", "source": source}


def todo_question(text: str, source: str) -> dict:
    return {"type": TODO_QUESTION, "text": text, "source": source, "name_zh": "", "name_ja": "", "city": "", "kind": ""}


class FakeLLM:
    """假的 `service.llm.call`。每一種可以給一個值、一個 list（照順序一次一個）或一個函式（拿到 user 訊息）。

    split（拆題）、plan（③）、replan（⑦，沒給是 `{"nodes": []}`）、summary（整理重點，沒給是 `cite_first`）、
    answer（answer.md）、general（general.md）。其他輸出格式（行程解析、地點解析）交給 `extract`
    （v4 抽取測試的 `FakeLLM`）。
    """

    def __init__(self, split=None, plan=None, replan=None, summary=None, answer="答案", general="一般知識的答案",
                 extract: extract_fakes.FakeLLM | None = None):
        self.sources = {"split": split, "plan": plan if plan is not None else {"nodes": []},
                        "replan": replan if replan is not None else {"nodes": []},
                        "summary": summary or cite_first, "answer": answer, "general": general}
        self.extract = extract or extract_fakes.FakeLLM()
        self.calls: list[tuple[str, str]] = []   # (哪一種, user 訊息)

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]

    def users(self, kind: str) -> list[str]:
        return [u for k, u in self.calls if k == kind]

    async def __call__(self, system, user, schema, model, effort=None, tools=None):
        kind = {id(SPLIT_SCHEMA): "split", id(PLAN_SCHEMA): "plan", id(REPLAN_SCHEMA): "replan",
                id(SUMMARY_SCHEMA): "summary"}.get(id(schema))
        if kind is None and schema is None:
            kind = "general" if "「其他」題" in system else "answer"
        if kind is None:
            self.calls.append(("extract", user))
            return await self.extract(system, user, schema, model, effort, tools)
        self.calls.append((kind, user))
        llm.record(USAGE)
        source = self.sources[kind]
        if callable(source):
            value = source(user)
        elif isinstance(source, list):
            value = source.pop(0)
        else:
            value = source
        return value if isinstance(value, str) else json.loads(json.dumps(value))


# ---------- 節點的 LLM 輸出 ----------

def ref(r: str) -> dict:
    """① 地點列表裡的地點。"""
    return {"ref": r, "name_zh": "", "name_ja": "", "city": "", "kind": ""}


def new(name_zh: str, name_ja: str, city: str = "東京", kind: str = SPOT) -> dict:
    """列表裡沒有的新地點。"""
    return {"ref": "", "name_zh": name_zh, "name_ja": name_ja, "city": city, "kind": kind}


EMPTY_PLACE = {"ref": "", "name_zh": "", "name_ja": "", "city": "", "kind": ""}
EMPTY = {"mode": "", "place": EMPTY_PLACE, "km": 0}
ANY = {"mode": "不限", "place": EMPTY_PLACE, "km": 0}


def at(place: dict) -> dict:
    return {"mode": "地點", "place": place, "km": 0}


def around(place: dict, km: float) -> dict:
    return {"mode": "周圍", "place": place, "km": km}


def on(day: str) -> dict:
    """排法的一站：使用者行程的一天（D1）。"""
    return {"day": day, "place": EMPTY_PLACE}


def stop(place: dict) -> dict:
    return {"day": "", "place": place}


def _node(kind, *, target=EMPTY, origin=EMPTY, dest=EMPTY, both_ways="", stops=(), year="", months=(),
          day_type="", hours=(), aspect="人潮", reason="理由", relax_from=None) -> dict:
    out = {"kind": kind, "target": target, "origin": origin, "dest": dest, "both_ways": both_ways,
           "stops": list(stops), "year": year, "months": list(months), "day_type": day_type,
           "hours": [{"start": a, "end": b} for a, b in hours], "aspect": aspect, "reason": reason}
    if relax_from is not None:
        out["relax_from"] = relax_from
    return out


def point(target: dict, **kw) -> dict:
    return _node("點", target=target, **kw)


def move(origin: dict, dest: dict, both_ways: str = NO, **kw) -> dict:
    return _node("段", origin=origin, dest=dest, both_ways=both_ways, **kw)


def day(stops=(), **kw) -> dict:
    return _node("天", stops=stops, **kw)


def nodes(*items) -> dict:
    return {"nodes": list(items)}


class JEVFail(JEVError):
    pass


@dataclass
class _Resolution:
    place_id: int | None


@dataclass
class _Trace:
    result: str
    fail_reason: str = ""
    used: str = ""


class FakeGeocoder:
    """只有 `resolve` 的假 Geocoder：日文名稱 → place id（沒有的解析不到）。asked 記下每次解析的日文名稱。"""

    def __init__(self, ids: dict[str, int]):
        self.ids = ids
        self.asked: list[str] = []

    async def resolve(self, p, ctx, on_done=None):
        self.asked.append(p.name_ja)
        pid = self.ids.get(p.name_ja)
        if on_done is not None:
            on_done(_Trace("解析到" if pid else "捨棄", "" if pid else "找不到"))
        return _Resolution(pid)
