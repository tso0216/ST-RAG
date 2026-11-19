"""問 llm：要用哪些名稱、在什麼範圍查（搜尋計畫）、候選裡哪一個是這個地點、上網查地址。組提示詞、定義輸出格式、呼叫 llm。只有 `geocoding` 用。"""
import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from service import llm

from . import config
from .data_schema import LOCATED_GUESS, SOURCE_LABELS, Area, Candidate, Context, Nearby, PlaceRef

log = logging.getLogger(__name__)

PICK_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "choice": {"type": "integer"}},
    "required": ["reason", "choice"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}},
        "pref": {"type": "string"},
        "municipality": {"type": "string"},
        "near": {"type": "integer"},
        "km": {"type": "number"},
    },
    "required": ["reason", "names", "pref", "municipality", "near", "km"],
    "additionalProperties": False,
}

WEB_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "address": {"type": "string"}, "source_url": {"type": "string"}},
    "required": ["found", "address", "source_url"],
    "additionalProperties": False,
}
WEB_TOOLS = ["WebSearch", "WebFetch"]



@dataclass(frozen=True)
class Plan:
    """一輪的搜尋計畫。範圍：`near` 不是 0 是圓（同一天第幾個地點為中心、半徑 `km`），
    否則 `pref`、`municipality` 是行政區，都空的是全日本。"""
    names: list[str]
    pref: str = ""
    municipality: str = ""
    near: int = 0
    km: float = 0.0
    reason: str = ""

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


async def plan(place: PlaceRef, ctx: Context, history: list[str], error: str = "") -> Plan:
    """請 llm 給這一輪的搜尋計畫：幾個日文名稱和一個範圍。

    history：之前每一輪查了什麼、結果如何（一輪一行）；error：上一次給的範圍不能用的原因（請它改）。
    """
    parts = [_describe(place, ctx, numbered=True)]
    if history:
        parts.append("<history>\n" + "\n".join(history) + "\n</history>")
    if error:
        parts.append(f"<error>{error}</error>")
    out = await llm.call((config.PROMPT_DIR / "plan.md").read_text(), "\n".join(parts), PLAN_SCHEMA,
                         config.MODEL, effort=config.EFFORT)
    names = list(dict.fromkeys(n.strip() for n in out["names"] if n.strip()))
    result = Plan(names, out["pref"].strip(), out["municipality"].strip(), int(out["near"] or 0),
                  float(out["km"] or 0), out["reason"].strip())
    log.info("  llm 的搜尋計畫：%s｜%s（%s）", "、".join(names), result.to_log() or "全日本", result.reason)
    return result


async def pick(place: PlaceRef, ctx: Context, candidates: list[Candidate], area: Area,
               widened_from: Area | None = None) -> Pick:
    """問 llm 候選裡哪一筆是這個地點；回傳它挑的編號（超出範圍當成 0，都不是）和理由。

    area 是實際查的範圍；widened_from 是自動放寬前的範圍（沒放寬是 None）。
    """
    results = "\n".join(f"{i}. {describe_candidate(c, candidates, ctx.same_day, area)}"
                        for i, c in enumerate(candidates, 1))
    where = area.label()
    if widened_from is not None:
        where += f"（原本是 {widened_from.label()}，裡面沒有同名的，自動放寬）"
    user = f"{_describe(place, ctx)}\n<area>{where}</area>\n<results>\n{results}\n</results>"
    out = await llm.call((config.PROMPT_DIR / "pick.md").read_text(), user, PICK_SCHEMA,
                         config.MODEL, effort=config.EFFORT)
    choice = out["choice"] if 1 <= out["choice"] <= len(candidates) else 0
    log.info("  llm 挑第 %s 筆：%s", choice or "0（都不是）", out["reason"])
    return Pick(choice, out["reason"])


async def web_address(place: PlaceRef, ctx: Context, tried: list[str]) -> WebAddress:
    """請 llm 上網查這個地點的日文地址（開 WebSearch、WebFetch）。`found` 是 true 但地址是空的，當作沒找到。"""
    user = f"{_describe(place, ctx)}\n<tried>{'、'.join(tried)}</tried>"
    out = await llm.call((config.PROMPT_DIR / "web_address.md").read_text(), user, WEB_ADDRESS_SCHEMA,
                         config.MODEL, effort=config.EFFORT, tools=WEB_TOOLS)
    address, url = out["address"].strip(), out["source_url"].strip()
    found = bool(out["found"]) and bool(address)
    log.info("  上網查地址：%s", f"{address}（{url}）" if found else "查不到")
    return WebAddress(found, address, url)


def describe_candidate(c: Candidate, candidates: list[Candidate], same_day: list[Nearby], area: Area) -> str:
    """`名稱｜來源｜類別｜縣 市區町村 地址｜座標｜離中心「X」N 公里｜離同一天的「X」約 N 公里｜300 公尺內其他候選 N 筆`。

    範圍不是圓就沒有「離中心」；同一天沒有已解析的地點就沒有「離同一天」。
    """
    where = " ".join(x for x in (c.pref, c.municipality, c.address) if x)
    parts = [c.name, SOURCE_LABELS.get(c.source, c.source), c.category, where, f"{c.lat:.4f}, {c.lon:.4f}"]
    center = center_km(c, area)
    if center is not None:
        parts.append(f"離中心「{area.anchor}」{center:.1f} 公里")
    near = nearest(c, same_day)
    if near:
        parts.append(f"離同一天的「{near[0]}」約 {near[1]:.1f} 公里")
    parts.append(f"{config.NEARBY_KM * 1000:g} 公尺內其他候選 {nearby_count(c, candidates)} 筆")
    return "｜".join(parts)


def center_km(c: Candidate, area: Area) -> float | None:
    """離圓形範圍中心幾公里；範圍不是圓是 None。"""
    return km(c.lat, c.lon, area.lat, area.lon) if area.is_circle else None


def nearby_count(c: Candidate, candidates: list[Candidate]) -> int:
    """`config.NEARBY_KM` 以內還有幾筆其他候選（不算自己）。"""
    return sum(1 for o in candidates if o is not c and km(c.lat, c.lon, o.lat, o.lon) <= config.NEARBY_KM)


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
