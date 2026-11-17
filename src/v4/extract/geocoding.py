"""地點解析：一個地點從「找得到嗎」到有結果或三次無結果的整個流程。

`located` 是 `是`、`猜的` 的地點：
1. 查資料庫正名、別名（`city` 也要相同），只查到一個就用它。
2. 查使用者填的答案（`place_unresolved.tsv`），有就照填的處理。
3. 用日文名稱查 OSM，有結果就問 llm 是哪一個；挑中就加入資料庫。
4. 沒結果或都不是：請 llm 再提供一個名稱，回到 3。查滿 `config.GEOCODE_ROUNDS` 次還沒有就記下來。

`located` 是 `猜不出來` 的地點不查資料庫也不查 OSM：不是行程的一站就不收入；
是行程的一站就看使用者替這一篇填的答案，沒填就記下來。
"""
import asyncio
import logging
import sqlite3

from v4 import place as place_table

from . import ask_place, config, unresolved
from .data_schema import (AREA, LOCATED_GUESS, LOCATED_UNKNOWN, LOCATED_YES, NO_GUESS, NOT_FOUND, Candidate,
                          Context, Nearby, Parsed, PlaceRef, Resolution, Unresolved)
from .osm_search import OsmSearch

log = logging.getLogger(__name__)

_ORDER = {LOCATED_YES: 0, LOCATED_GUESS: 1, LOCATED_UNKNOWN: 2}


