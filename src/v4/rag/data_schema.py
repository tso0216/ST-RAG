"""各步驟之間傳的資料結構，和給 LLM 的輸出格式。不依賴其他模組。

- 常數：題型、節點種類、地點條件的寫法、證據的標記、狀態
- 給 LLM 的輸出格式：`SPLIT_SCHEMA`（拆題）、`PLAN_SCHEMA`（③）、`REPLAN_SCHEMA`（⑦，多 `relax_from`）、
  `SUMMARY_SCHEMA`（整理重點），只在這裡定義
- 拆題結果：`Question`；使用者的行程：`UserDay`
- 節點：`Where`（一個地點）、`Scope`（地點條件）、`TimeCond`（時間條件）、`Stop`（排法展開後的一站）、`Node`
- 節點結果：`Evidence`、`Summary`、`DayView`、`NodeResult`

LLM 輸出裡「留空」一律是空字串、空陣列或 0。
"""
from dataclasses import dataclass, field

# ---------- 題型（8.3） ----------
POINT = "點"
SEGMENT = "段"
ITINERARY = "行程"
OTHER = "其他"
UNANSWERABLE = "無法處理"
QTYPES = (POINT, SEGMENT, ITINERARY, OTHER, UNANSWERABLE)
DB_QTYPES = (POINT, SEGMENT, ITINERARY)   # 用資料庫回答、要解析使用者行程的題型

# ② 分流的三條路
ROUTE_FIXED = "固定文字"
ROUTE_GENERAL = "其他題"
ROUTE_RAG = "點段行程"

# ---------- 節點（10.2） ----------
NODE_POINT = "點"
NODE_MOVE = "段"
NODE_DAY = "天"
NODE_KINDS = (NODE_POINT, NODE_MOVE, NODE_DAY)

# 地點條件的寫法（6.1）
AT = "地點"
AROUND = "周圍"
ANYWHERE = "不限"
SCOPE_MODES = (AT, AROUND, ANYWHERE)

SPOT = "景點"
AREA = "區域"
PLACE_KINDS = (SPOT, AREA)
STAY = "住宿"
DAY_TYPES = ("平日", "假日")
YES = "是"
NO = "否"

# 經驗所在的表；證據編號是「表名 id」（`place_note 1767020760-3`）
PLACE_NOTE = "place_note"
MOVE = "move"
DAY_NOTE = "day_note"
DAY = "day"

# 證據的標記
FLAG_UNKNOWN = "時間不明"
FLAG_DIFFERENT = "情況不同"
FLAG_REVERSED = "方向相反"
FLAG_BY_NAME = "名稱比對"

# 節點結果的狀態（12.5）
HAS = "有"
HAS_NO_NOTE = "有，沒有評語"
NONE = "沒有"

# 查詢狀態表（13.2）
ST_HAS = "有"
ST_RELAXED = "放寬後有"
ST_NONE = "沒有"
ST_UNRESOLVED = "地點解析不到"
ST_OVER = "超過節點上限，沒查"

# ④ 拿掉節點的原因
DROP_FORMAT = "格式"
DROP_UNRESOLVED = "地點解析不到"
DROP_DUPLICATE = "重複"
DROP_OVER = "超過上限"

# 整理重點的「還沒查的事」
TODO_PLACE = "地點"
TODO_QUESTION = "問題"

# 排法的計算用的數字種類（12.6）
STAY_TIME = "停留時間"
MOVE_TIME = "移動時間"
MINUTE = "分鐘"


# ---------- 給 LLM 的輸出格式 ----------

def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


SPLIT_SCHEMA = _object({
    "questions": _array(_object({"text": _STR, "type": _enum(QTYPES)})),
})

