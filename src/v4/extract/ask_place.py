"""問 llm：候選裡哪一個是這個地點、換一個名稱再查、上網查地址。組提示詞、定義輸出格式、呼叫 llm。只有 `geocoding` 用。"""
import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from service import llm

from . import config
from .data_schema import LOCATED_GUESS, SOURCE_LABELS, Candidate, Context, Nearby, PlaceRef

log = logging.getLogger(__name__)

PICK_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "choice": {"type": "integer"}},
    "required": ["reason", "choice"],
    "additionalProperties": False,
}

NEW_NAME_SCHEMA = {
    "type": "object",
    "properties": {"name_ja": {"type": "string"}, "city": {"type": "string"}},
    "required": ["name_ja", "city"],
    "additionalProperties": False,
}

WEB_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "address": {"type": "string"}, "source_url": {"type": "string"}},
    "required": ["found", "address", "source_url"],
    "additionalProperties": False,
}
WEB_TOOLS = ["WebSearch", "WebFetch"]

NO_RESULT = "沒有結果"
NONE_MATCH = "有結果，但都不是這個地點"


@dataclass(frozen=True)
class Pick:
    choice: int      # 候選的編號，0 是都不是
    reason: str


@dataclass(frozen=True)
class WebAddress:
    found: bool
    address: str     # 日文地址，從都道府縣寫起
    source_url: str  # 寫著這個地址的網頁


async def pick(place: PlaceRef, ctx: Context, candidates: list[Candidate]) -> Pick:
    """問 llm 候選裡哪一筆是這個地點；回傳它挑的編號（超出範圍當成 0，都不是）和理由。"""
    results = "\n".join(f"{i}. {describe_candidate(c, ctx.same_day)}" for i, c in enumerate(candidates, 1))
    user = f"{_describe(place, ctx)}\n<results>\n{results}\n</results>"
    out = await llm.call((config.PROMPT_DIR / "pick.md").read_text(), user, PICK_SCHEMA,
                         config.MODEL, effort=config.EFFORT)
    choice = out["choice"] if 1 <= out["choice"] <= len(candidates) else 0
    log.info("  llm 挑第 %s 筆：%s", choice or "0（都不是）", out["reason"])
    return Pick(choice, out["reason"])


async def new_name(place: PlaceRef, ctx: Context, tried: list[str], had_results: bool) -> tuple[str, str]:
    """請 llm 換一個日文名稱（和城市）再查。had_results：上一次是有結果但都不是，還是沒有結果。"""
    user = (
        f"{_describe(place, ctx)}\n"
        f"<tried>{'、'.join(tried)}</tried>\n"
        f"<last>{NONE_MATCH if had_results else NO_RESULT}</last>"
    )
    out = await llm.call((config.PROMPT_DIR / "new_name.md").read_text(), user, NEW_NAME_SCHEMA,
                         config.MODEL, effort=config.EFFORT)
    return out["name_ja"].strip(), out["city"].strip()


async def web_address(place: PlaceRef, ctx: Context, tried: list[str]) -> WebAddress:
    """請 llm 上網查這個地點的日文地址（開 WebSearch、WebFetch）。`found` 是 true 但地址是空的，當作沒找到。"""
    user = f"{_describe(place, ctx)}\n<tried>{'、'.join(tried)}</tried>"
    out = await llm.call((config.PROMPT_DIR / "web_address.md").read_text(), user, WEB_ADDRESS_SCHEMA,
                         config.MODEL, effort=config.EFFORT, tools=WEB_TOOLS)
    address, url = out["address"].strip(), out["source_url"].strip()
    found = bool(out["found"]) and bool(address)
    log.info("  上網查地址：%s", f"{address}（{url}）" if found else "查不到")
    return WebAddress(found, address, url)


def describe_candidate(c: Candidate, same_day: list[Nearby]) -> str:
    """`名稱｜來源｜類別｜縣 市區町村 地址｜離同一天的「X」約 N 公里`；同一天沒有已解析的地點就沒有最後一段。"""
    where = " ".join(x for x in (c.pref, c.municipality, c.address) if x)
    near = nearest(c, same_day)
    distance = f"｜離同一天的「{near[0]}」約 {near[1]:.1f} 公里" if near else ""
    return f"{c.name}｜{SOURCE_LABELS.get(c.source, c.source)}｜{c.category}｜{where}{distance}"


def nearest(c: Candidate, same_day: list[Nearby]) -> tuple[str, float] | None:
    """同一天已經解析出來的地點裡離它最近的那一個：(名稱, 公里)；都還沒解析是 None。"""
    known = [(km(c.lat, c.lon, n.lat, n.lon), n.name) for n in same_day if n.lat is not None]
    if not known:
        return None
    distance, name = min(known)
    return name, distance


def _describe(place: PlaceRef, ctx: Context) -> str:
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
