"""問 llm 是哪一個、請 llm 再提供：組提示詞、定義輸出格式、呼叫 llm。只有 `geocoding` 用。"""
import logging
from math import asin, cos, radians, sin, sqrt

from service import llm

from . import config
from .data_schema import LOCATED_GUESS, Candidate, Context, Nearby, PlaceRef

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

NO_RESULT = "沒有結果"
NONE_MATCH = "有結果，但都不是這個地點"


async def pick(place: PlaceRef, ctx: Context, candidates: list[Candidate]) -> Candidate | None:
    """問 llm 搜尋結果裡哪一筆是這個地點；都不是回傳 None。"""
    results = "\n".join(
        f"{i}. {c.name}｜{c.type}｜{c.address}{_distance(c, ctx.same_day)}" for i, c in enumerate(candidates, 1))
    user = f"{_describe(place, ctx)}\n<results>\n{results}\n</results>"
    out = await llm.call((config.PROMPT_DIR / "pick.md").read_text(), user, PICK_SCHEMA,
                         config.MODEL, effort=config.EFFORT)
    choice = out["choice"]
    log.info("  llm 挑第 %s 筆：%s", choice or "0（都不是）", out["reason"])
    return candidates[choice - 1] if 1 <= choice <= len(candidates) else None


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


def _describe(place: PlaceRef, ctx: Context) -> str:
    same_day = "、".join(n.name for n in ctx.same_day)
    return (
        f"<place>{place.name_zh}｜{place.name_ja}｜{place.city or '城市不明'}｜{place.kind}</place>\n"
        f"<sentence>{place.sentence}</sentence>\n"
        f"<same_day>{same_day}</same_day>\n"
        f"<guessed>{'是' if place.located == LOCATED_GUESS else '否'}</guessed>"
    )


def _distance(c: Candidate, same_day: list[Nearby]) -> str:
    """離同一天已經解析出來的地點裡最近的那一個多遠。"""
    known = [(km(c.lat, c.lon, n.lat, n.lon), n.name) for n in same_day if n.lat is not None]
    if not known:
        return ""
    distance, name = min(known)
    return f"｜離同一天的「{name}」約 {distance:.1f} 公里"


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = (sin(radians(lat2 - lat1) / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * asin(sqrt(a))
