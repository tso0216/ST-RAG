"""問 llm：要用哪些名稱、在什麼範圍查（搜尋計畫）、候選裡哪一個是這個地點、上網查地址。組提示詞、定義輸出格式、呼叫 llm。只有 `geocoding` 用。

三種都是一次問好幾個地點（一批最多 `config.GEOCODE_BATCH` 個，由 `geocoding` 分批）：每個地點包成
`<item ref="地點編號">…</item>`，llm 回 `places` 陣列，一個地點一筆，用 `ref` 對回去。
"""
import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from . import config, llm_log, usage
from .data_schema import LOCATED_GUESS, SOURCE_LABELS, Area, Candidate, Context, Nearby, PlaceRef

log = logging.getLogger(__name__)

_PICK_ITEM = {
    "type": "object",
    "properties": {"ref": {"type": "string"}, "reason": {"type": "string"}, "choice": {"type": "integer"}},
    "required": ["ref", "reason", "choice"],
    "additionalProperties": False,
}

_PLAN_ITEM = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "reason": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}},
        "pref": {"type": "string"},
        "municipality": {"type": "string"},
        "near": {"type": "integer"},
        "km": {"type": "number"},
    },
    "required": ["ref", "reason", "names", "pref", "municipality", "near", "km"],
    "additionalProperties": False,
}

_WEB_ITEM = {
    "type": "object",
    "properties": {"ref": {"type": "string"}, "found": {"type": "boolean"}, "address": {"type": "string"},
                   "source_url": {"type": "string"}},
    "required": ["ref", "found", "address", "source_url"],
    "additionalProperties": False,
}


def _batch(item: dict) -> dict:
    """一次問好幾個地點：輸出是 `places` 陣列，一個地點一筆，用 `ref` 對回去。"""
    return {"type": "object", "properties": {"places": {"type": "array", "items": item}},
            "required": ["places"], "additionalProperties": False}


PICK_SCHEMA = _batch(_PICK_ITEM)
PLAN_SCHEMA = _batch(_PLAN_ITEM)
WEB_ADDRESS_SCHEMA = _batch(_WEB_ITEM)
WEB_TOOLS = ["WebSearch", "WebFetch"]
NO_ANSWER = "llm 沒有回這個地點"


@dataclass(frozen=True)
class Plan:
    """一輪的搜尋計畫。範圍：`near` 不是 0 是圓（同一天第幾個地點為中心、半徑 `km`），
    否則 `pref`、`municipality` 是行政區，都空的是全日本。answered 是 False：llm 沒有回這個地點。"""
    names: list[str]
    pref: str = ""
    municipality: str = ""
    near: int = 0
    km: float = 0.0
    reason: str = ""
    answered: bool = True

    def to_log(self) -> dict:
        if self.near:
            return {"near": self.near, "km": self.km}
        return {k: v for k, v in (("pref", self.pref), ("municipality", self.municipality)) if v}


@dataclass(frozen=True)
class Pick:
    choice: int      # 候選的編號，0 是都不是
    reason: str


@dataclass(frozen=True)
class WebAddress:
    found: bool
    address: str     # 日文地址，從都道府縣寫起
    source_url: str  # 寫著這個地址的網頁


@dataclass(frozen=True)
class PlanAsk:
    """問一個地點的搜尋計畫要給的東西。history：之前每一輪（一輪一行）；error：上一次的範圍不能用的原因。"""
    place: PlaceRef
    ctx: Context
    history: list[str]
    error: str = ""


@dataclass(frozen=True)
class PickAsk:
    """問一個地點是哪一筆候選要給的東西。llm 挑的編號是 candidates 的第幾筆（1 起算）。
    area 是實際查的範圍；widened_from 是自動放寬前的（沒放寬是 None）。"""
    place: PlaceRef
    ctx: Context
    candidates: list[Candidate]
    area: Area
    widened_from: Area | None = None


@dataclass(frozen=True)
class WebAsk:
    """問一個地點的地址要給的東西。tried：在地圖上試過的日文名稱。"""
    place: PlaceRef
    ctx: Context
    tried: list[str]


