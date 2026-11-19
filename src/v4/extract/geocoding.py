"""地點解析：一個地點從「找得到嗎」到有結果或要使用者決定的整個流程（流程圖在 `docs/geocode_mvp.drawio`）。

`located` 是 `是`、`猜的` 的地點：
1. 查資料庫正名、別名（`city` 也要相同），只查到一個就用它。
2. 查使用者填的答案（`place_unresolved.tsv`），有就照填的處理。
3. 用日文名稱和城市在本機地圖（OSM＋Overture）模糊搜尋，有候選就問 llm 是哪一個；挑中就加入資料庫。
4. 沒有候選或都不是：請 llm 換一個名稱和城市，回到 3。查滿 `config.GEOCODE_ROUNDS` 輪還沒有就到 5。
5. 請 llm 上網查地址，再用國土地理院把地址轉成經緯度；查不到就記下來等使用者決定。

`located` 是 `猜不出來` 的地點不查資料庫也不查地圖：不是行程的一站就不收入；
是行程的一站就看使用者替這一篇填的答案，沒填就記下來。

每個地點不管結果如何都寫一行 log（`geocode_log`）；llm 或國土地理院出錯時先寫一行 `中斷` 再讓例外往外丟。
"""
import asyncio
import logging
import sqlite3

from service.gsi import GsiClient
from v4 import place as place_table

from . import ask_place, config, unresolved
from . import geocode_log as glog
from .data_schema import (AREA, LOCATED_GUESS, LOCATED_UNKNOWN, LOCATED_YES, NO_GUESS, NOT_FOUND, OSM, Candidate,
                          Context, Nearby, Parsed, PlaceRef, Resolution, Unresolved)
from .local_map import LocalMap

log = logging.getLogger(__name__)

_ORDER = {LOCATED_YES: 0, LOCATED_GUESS: 1, LOCATED_UNKNOWN: 2}

NO_GUESS_WAITING = "猜不出來，是行程的一站"