# 一個地點：ref 是 ① 地點列表的編號（p3）；列表裡沒有的 ref 留空，寫新地點的名稱、城市、景點或區域
_PLACE = _object({"ref": _STR, "name_zh": _STR, "name_ja": _STR, "city": _STR, "kind": _STR})
# 地點條件：mode 是地點、周圍、不限；用不到這個欄位時 mode 留空
_SCOPE = _object({"mode": _enum((*SCOPE_MODES, "")), "place": _PLACE, "km": _NUM})
# 排法的一站：day 是使用者行程裡一天的編號（D3），代表那天全部的站；是地點時 day 留空、填 place
_STOP = _object({"day": _STR, "place": _PLACE})
_HOURS = _object({"start": _INT, "end": _INT})

NODE_FIELDS = {
    "kind": _enum(NODE_KINDS),
    "target": _SCOPE,                # 點節點的對象
    "origin": _SCOPE,                # 段節點的起點
    "dest": _SCOPE,                  # 段節點的終點
    "both_ways": _enum((YES, NO, "")),   # 段節點：反方向也要
    "stops": _array(_STOP),          # 天節點的排法，照順序；不填是空陣列
    "year": _STR,                    # 四位數的年，只要這一年以後的經驗；不限留空
    "months": _array(_INT),
    "day_type": _STR,                # 平日、假日；不限留空
    "hours": _array(_HOURS),         # 鐘點範圍，start > end 是跨夜
    "aspect": _STR,
    "reason": _STR,
}
PLAN_SCHEMA = _object({"nodes": _array(_object(NODE_FIELDS))})
REPLAN_SCHEMA = _object({"nodes": _array(_object({**NODE_FIELDS, "relax_from": _STR}))})

SUMMARY_SCHEMA = _object({
    "points": _array(_object({"text": _STR, "sources": _array(_STR)})),
    "numbers": _array(_object({"kind": _STR, "min": _NUM, "max": _NUM, "unit": _STR, "quote": _STR,
                               "source": _STR})),
    "differences": _array(_object({"source": _STR, "what": _STR})),
    "todo": _array(_object({"type": _enum((TODO_PLACE, TODO_QUESTION)), "text": _STR, "source": _STR,
                            "name_zh": _STR, "name_ja": _STR, "city": _STR, "kind": _STR})),
})


# ---------- 拆題、使用者的行程 ----------

@dataclass(frozen=True)
class Question:
    no: int          # 第幾題，從 1 起算
    text: str        # 問句
    qtype: str       # 題型


@dataclass(frozen=True)
class UserStop:
    ref: str                 # ① 地點列表的編號
    type: str                # 景點／住宿


@dataclass(frozen=True)
class UserDay:
    """使用者行程的一天：編號是 D1、D2……（照 ① 的順序）。"""
    label: str               # 原文的寫法
    month: str
    stops: tuple[UserStop, ...]


# ---------- 節點 ----------

@dataclass(frozen=True)
class Where:
    """節點裡的一個地點：① 地點列表的編號（ref），或新地點（名稱、城市、景點或區域）。

    place_id 是 ④ 解析的結果，None 是解析不到（或還沒解析）。
    """
    ref: str = ""
    name_zh: str = ""
    name_ja: str = ""
    city: str = ""
    kind: str = ""
    place_id: int | None = None

    @property
    def label(self) -> str:
        return self.name_zh or self.name_ja or self.ref

    def key(self) -> tuple:
        """同一篇發問文裡同一個地點只解析一次，用這個當鍵。"""
        return ("ref", self.ref) if self.ref else ("new", self.name_zh, self.name_ja, self.city, self.kind)


@dataclass(frozen=True)
class Scope:
    """地點條件（6.1）。地點、周圍有 where；周圍另外有半徑 km。"""
    mode: str
    where: Where | None = None
    km: float | None = None

    @property
    def label(self) -> str:
        if self.mode == ANYWHERE or self.where is None:
            return ANYWHERE
        if self.mode == AROUND:
            return f"{self.where.label} 周圍 {self.km:g} 公里"
        return self.where.label

    def key(self) -> tuple:
        """比對是不是同樣的條件（去重複）：用解析後的 place id。"""
        if self.mode == ANYWHERE or self.where is None:
            return (ANYWHERE,)
        place = self.where.place_id if self.where.place_id is not None else self.where.key()
        return (self.mode, place, self.km if self.mode == AROUND else None)