class Geocoder:
    def __init__(self, conn: sqlite3.Connection, osm: OsmSearch, answers: unresolved.Answers):
        self.conn = conn
        self.osm = osm
        self.answers = answers

    async def resolve_article(self, article_id: str, parsed: Parsed) -> tuple[dict[str, int | None], list[Unresolved]]:
        """解析一篇文章用到的每個地點，各做一次。

        回傳（地點編號 → place id，不收入的是 None；要使用者決定的地點）。
        沒有被行程、place_note、move 用到的地點不解析。`猜的` 排在 `是` 後面做，
        這樣問 llm 是哪一家分店時，同一天的其他地點已經有座標可以比遠近。
        """
        stops: dict[str, list[tuple[int, int]]] = {}
        for d, day in enumerate(parsed.days):
            for position, ref in enumerate(day.stops, 1):
                stops.setdefault(ref, []).append((d, position))
        used = set(stops) | {n.place for n in parsed.place_notes} | {r for m in parsed.moves for r in (m.origin, m.dest)}
        names = {p.ref: p.name_zh for p in parsed.places}
        place_ids: dict[str, int | None] = {}
        undecided: list[Unresolved] = []
        for p in sorted((p for p in parsed.places if p.ref in used), key=lambda p: _ORDER[p.located]):
            same_day = []
            for ref in dict.fromkeys(r for d, _ in stops.get(p.ref, []) for r in parsed.days[d].stops if r != p.ref):
                found = place_table.get(self.conn, place_ids[ref]) if place_ids.get(ref) else None
                same_day.append(Nearby(names.get(ref, ref), found and found.lat, found and found.lon))
            ctx = Context(
                article_id=article_id,
                in_itinerary=p.ref in stops,
                where="、".join(f"{parsed.days[d].label} 第 {position} 站" for d, position in stops.get(p.ref, [])),
                same_day=same_day,
            )
            result = await self.resolve(p, ctx)
            if result.unresolved:
                undecided.append(result.unresolved)
            else:
                place_ids[p.ref] = result.place_id
        return place_ids, undecided

    async def resolve(self, p: PlaceRef, ctx: Context) -> Resolution:
        """解析一個地點。llm 或 OSM 呼叫失敗時例外往外丟，整批停下。"""
        log.info("%s 地點 %s：%s｜%s｜%s｜%s", ctx.article_id, p.ref, p.name_zh, p.name_ja, p.city or "城市不明", p.located)
        if p.located == LOCATED_UNKNOWN:
            if not ctx.in_itinerary:
                log.info("  猜不出來，不是行程的一站：不收入")
                return Resolution(excluded=True)
            answer = self.answers.for_guess(ctx.article_id, p.name_zh)
            if answer is None:
                log.info("  猜不出來，是行程的一站（%s）：等你決定", ctx.where)
                return Resolution(unresolved=_unresolved(NO_GUESS, p, ctx))
            return await self._apply(answer, NO_GUESS, p, ctx)

        guessed = p.located == LOCATED_GUESS
        # `猜的` 的 name_zh 是連鎖店名，不是某一家分店，不拿來查
        found = place_table.find(self.conn, name_ja=p.name_ja, name_zh=None if guessed else p.name_zh, city=p.city)
        if found is not None:
            log.info("  資料庫已有：place %d", found)
            return Resolution(place_id=found)
        answer = self.answers.for_place(p.name_zh, p.name_ja, p.city, guessed)
        if answer is not None:
            return await self._apply(answer, NOT_FOUND, p, ctx)

        tried: list[tuple[str, str]] = []
        name, city = p.name_ja, p.city
        for round_ in range(config.GEOCODE_ROUNDS):
            repeated = not name or (name, city) in tried
            candidates = [] if repeated else await self._search(name, city)
            tried.append((name, city))
            if repeated:
                log.info("  第 %d 次：llm 沒有給新的名稱，不查", round_ + 1)
            else:
                log.info("  第 %d 次查 OSM「%s %s」：%d 筆%s", round_ + 1, name, city, len(candidates),
                         "".join(f"\n    {i}. {c.name}｜{c.type}｜{c.address}" for i, c in enumerate(candidates, 1)))
            chosen = await ask_place.pick(p, ctx, candidates) if candidates else None
            if chosen is not None:
                place_id = self._add(chosen, p, zh=not guessed, ja=(p.name_ja, name))
                log.info("  用 %s/%d「%s」：place %d", chosen.osm_type, chosen.osm_id, chosen.name, place_id)
                return Resolution(place_id=place_id)
            if round_ + 1 < config.GEOCODE_ROUNDS:
                name, city = await ask_place.new_name(p, ctx, [n for n, _ in tried], had_results=bool(candidates))
                log.info("  llm 改提供名稱：「%s %s」", name, city)
        log.info("  查 %d 次都沒有結果：等你決定", config.GEOCODE_ROUNDS)
        return Resolution(unresolved=_unresolved(NOT_FOUND, p, ctx, tried=list(dict.fromkeys(n for n, _ in tried if n))))

    async def _apply(self, answer: unresolved.Answer, type_: str, p: PlaceRef, ctx: Context) -> Resolution:
        """照使用者填的答案處理。

        `猜不出來` 的 name_zh、name_ja 是「飯店」這種寫法，不是這個地點的名字，所以都不加進別名。
        """
        named = type_ == NOT_FOUND
        zh = named and p.located != LOCATED_GUESS
        ja = (p.name_ja,) if named else ()
        log.info("  照你填的答案「%s」處理", answer.text)
        if answer.kind == unresolved.EXCLUDE:
            return Resolution(excluded=True)
        if answer.kind == unresolved.COORD:
            place_id = place_table.add_manual(self.conn, name_ja=p.name_ja, name_zh=p.name_zh if zh else None,
                                              city=p.city, lat=answer.lat, lon=answer.lon)
            place_table.add_alias(self.conn, place_id, name_zh=p.name_zh if zh else None)
            for alias in ja:
                place_table.add_alias(self.conn, place_id, name_ja=alias)
            return Resolution(place_id=place_id)
        if answer.kind == unresolved.OSM:
            chosen = await asyncio.to_thread(self.osm.lookup, answer.osm_type, answer.osm_id)
        else:
            found = await self._search(answer.name, p.city)
            chosen, ja = (found[0] if found else None), (*ja, answer.name)
        if chosen is None:
            return Resolution(unresolved=_unresolved(type_, p, ctx, note=f"填的答案「{answer.text}」在 OSM 查不到，請改填"))
        return Resolution(place_id=self._add(chosen, p, zh=zh, ja=ja))

    async def _search(self, name: str, city: str) -> list[Candidate]:
        # OSM 每秒限 1 次，等的時候不要卡住其他文章的 llm 解析
        return await asyncio.to_thread(self.osm.search, name, city)

    def _add(self, c: Candidate, p: PlaceRef, zh: bool, ja: tuple[str, ...]) -> int:
        """加入資料庫：OSM 物件已經在資料庫就只加別名，否則新增 place 再加別名。種類和範圍由 OSM 物件決定。"""
        place_id = place_table.add(
            self.conn, osm_type=c.osm_type, osm_id=c.osm_id, kind=c.kind, name_ja=c.name or p.name_ja,
            name_zh=p.name_zh if zh else None, city=p.city, lat=c.lat, lon=c.lon,
            boundary=c.boundary if c.kind == AREA else None)
        place_table.add_alias(self.conn, place_id, name_zh=p.name_zh if zh else None)
        for alias in ja:
            place_table.add_alias(self.conn, place_id, name_ja=alias)
        return place_id


def _unresolved(type_: str, p: PlaceRef, ctx: Context, tried: list[str] | None = None, note: str = "") -> Unresolved:
    return Unresolved(
        type=type_, article_id=ctx.article_id, name_zh=p.name_zh, name_ja=p.name_ja, city=p.city,
        guessed=p.located == LOCATED_GUESS, sentence=p.sentence, tried=tried or [], reason=p.reason,
        where=ctx.where, same_day=[n.name for n in ctx.same_day], note=note)
