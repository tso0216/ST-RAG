"""各步驟之間傳遞的資料結構。不依賴其他模組。

- 整理後的文章：`Article`、`Comment`
- 兩次 llm 解析的輸出格式：`LINE_SCHEMA`、`NOTE_SCHEMA`，只在這裡定義
- 解析結果（檢查過、兩次合起來）：`Parsed`，也是 `data/v4_pending/` 存的內容
- 地點解析：`Candidate`（本機地圖的一筆結果）、`Area`（搜尋範圍）、`Context`（地點在文章裡的上下文）、
  `Resolution`
- 執行結果：`Report`

llm 輸出裡「留空」一律是空字串。
"""
from dataclasses import asdict, dataclass, field
from pathlib import Path

SPOT = "景點"
AREA = "區域"
KINDS = (SPOT, AREA)

LOCATED_YES = "是"
LOCATED_GUESS = "猜的"
LOCATED_UNKNOWN = "猜不出來"
LOCATED = (LOCATED_YES, LOCATED_GUESS, LOCATED_UNKNOWN)

DAY_TYPES = ("平日", "假日")

# 行程裡一站的種類（`day_place.type`）
STAY = "住宿"
STOP_TYPES = (SPOT, STAY)

# ---------- 整理後的文章 ----------

@dataclass(frozen=True)
class Comment:
    no: int            # 合併後的編號，從 1 開始
    user: str
    content: str
    by_author: bool    # 原 po 的推文


@dataclass(frozen=True)
class Article:
    id: str            # M.1767013758.A.A9D
    title: str
    author: str
    posted_at: str     # 發文時間，ISO 格式（2025-12-29T21:09:16）
    text: str          # 主文
    comments: list[Comment]

    @property
    def posted_date(self) -> str:
        return self.posted_at[:10]


# ---------- 給 llm 的輸出格式 ----------

def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_INT = {"type": "integer"}

_PLACE_FIELDS = {
    "ref": _STR,                                   # 這篇文章裡的編號（p1、p2）
    "name_zh": _STR,
    "name_ja": _STR,
    "city": _STR,
    "kind": {"type": "string", "enum": list(KINDS)},
    "located": {"type": "string", "enum": list(LOCATED)},
    "reason": _STR,                                # 猜的：依據；猜不出來：原因
}
_PLACE = _object({**_PLACE_FIELDS, "sentence": _STR})   # sentence：原文提到這個地點的句子，照抄

_TIME = _object({"year": _STR, "month": _STR, "day_type": _STR, "time": _STR})

# 第一次解析為了少輸出：地點給主文的行號（`line`）而不抄句子；一站是一個字串，
# 景點 `p3`、住宿 `h3`（`STOP_PREFIX`）。`parse_line` 檢查過後轉回 `sentence` 和 `{ref, type}`。
LINE_SCHEMA = _object({
    "places": _array(_object({**_PLACE_FIELDS, "line": _INT})),
    "days": _array(_object({"label": _STR, "month": _STR, "stops": _array(_STR)})),
})
STOP_PREFIX = {"p": SPOT, "h": STAY}

NOTE_SCHEMA = _object({
    "places": _array(_PLACE),
    "place_notes": _array(_object({"place": _STR, "content": _STR, "times": _array(_TIME)})),
    "moves": _array(_object({"from": _STR, "to": _STR, "content": _STR, "times": _array(_TIME)})),
    "day_notes": _array(_object({"days": _array(_INT), "content": _STR})),   # days：第幾天（1 起算）
})


# ---------- 解析結果 ----------

@dataclass(frozen=True)
class PlaceRef:
    """文章裡的一個地點（`places[]` 的一筆）。"""
    ref: str
    name_zh: str
    name_ja: str
    city: str
    kind: str
    located: str
    reason: str = ""
    sentence: str = ""


@dataclass(frozen=True)
class Stop:
    ref: str                 # 地點編號
    type: str                # 景點／住宿


@dataclass(frozen=True)
class Day:
    label: str               # 原文的寫法，只用來對推文，不寫進資料庫
    month: str
    stops: list[Stop]        # 照原文順序

    @property
    def refs(self) -> list[str]:
        return [s.ref for s in self.stops]


@dataclass(frozen=True)
class TimeTag:
    year: str = ""
    month: str = ""
    day_type: str = ""
    time: str = ""


@dataclass(frozen=True)
class PlaceNote:
    place: str               # 地點編號
    content: str
    times: list[TimeTag]


@dataclass(frozen=True)
class Move:
    origin: str              # 起點的地點編號（llm 輸出的 `from`）
    dest: str                # 終點的地點編號（llm 輸出的 `to`）
    content: str
    times: list[TimeTag]


@dataclass(frozen=True)
class DayNote:
    days: list[int]          # 講的是第幾天（`Parsed.days` 的順序，1 起算）；講整趟的是空的
    content: str