@dataclass(frozen=True)
class TimeCond:
    """時間條件（5.1）。都沒填就是不限。hours 每個是（H1, H2），H1 > H2 是跨夜。"""
    year: int | None = None
    months: tuple[int, ...] = ()
    day_type: str = ""
    hours: tuple[tuple[int, int], ...] = ()

    @property
    def empty(self) -> bool:
        return self.year is None and not self.months and not self.day_type and not self.hours

    def parts(self) -> dict[str, str]:
        """每個維度給人和 LLM 看的寫法；不限的是「不限」。"""
        return {
            "年份": f"{self.year} 年以後" if self.year else ANYWHERE,
            "月份": "、".join(map(str, self.months)) if self.months else ANYWHERE,
            "平日／假日": self.day_type or ANYWHERE,
            "時段": "、".join(f"{a}–{b}" for a, b in self.hours) if self.hours else ANYWHERE,
        }

    @property
    def label(self) -> str:
        shown = [f"{k} {v}" for k, v in self.parts().items() if v != ANYWHERE]
        return "，".join(shown) or "時間不限"


@dataclass(frozen=True)
class Stop:
    """排法展開後的一站。day 是從使用者行程哪一天展開來的（D3），自己寫的站是空的。"""
    where: Where
    day: str = ""
    type: str = SPOT

    @property
    def resolved(self) -> bool:
        return self.where.place_id is not None

    def key(self) -> tuple:
        return ("id", self.where.place_id) if self.resolved else ("name", self.where.label)


@dataclass
class Node:
    """通過 ④ 的節點。no 是 N1、N2……（整題連續編）。"""
    no: str
    kind: str
    aspect: str
    reason: str
    time: TimeCond = field(default_factory=TimeCond)
    target: Scope | None = None          # 點
    origin: Scope | None = None          # 段
    dest: Scope | None = None            # 段
    both_ways: bool = False              # 段：反方向也要
    arrangement: tuple[str, ...] = ()    # 天：排法照 LLM 寫的（D3、清水寺），給人看
    stops: tuple[Stop, ...] = ()         # 天：排法展開成站
    relax_from: str = ""                 # ⑦：從哪個節點放寬
    changes: tuple[str, ...] = ()        # 跟 relax_from 那個節點比，改了什麼
    round: int = 0                       # 第幾輪加的（0 是 ③）
    raw: dict = field(default_factory=dict)   # LLM 的原始輸出

    @property
    def has_arrangement(self) -> bool:
        return bool(self.stops)

    def object_label(self) -> str:
        """對象或排法給人看的寫法。"""
        if self.kind == NODE_POINT and self.target:
            return self.target.label
        if self.kind == NODE_MOVE and self.origin and self.dest:
            arrow = " ↔ " if self.both_ways else " → "
            return f"{self.origin.label}{arrow}{self.dest.label}"
        return "、".join(self.arrangement) if self.arrangement else "沒有排法"

    def describe(self) -> str:
        """一行說明：`N1 點｜淺草寺｜月份 10｜面向：排隊時間`。"""
        parts = [f"{self.no} {self.kind}", self.object_label(), self.time.label, f"面向：{self.aspect}"]
        if self.kind == NODE_MOVE:
            parts.insert(2, f"反方向也要：{YES if self.both_ways else NO}")
        if self.relax_from:
            parts.append(f"從 {self.relax_from} 放寬（{'；'.join(self.changes) or '條件一樣'}）")
        return "｜".join(parts)


# ---------- 節點結果 ----------