async def plan(asks: list[PlanAsk]) -> dict[str, Plan]:
    """一次請 llm 給好幾個地點這一輪的搜尋計畫（幾個日文名稱和一個範圍）。回傳 地點編號 → 計畫。

    llm 沒回的地點是 `answered=False`、沒有名稱的計畫；`geocoding` 把它當成範圍不能用，跟其他要重問的一起重問。
    """
    def body(a: PlanAsk) -> str:
        parts = [_describe(a.place, a.ctx, numbered=True)]
        if a.history:
            parts.append("<history>\n" + "\n".join(a.history) + "\n</history>")
        if a.error:
            parts.append(f"<error>{a.error}</error>")
        return "\n".join(parts)

    out = await _ask("plan.md", PLAN_SCHEMA, {a.place.ref: body(a) for a in asks}, usage.PLAN, retry_missing=False)
    plans = {}
    for a in asks:
        x = out.get(a.place.ref)
        if x is None:
            plans[a.place.ref] = Plan([], reason=NO_ANSWER, answered=False)
            continue
        names = list(dict.fromkeys(n.strip() for n in x["names"] if n.strip()))
        result = Plan(names, x["pref"].strip(), x["municipality"].strip(), int(x["near"] or 0),
                      float(x["km"] or 0), x["reason"].strip())
        plans[a.place.ref] = result
        log.info("  %s 的搜尋計畫：%s｜%s（%s）", a.place.name_zh, "、".join(names), result.to_log() or "全日本",
                 result.reason)
    return plans


async def pick(asks: list[PickAsk]) -> dict[str, Pick]:
    """一次問 llm 好幾個地點各是哪一筆候選。回傳 地點編號 → 挑的編號（超出範圍或沒回當成 0，都不是）和理由。"""
    def body(a: PickAsk) -> str:
        results = "\n".join(f"{i}. {describe_candidate(c, a.ctx.same_day, a.area)}"
                            for i, c in enumerate(a.candidates, 1))
        where = a.area.label()
        if a.widened_from is not None:
            where += f"（原本是 {a.widened_from.label()}，裡面沒有同名的，自動放寬）"
        return f"{_describe(a.place, a.ctx)}\n<area>{where}</area>\n<results>\n{results}\n</results>"

    out = await _ask("pick.md", PICK_SCHEMA, {a.place.ref: body(a) for a in asks}, usage.PICK)
    picks = {}
    for a in asks:
        x = out.get(a.place.ref)
        if x is None:
            picks[a.place.ref] = Pick(0, NO_ANSWER)
            continue
        choice = x["choice"] if 1 <= x["choice"] <= len(a.candidates) else 0
        picks[a.place.ref] = Pick(choice, x["reason"])
        log.info("  %s %s：%s", a.place.name_zh, f"挑第 {choice} 筆" if choice else "不選", x["reason"])
    return picks


async def web_address(asks: list[WebAsk]) -> dict[str, WebAddress]:
    """一次請 llm 上網查好幾個地點的日文地址（開 WebSearch、WebFetch）。回傳 地點編號 → 地址。

    `found` 是 true 但地址是空的、或 llm 沒回的地點，當作沒找到。
    """
    out = await _ask("web_address.md", WEB_ADDRESS_SCHEMA,
                     {a.place.ref: f"{_describe(a.place, a.ctx)}\n<tried>{'、'.join(a.tried)}</tried>" for a in asks},
                     usage.WEB, tools=WEB_TOOLS)
    found = {}
    for a in asks:
        x = out.get(a.place.ref) or {"found": False, "address": "", "source_url": ""}
        address, url = x["address"].strip(), x["source_url"].strip()
        ok = bool(x["found"]) and bool(address)
        found[a.place.ref] = WebAddress(ok, address if ok else "", url if ok else "")
        log.info("  %s 上網查地址：%s", a.place.name_zh, f"{address}（{url}）" if ok else "查不到")
    return found