@dataclass(frozen=True)
class Parsed:
    """一篇文章兩次 llm 解析合起來、檢查過的結果。"""
    places: list[PlaceRef]
    days: list[Day]
    place_notes: list[PlaceNote]
    moves: list[Move]
    day_notes: list[DayNote]
    dropped: int = 0         # 第二次解析重問後還有問題而不收入的筆數

    @classmethod
    def from_llm(cls, line: dict, note: dict, dropped: int = 0) -> "Parsed":
        """line 是 `parse_line.parse` 的回傳（已轉回 `sentence` 和 `{ref, type}`），
        note 是照 `NOTE_SCHEMA` 的輸出（各批合併後的）。"""
        return cls(
            places=[PlaceRef(**p) for p in [*line["places"], *note["places"]]],
            days=[Day(d["label"], d["month"], [Stop(s["ref"], s["type"]) for s in d["stops"]]) for d in line["days"]],
            place_notes=[PlaceNote(n["place"], n["content"], _times(n["times"]))
                         for n in note["place_notes"]],
            moves=[Move(m["from"], m["to"], m["content"], _times(m["times"]))
                   for m in note["moves"]],
            day_notes=[DayNote(list(n["days"]), n["content"]) for n in note["day_notes"]],
            dropped=dropped,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Parsed":
        return cls(
            places=[PlaceRef(**p) for p in d["places"]],
            days=[Day(x["label"], x["month"], [Stop(**s) for s in x["stops"]]) for x in d["days"]],
            place_notes=[PlaceNote(**{**n, "times": _times(n["times"])}) for n in d["place_notes"]],
            moves=[Move(**{**m, "times": _times(m["times"])}) for m in d["moves"]],
            day_notes=[DayNote(list(n["days"]), n["content"]) for n in d["day_notes"]],
            dropped=d.get("dropped", 0),
        )


def _times(items: list[dict]) -> list[TimeTag]:
    return [TimeTag(**t) for t in items]


# ---------- 地點解析 ----------

# 本機地圖的來源
OSM = "osm"
OVERTURE = "overture"
SOURCE_LABELS = {OSM: "OSM", OVERTURE: "Overture"}

# 本機地圖的比對結果，好的在前
EXACT = "完全相同"
CONTAINS = "包含"
FUZZY = "模糊"


@dataclass(frozen=True)
class Candidate:
    """本機地圖（OSM＋Overture）查到的一筆地點。"""
    source: str              # osm／overture
    source_id: str           # OSM：node/123、way/456、relation/789；Overture：GERS id
    name: str
    category: str            # OSM：amenity=restaurant；Overture：categories.primary
    kind: str                # 景點／區域
    pref: str                # 都道府縣
    municipality: str        # 市區町村
    address: str
    lat: float               # 代表點
    lon: float
    boundary: dict | None    # 區域的範圍（geojson），其他是 None
    match: str = ""          # 完全相同／包含／模糊；用來源 id 查到的是空字串
    similarity: float = 0.0  # 跟查詢名稱的 jaro_winkler_similarity（有好幾個查詢名稱時取最高的）
    importance: float = 0.0  # 維基百科重要度（0–1）；沒有維基條目是 0


@dataclass(frozen=True)
class Area:
    """本機地圖的搜尋範圍，三種其中之一：

    - 行政區：`pref`（都道府縣），可以再加 `municipality`（市區町村）。寫法是地圖裡的寫法（`LocalMap.admin_area` 對過）。
    - 圓：以 `anchor`（同一天已解析的地點）為中心、半徑 `km` 公里。
    - 都沒有：全日本。
    """
    pref: str = ""
    municipality: str = ""
    anchor: str = ""
    lat: float | None = None
    lon: float | None = None
    km: float | None = None

    @property
    def is_circle(self) -> bool:
        return self.lat is not None and self.lon is not None and self.km is not None

    def label(self) -> str:
        """給人和 llm 看的寫法：`東京 新宿区`、`「上野站」方圓 1 公里`、`全日本`。"""
        if self.is_circle:
            return f"「{self.anchor}」方圓 {self.km:g} 公里"
        return " ".join(x for x in (self.pref, self.municipality) if x) or "全日本"


@dataclass(frozen=True)
class Nearby:
    """同一天的其他地點；已經解析出來的才有座標。"""
    name: str
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class Context:
    """一個地點在文章裡的上下文，地點解析時用。"""
    article_id: str
    same_day: list[Nearby] = field(default_factory=list)


@dataclass(frozen=True)
class Resolution:
    """一個地點的解析結果：place_id 是 None 就是解析不到（查不到或猜不出來），照樣寫入，place id 留空。"""
    place_id: int | None = None


# ---------- 執行結果 ----------

@dataclass
class Report:
    written: list[str] = field(default_factory=list)               # 寫入的文章 id
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (檔案或文章 id, 原因)
    dropped: dict[str, int] = field(default_factory=dict)          # 沒寫進資料庫的東西（`store.DROPPED_KINDS`）→ 筆數
    dropped_items: list = field(default_factory=list)              # 寫入時拿掉的每一筆（`store.Dropped`）
    unfinished: list[str] = field(default_factory=list)            # 整批停下時還沒做完的文章 id
    error: str | None = None                                       # 整批停下的原因
    log_dir: Path | None = None                                    # 這次的 log 資料夾（`geocode_log`）
    geocode_traces: list = field(default_factory=list)             # 這次解析過的每個地點（`geocode_log.Trace`）
    parts: dict = field(default_factory=dict)                      # 各部分 → token 和時間（`usage.Part`）
    seconds: float = 0.0                                           # 整批花的時間