@dataclass
class Evidence:
    """一筆證據。ref 是證據編號（`place_note 1767020760-3`）。"""
    table: str
    id: str
    article_id: str
    content: str
    place: str                    # 地點的顯示名稱（段是「起點 → 終點」）
    group: str = ""               # 依地點分組時的組名
    time: str = ""                # 經驗的時間（給 LLM 看）
    year: str = ""                # 年（經驗的年；沒寫的是發文年）
    score: float | None = None    # JEV 的機率；天節點有排法挑出的沒有
    flags: list[str] = field(default_factory=list)
    places: tuple = ()            # 這筆經驗的地點：點是（place id,），段是（起點 id, 終點 id）；解析不到是 None

    @property
    def ref(self) -> str:
        return f"{self.table} {self.id}"


@dataclass
class Summary:
    """整理重點（12.6）檢查過的結果。dropped：重問後還有問題而拿掉的那幾則。"""
    points: list[dict] = field(default_factory=list)
    numbers: list[dict] = field(default_factory=list)
    differences: list[dict] = field(default_factory=list)
    todo: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


@dataclass
class DayView:
    """天節點挑出或列出的一天（12.5「每一天帶出」）。"""
    id: str
    article_id: str
    title: str
    posted_at: str
    no: int                       # 第幾天
    days_in_article: int          # 那篇在資料庫裡有幾天
    month: str
    stops: list[dict]             # {"name", "type", "place_id", "same"}
    notes: list[dict]             # 對到這一天的 day_note：{"id", "content"}
    trip_notes: list[dict]        # 那篇講整趟的 day_note
    same: list[str] = field(default_factory=list)   # 相同的站（排法那一站的名稱）
    unknown: list[str] = field(default_factory=list)  # 時間不明的維度

    @property
    def same_count(self) -> int:
        return len(self.same)


@dataclass
class NodeResult:
    status: str                                  # 有、有，沒有評語、沒有
    candidates: int = 0                          # 候選筆數（天節點有排法：至少一站相同的天數，不看時間）
    time_matched: int = 0                        # 時間相符筆數
    embedded: int | None = None                  # embedding 先取了幾筆；沒做是 None
    judged: int = 0                              # 送相關判斷的筆數
    passed: int = 0                              # 通過相關判斷的筆數
    evidence: list[Evidence] = field(default_factory=list)
    groups: list[tuple[str, list[str]]] = field(default_factory=list)   # （組名，證據編號）
    summary: Summary | None = None
    other_counts: dict | None = None             # 其他條件下的筆數（12.7）
    days: list[DayView] = field(default_factory=list)
    same_days: int | None = None                 # 有排法：至少一站相同、時間相符的天數（取前 20 天之前）
    calc: dict | None = None                     # 排法的計算（12.8）

    @property
    def found(self) -> bool:
        return self.status != NONE


# ---------- ④ 的結果、一題的狀態 ----------

@dataclass
class Dropped:
    """④ 拿掉的節點。index 是 LLM 輸出的第幾個（0 起算）；why 是 DROP_* 其中一個。"""
    index: int
    why: str
    reason: str
    label: str
    raw: dict
    round: int = 0


@dataclass
class Batch:
    """③ 或 ⑦ 一次的結果：通過的節點、拿掉的節點、照 LLM 輸出順序的項目、紀錄。"""
    accepted: list[Node] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    order: list[tuple] = field(default_factory=list)   # 照輸出順序：("node", "N3", 第幾個) 或 ("drop", Dropped, 第幾個)
    record: dict = field(default_factory=dict)
    empty: bool = False                                              # LLM 一個節點都沒輸出


@dataclass
class QState:
    """一題目前的狀態：所有節點、結果、查詢狀態表的項目、補規劃做了幾輪。"""
    nodes: list[Node] = field(default_factory=list)
    results: dict[str, NodeResult] = field(default_factory=dict)
    items: list[tuple[str, object]] = field(default_factory=list)   # ("node", "N1") 或 ("drop", Dropped)
    dropped: list[Dropped] = field(default_factory=list)            # ③、⑦ 拿掉的所有節點
    replans: int = 0

    def node(self, no: str) -> Node:
        return next(n for n in self.nodes if n.no == no)

    def pairs(self) -> list[tuple[Node, NodeResult]]:
        return [(n, self.results[n.no]) for n in self.nodes if n.no in self.results]
