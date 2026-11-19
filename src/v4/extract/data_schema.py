"""各步驟之間傳遞的資料結構。不依賴其他模組。

- 整理後的文章：`Article`、`Comment`
- 兩次 llm 解析的輸出格式：`LINE_SCHEMA`、`NOTE_SCHEMA`，只在這裡定義
- 解析結果（檢查過、兩次合起來）：`Parsed`，也是 `data/v4_pending/` 存的內容
- 地點解析：`Candidate`（本機地圖的一筆結果）、`Context`（地點在文章裡的上下文）、`Resolution`、`Unresolved`
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

# 要使用者決定的兩種地點
NOT_FOUND = "解析不到"
NO_GUESS = "猜不出來"


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

_PLACE = _object({
    "ref": _STR,                                   # 這篇文章裡的編號（p1、p2）
    "name_zh": _STR,
    "name_ja": _STR,
    "city": _STR,
    "kind": {"type": "string", "enum": list(KINDS)},
    "located": {"type": "string", "enum": list(LOCATED)},
    "reason": _STR,                                # 猜的：依據；猜不出來：原因
    "sentence": _STR,                              # 原文提到這個地點的句子，照抄
})

_TIME = _object({"year": _STR, "month": _STR, "day_type": _STR, "time": _STR})

LINE_SCHEMA = _object({
    "places": _array(_PLACE),
    "days": _array(_object({"label": _STR, "month": _STR, "stops": _array(_STR)})),
})

NOTE_SCHEMA = _object({
    "places": _array(_PLACE),
    "place_notes": _array(_object({"place": _STR, "content": _STR, "times": _array(_TIME)})),
    "moves": _array(_object({"from": _STR, "to": _STR, "content": _STR, "times": _array(_TIME)})),
    "day_notes": _array(_object({"day": _STR, "content": _STR})),
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
class Day:
    label: str               # 原文的寫法，只用來對推文，不寫進資料庫
    month: str
    stops: list[str]         # 地點編號，照原文順序


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
    day: str                 # `Day.label`；講整趟的留空
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
        """line、note 是照 `LINE_SCHEMA`、`NOTE_SCHEMA` 的輸出（note 是各批合併後的）。"""
        return cls(
            places=[PlaceRef(**p) for p in [*line["places"], *note["places"]]],
            days=[Day(d["label"], d["month"], list(d["stops"])) for d in line["days"]],
            place_notes=[PlaceNote(n["place"], n["content"], _times(n["times"]))
                         for n in note["place_notes"]],
            moves=[Move(m["from"], m["to"], m["content"], _times(m["times"]))
                   for m in note["moves"]],
            day_notes=[DayNote(**n) for n in note["day_notes"]],
            dropped=dropped,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Parsed":
        return cls(
            places=[PlaceRef(**p) for p in d["places"]],
            days=[Day(**x) for x in d["days"]],
            place_notes=[PlaceNote(**{**n, "times": _times(n["times"])}) for n in d["place_notes"]],
            moves=[Move(**{**m, "times": _times(m["times"])}) for m in d["moves"]],
            day_notes=[DayNote(**n) for n in d["day_notes"]],
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
MATCHES = (EXACT, CONTAINS, FUZZY)


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
    similarity: float = 0.0  # 跟查詢名稱的 jaro_winkler_similarity


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
    in_itinerary: bool = False                       # 是不是行程的一站
    where: str = ""                                  # 是哪一天的第幾站（`1/31(六) 第 3 站`）
    same_day: list[Nearby] = field(default_factory=list)


@dataclass(frozen=True)
class Unresolved:
    """要使用者決定的一個地點。"""
    type: str                # NOT_FOUND（解析不到）／NO_GUESS（猜不出來）
    article_id: str
    name_zh: str
    name_ja: str
    city: str
    guessed: bool = False    # `located` 是 `猜的`
    sentence: str = ""
    tried: list[str] = field(default_factory=list)   # 試過的日文名稱
    reason: str = ""         # llm 寫的依據或原因
    where: str = ""          # 猜不出來：是哪一天的第幾站
    same_day: list[str] = field(default_factory=list)
    note: str = ""           # 使用者填的答案用不上時的說明


@dataclass(frozen=True)
class Resolution:
    """一個地點的解析結果，三種其中之一：有 place、不收入、要使用者決定。"""
    place_id: int | None = None
    excluded: bool = False
    unresolved: Unresolved | None = None


# ---------- 執行結果 ----------

@dataclass
class Report:
    written: list[str] = field(default_factory=list)               # 寫入的文章 id
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (檔案或文章 id, 原因)
    waiting: list[str] = field(default_factory=list)               # 等地點答案的文章 id
    dropped: int = 0                                               # 不收入的筆數
    unresolved: list[Unresolved] = field(default_factory=list)     # 要使用者決定的地點
    unfinished: list[str] = field(default_factory=list)            # 整批停下時還沒做完的文章 id
    error: str | None = None                                       # 整批停下的原因
    geocode_log: Path | None = None                                # 這次的地點解析 log 檔