async def _ask(prompt: str, schema: dict, bodies: dict[str, str], part: str, tools: list[str] | None = None,
               retry_missing: bool = True) -> dict[str, dict]:
    """把好幾個地點包成 `<item ref="...">…</item>` 一次問，回傳 地點編號 → llm 給那個地點的那一筆。

    llm 回了不認得的編號不管、同一個編號回兩次用第一筆。用量和時間記在 part（`usage`）。
    retry_missing：有地點沒回，就只拿沒回的再問，最多 `config.RETRIES` 次；還是沒回的不在回傳裡。
    """
    system = (config.PROMPT_DIR / prompt).read_text()
    answers: dict[str, dict] = {}
    todo = dict(bodies)
    for _ in range(1 + (config.RETRIES if retry_missing else 0)):
        user = "\n".join(f'<item ref="{ref}">\n{body}\n</item>' for ref, body in todo.items())
        with usage.part(part):
            out = await llm_log.call(system, user, schema, part, prompt, tools=tools)
        assert isinstance(out, dict)   # 有給輸出格式，回的一定是 dict
        for x in out["places"]:
            if x["ref"] in todo and x["ref"] not in answers:
                answers[x["ref"]] = x
        todo = {ref: body for ref, body in todo.items() if ref not in answers}
        if not todo:
            break
        log.info("  llm 沒有回 %s", "、".join(todo))
    return answers


def describe_candidate(c: Candidate, same_day: list[Nearby], area: Area) -> str:
    """一筆候選給 llm 看的一行：`名稱｜來源｜類別｜地址｜離中心 N 公里｜離「X」N 公里`。

    地址裡跟範圍一樣的縣、市區町村不寫。範圍不是圓就沒有「離中心」；同一天沒有已解析的地點就沒有「離「X」」。
    """
    shown = [x for x, same in ((c.pref, c.pref == area.pref), (c.municipality, c.municipality == area.municipality))
             if x and not same]
    where = " ".join([*shown, c.address] if c.address else shown)
    parts = [c.name, SOURCE_LABELS.get(c.source, c.source), c.category]
    if where:
        parts.append(where)
    center = center_km(c, area)
    if center is not None:
        parts.append(f"離中心 {center:.1f} 公里")
    near = nearest(c, same_day)
    if near:
        parts.append(f"離「{near[0]}」{near[1]:.1f} 公里")
    return "｜".join(parts)


def center_km(c: Candidate, area: Area) -> float | None:
    """離圓形範圍中心幾公里；範圍不是圓是 None。"""
    return km(c.lat, c.lon, area.lat, area.lon) if area.is_circle else None


def nearest(c: Candidate, same_day: list[Nearby]) -> tuple[str, float] | None:
    """同一天已經解析出來的地點裡離它最近的那一個：(名稱, 公里)；都還沒解析是 None。"""
    known = [(km(c.lat, c.lon, n.lat, n.lon), n.name) for n in same_day if n.lat is not None]
    if not known:
        return None
    distance, name = min(known)
    return name, distance


def _describe(place: PlaceRef, ctx: Context, numbered: bool = False) -> str:
    """地點、句子、同一天的地點、是不是猜的。numbered：同一天的地點一行一個、加編號和座標（搜尋計畫用，可以當圓心）。"""
    if numbered:
        rows = [f"{i}. {n.name}（{n.lat:.4f}, {n.lon:.4f}）" if n.lat is not None else f"{i}. {n.name}（還沒定位，不能當中心）"
                for i, n in enumerate(ctx.same_day, 1)]
        same_day = "\n" + "\n".join(rows) + "\n" if rows else ""
    else:
        same_day = "、".join(n.name for n in ctx.same_day)
    return (
        f"<place>{place.name_zh}｜{place.name_ja}｜{place.city or '城市不明'}｜{place.kind}</place>\n"
        f"<sentence>{place.sentence}</sentence>\n"
        f"<same_day>{same_day}</same_day>\n"
        f"<guessed>{'是' if place.located == LOCATED_GUESS else '否'}</guessed>"
    )


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = (sin(radians(lat2 - lat1) / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * asin(sqrt(a))