class Geocoder:
    def __init__(self, conn: sqlite3.Connection, local_map: LocalMap, answers: unresolved.Answers,
                 gsi: GsiClient | None = None, log_file: glog.GeocodeLog | None = None):
        self.conn = conn
        self.map = local_map
        self.answers = answers
        self.gsi = gsi or GsiClient()
        self.log_file = log_file

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
        """解析一個地點，寫一行 log。llm 或國土地理院呼叫失敗時先寫一行 `中斷`，再讓例外往外丟（整批停下）。"""
        log.info("%s 地點 %s：%s｜%s｜%s｜%s", ctx.article_id, p.ref, p.name_zh, p.name_ja, p.city or "城市不明", p.located)
        trace = glog.Trace(p, ctx)
        try:
            result = await self._resolve(p, ctx, trace)
        except Exception as e:
            trace.result, trace.fail_reason = glog.STOPPED, f"{type(e).__name__}: {e}"
            self._write(trace)
            raise
        self._write(trace)
        return result

    async def _resolve(self, p: PlaceRef, ctx: Context, trace: glog.Trace) -> Resolution:
        if p.located == LOCATED_UNKNOWN:
            if not ctx.in_itinerary:
                log.info("  猜不出來，不是行程的一站：不收入")
                trace.result = glog.EXCLUDED
                return Resolution(excluded=True)
            answer = self.answers.for_guess(ctx.article_id, p.name_zh)
            if answer is None:
                log.info("  猜不出來，是行程的一站（%s）：等你決定", ctx.where)
                return self._waiting(trace, NO_GUESS_WAITING, NO_GUESS, p, ctx)
            return await self._apply(answer, NO_GUESS, p, ctx, trace)

        guessed = p.located == LOCATED_GUESS
        # `猜的` 的 name_zh 是連鎖店名，不是某一家分店，不拿來查
        found = place_table.find(self.conn, name_ja=p.name_ja, name_zh=None if guessed else p.name_zh, city=p.city)
        if found is not None:
            log.info("  資料庫已有：place %d", found)
            return self._done(trace, glog.DATABASE, found)
        answer = self.answers.for_place(p.name_zh, p.name_ja, p.city, guessed)
        if answer is not None:
            return await self._apply(answer, NOT_FOUND, p, ctx, trace)

        tried: list[tuple[str, str]] = []
        name, city = p.name_ja, p.city
        for round_no in range(1, config.GEOCODE_ROUNDS + 1):
            entry = {"round": round_no, "query": name, "city": city, "skipped": False, "candidates": [],
                     "choice": None, "pick_reason": None, "outcome": None, "next": None}
            trace.rounds.append(entry)
            repeated = not name or (name, city) in tried
            tried.append((name, city))
            candidates: list[Candidate] = []
            if repeated:
                log.info("  第 %d 輪：llm 沒有給新的名稱，不查", round_no)
                entry["skipped"], entry["outcome"] = True, glog.REPEATED
            else:
                candidates = await self._search(name, city)
                entry["candidates"] = [_candidate_entry(i, c, ctx) for i, c in enumerate(candidates, 1)]
                log.info("  第 %d 輪查本機地圖「%s %s」：%d 筆%s", round_no, name, city, len(candidates),
                         "".join(f"\n    {i}. {ask_place.describe_candidate(c, ctx.same_day)}（{c.match}）"
                                 for i, c in enumerate(candidates, 1)))
                if not candidates:
                    entry["outcome"] = glog.NO_CANDIDATES
                else:
                    trace.llm_calls += 1
                    picked = await ask_place.pick(p, ctx, candidates)
                    entry["choice"], entry["pick_reason"] = picked.choice, picked.reason
                    if picked.choice:
                        entry["outcome"] = glog.PICKED
                        chosen = candidates[picked.choice - 1]
                        place_id = self._add(chosen, p, zh=not guessed, ja=(p.name_ja, name))
                        log.info("  用 %s %s「%s」：place %d", chosen.source, chosen.source_id, chosen.name, place_id)
                        return self._done(trace, glog.map_result(round_no), place_id)
                    entry["outcome"] = glog.NONE_PICKED
            if round_no < config.GEOCODE_ROUNDS:
                trace.llm_calls += 1
                name, city = await ask_place.new_name(p, ctx, [n for n, _ in tried], had_results=bool(candidates))
                entry["next"] = {"name_ja": name, "city": city}
                log.info("  llm 改提供名稱：「%s %s」", name, city)
        log.info("  本機地圖查 %d 輪都沒有結果：上網查地址", config.GEOCODE_ROUNDS)
        return await self._web(p, ctx, trace, list(dict.fromkeys(n for n, _ in tried if n)))

    async def _web(self, p: PlaceRef, ctx: Context, trace: glog.Trace, tried: list[str]) -> Resolution:
        """請 llm 上網查地址，再用國土地理院轉成經緯度。"""
        trace.llm_calls += 1
        web = await ask_place.web_address(p, ctx, tried)
        trace.web = {"found": web.found, "address": web.address, "source_url": web.source_url,
                     "gsi_title": None, "lat": None, "lon": None}
        if not web.found:
            return self._waiting(trace, glog.WEB_NOT_FOUND, NOT_FOUND, p, ctx, tried=tried, note=_web_note(web))
        hit = await asyncio.to_thread(self.gsi.search, web.address)
        if hit is None:
            log.info("  國土地理院查不到「%s」：等你決定", web.address)
            return self._waiting(trace, glog.GSI_NOT_FOUND, NOT_FOUND, p, ctx, tried=tried,
                                 note=_web_note(web, glog.GSI_NOT_FOUND))
        trace.web.update(gsi_title=hit.title, lat=hit.lat, lon=hit.lon)
        place_id = self._store(
            source=place_table.GSI, source_id=f"{hit.title}｜{p.name_ja}", kind=p.kind, name_ja=p.name_ja,
            lat=hit.lat, lon=hit.lon, boundary=None, p=p, zh=p.located != LOCATED_GUESS, ja=(p.name_ja,))
        log.info("  國土地理院「%s」：place %d", hit.title, place_id)
        return self._done(trace, glog.WEB, place_id)

    async def _apply(self, answer: unresolved.Answer, type_: str, p: PlaceRef, ctx: Context,
                     trace: glog.Trace) -> Resolution:
        """照使用者填的答案處理。

        `猜不出來` 的 name_zh、name_ja 是「飯店」這種寫法，不是這個地點的名字，所以都不加進別名。
        """
        named = type_ == NOT_FOUND
        zh = named and p.located != LOCATED_GUESS
        ja = (p.name_ja,) if named else ()
        log.info("  照你填的答案「%s」處理", answer.text)
        if answer.kind == unresolved.EXCLUDE:
            trace.result = glog.EXCLUDED
            return Resolution(excluded=True)
        if answer.kind == unresolved.COORD:
            place_id = place_table.add_manual(self.conn, name_ja=p.name_ja, name_zh=p.name_zh if zh else None,
                                              city=p.city, lat=answer.lat, lon=answer.lon)
            self._alias(place_id, p, zh, ja)
            return self._done(trace, glog.ANSWER, place_id)
        if answer.kind == unresolved.OSM:
            chosen = await asyncio.to_thread(self.map.lookup, OSM, answer.osm_ref)
        else:
            found = await self._search(answer.name, p.city)
            chosen, ja = (found[0] if found else None), (*ja, answer.name)
        if chosen is None:
            note = f"填的答案「{answer.text}」在本機地圖查不到，請改填座標"
            return self._waiting(trace, note, type_, p, ctx, note=note)
        return self._done(trace, glog.ANSWER, self._add(chosen, p, zh=zh, ja=ja))

    async def _search(self, name: str, city: str) -> list[Candidate]:
        # 本機地圖一次查詢要掃整張表，丟到 thread，等的時候不要卡住其他文章的 llm 解析
        return await asyncio.to_thread(self.map.search, name, city)

    def _add(self, c: Candidate, p: PlaceRef, zh: bool, ja: tuple[str, ...]) -> int:
        """用本機地圖的候選：來源、名稱、種類、範圍用候選的；city 和別名照 llm 給的。"""
        return self._store(source=c.source, source_id=c.source_id, kind=c.kind, name_ja=c.name or p.name_ja,
                           lat=c.lat, lon=c.lon, boundary=c.boundary if c.kind == AREA else None, p=p, zh=zh, ja=ja)

    def _store(self, *, source: str, source_id: str, kind: str, name_ja: str, lat: float, lon: float,
               boundary: dict | None, p: PlaceRef, zh: bool, ja: tuple[str, ...]) -> int:
        """加入資料庫：同一個來源已經在資料庫就只加別名，否則新增 place 再加別名。"""
        place_id = place_table.add(
            self.conn, source=source, source_id=source_id, kind=kind, name_ja=name_ja,
            name_zh=p.name_zh if zh else None, city=p.city, lat=lat, lon=lon, boundary=boundary)
        self._alias(place_id, p, zh, ja)
        return place_id

    def _alias(self, place_id: int, p: PlaceRef, zh: bool, ja: tuple[str, ...]) -> None:
        place_table.add_alias(self.conn, place_id, name_zh=p.name_zh if zh else None)
        for alias in ja:
            place_table.add_alias(self.conn, place_id, name_ja=alias)

    def _done(self, trace: glog.Trace, result: str, place_id: int) -> Resolution:
        found = place_table.get(self.conn, place_id)
        source, source_id = place_table.source_of(self.conn, place_id) or (None, None)
        trace.result = result
        trace.used = {"id": place_id, "source": source, "source_id": source_id, "name": found.name_ja,
                      "lat": found.lat, "lon": found.lon, "google_maps": glog.maps_url(found.lat, found.lon)}
        return Resolution(place_id=place_id)

    def _waiting(self, trace: glog.Trace, reason: str, type_: str, p: PlaceRef, ctx: Context,
                 tried: list[str] | None = None, note: str = "") -> Resolution:
        trace.result, trace.fail_reason = glog.WAITING, reason
        log.info("  等你決定：%s", reason)
        return Resolution(unresolved=_unresolved(type_, p, ctx, tried=tried, note=note))

    def _write(self, trace: glog.Trace) -> None:
        if self.log_file is not None:
            self.log_file.write(trace.record())


def _candidate_entry(no: int, c: Candidate, ctx: Context) -> dict:
    near = ask_place.nearest(c, ctx.same_day)
    return {"no": no, "source": c.source, "source_id": c.source_id, "name": c.name, "category": c.category,
            "pref": c.pref, "municipality": c.municipality, "address": c.address, "match": c.match,
            "similarity": c.similarity,
            "nearest": {"name": near[0], "km": round(near[1], 2)} if near else None}


def _web_note(web: ask_place.WebAddress, problem: str = "") -> str:
    """上網查到的地址和網址，寫進「待人工補」清單的說明；什麼都沒查到是空字串。"""
    parts = [f"上網查到的地址：{web.address}" if web.address else "", f"網址：{web.source_url}" if web.source_url else ""]
    text = "，".join(x for x in parts if x)
    return f"{text}（{problem}）" if text and problem else text


def _unresolved(type_: str, p: PlaceRef, ctx: Context, tried: list[str] | None = None, note: str = "") -> Unresolved:
    return Unresolved(
        type=type_, article_id=ctx.article_id, name_zh=p.name_zh, name_ja=p.name_ja, city=p.city,
        guessed=p.located == LOCATED_GUESS, sentence=p.sentence, tried=tried or [], reason=p.reason,
        where=ctx.where, same_day=[n.name for n in ctx.same_day], note=note)
