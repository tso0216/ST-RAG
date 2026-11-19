"""地點解析：一個地點從「找得到嗎」到有座標或捨棄的整個流程（流程圖在 `docs/geocode_mvp.drawio`）。

`located` 是 `是`、`猜的` 的地點：
1. 查資料庫正名、別名（`city` 也要相同），只查到一個就用它。
2. 請 llm 給搜尋計畫：幾個日文名稱和一個範圍（行政區、同一天某個地點周圍的圓、或全日本）。
   範圍對不到地圖（縣市寫錯、中心還沒定位）就把原因告訴 llm 請它改，最多重問 `config.RETRIES` 次。
3. 在範圍內用這些名稱搜尋本機地圖（OSM＋Overture）。範圍內沒有同名的（完全相同或包含）就自動放寬一次：
   市區町村 → 整個縣、圓的半徑 × `config.WIDEN_FACTOR`。
4. 有候選就問 llm 是哪一個（每筆附上座標、離中心和同一天地點多遠、附近有幾筆其他候選）；挑中就加入資料庫。
5. 沒有候選或都不是：把這一輪的結果告訴 llm，回到 2。查滿 `config.GEOCODE_ROUNDS` 輪還沒有就到 6。
6. 請 llm 上網查地址，再用國土地理院把地址轉成經緯度；查不到就捨棄。

`located` 是 `猜不出來` 的地點不查資料庫也不查地圖，直接捨棄。
捨棄的地點 place id 是 None，寫入時怎麼拿掉看 `store.write_article`。

每個地點不管結果如何都寫一行 log（`geocode_log`）；llm 或國土地理院出錯時先寫一行 `中斷` 再讓例外往外丟。
"""
import asyncio
import logging
import sqlite3
from collections.abc import Callable

from service.gsi import GsiClient
from v4 import place as place_table

from . import ask_place, config
from . import geocode_log as glog
from .data_schema import (AREA, CONTAINS, EXACT, LOCATED_GUESS, LOCATED_UNKNOWN, LOCATED_YES, Area, Candidate,
                          Context, Nearby, Parsed, PlaceRef, Resolution)
from .local_map import AreaError, LocalMap, normalize

log = logging.getLogger(__name__)

_ORDER = {LOCATED_YES: 0, LOCATED_GUESS: 1, LOCATED_UNKNOWN: 2}

class Geocoder:
    def __init__(self, conn: sqlite3.Connection, local_map: LocalMap,
                 gsi: GsiClient | None = None, log_file: glog.GeocodeLog | None = None):
        self.conn = conn
        self.map = local_map
        self.gsi = gsi or GsiClient()
        self.log_file = log_file

    async def resolve_article(self, article_id: str, parsed: Parsed,
                              on_place: Callable[[int, int, glog.Trace], None] | None = None,
                              ) -> dict[str, int | None]:
        """解析一篇文章用到的每個地點（`places_to_resolve`），各做一次。

        回傳地點編號 → place id，捨棄的是 None。
        on_place 有給的話，每做完一個地點呼叫一次（第幾個、共幾個、這個地點的 log），印進度用。
        """
        stops = _stops(parsed)
        names = {p.ref: p.name_zh for p in parsed.places}
        place_ids: dict[str, int | None] = {}
        todo = places_to_resolve(parsed)
        for no, p in enumerate(todo, 1):
            same_day = []
            for ref in dict.fromkeys(r for d, _ in stops.get(p.ref, []) for r in parsed.days[d].stops if r != p.ref):
                found = place_table.get(self.conn, place_ids[ref]) if place_ids.get(ref) else None
                same_day.append(Nearby(names.get(ref, ref), found and found.lat, found and found.lon))
            ctx = Context(article_id=article_id, same_day=same_day)
            result = await self.resolve(
                p, ctx, on_done=None if on_place is None else lambda trace, no=no: on_place(no, len(todo), trace))
            place_ids[p.ref] = result.place_id
        return place_ids

    async def resolve(self, p: PlaceRef, ctx: Context,
                      on_done: Callable[[glog.Trace], None] | None = None) -> Resolution:
        """解析一個地點，寫一行 log，有給 on_done 就把這行 log 交給它。
        llm 或國土地理院呼叫失敗時先寫一行 `中斷`，再讓例外往外丟（整批停下）。"""
        log.info("%s 地點 %s：%s｜%s｜%s｜%s", ctx.article_id, p.ref, p.name_zh, p.name_ja, p.city or "城市不明", p.located)
        trace = glog.Trace(p, ctx)
        try:
            result = await self._resolve(p, ctx, trace)
        except Exception as e:
            trace.result, trace.fail_reason = glog.STOPPED, f"{type(e).__name__}: {e}"
            self._write(trace)
            raise
        self._write(trace)
        if on_done is not None:
            on_done(trace)
        return result

    async def _resolve(self, p: PlaceRef, ctx: Context, trace: glog.Trace) -> Resolution:
        if p.located == LOCATED_UNKNOWN:
            return self._drop(trace, glog.NO_GUESS)

        guessed = p.located == LOCATED_GUESS
        # `猜的` 的 name_zh 是連鎖店名，不是某一家分店，不拿來查
        found = place_table.find(self.conn, name_ja=p.name_ja, name_zh=None if guessed else p.name_zh, city=p.city)
        if found is not None:
            log.info("  資料庫已有：place %d", found)
            return self._done(trace, glog.DATABASE, found)

        tried: list[str] = []                  # 查過的名稱，上網查地址用
        searched: list[tuple] = []             # 查過的（名稱, 範圍）
        history: list[str] = []                # 給 llm 看的每一輪結果
        for round_no in range(1, config.GEOCODE_ROUNDS + 1):
            entry = {"round": round_no, "names": [], "area": None, "plan_reason": None, "area_error": None,
                     "area_used": None, "widened": False, "skipped": False, "candidates": [],
                     "choice": None, "pick_reason": None, "outcome": None}
            trace.rounds.append(entry)
            plan, area, error = await self._plan(p, ctx, history, trace)
            entry.update(names=plan.names, area=plan.to_log(), plan_reason=plan.reason)
            if area is None:
                log.info("  第 %d 輪：範圍不能用，不查", round_no)
                entry["area_error"], entry["outcome"] = error, glog.AREA_INVALID
                history.append(_history(round_no, plan.names, str(plan.to_log() or "全日本"), f"範圍不能用：{error}"))
                continue
            key = (frozenset(normalize(n) for n in plan.names), area)
            if key in searched:
                log.info("  第 %d 輪：名稱和範圍跟之前一樣，不查", round_no)
                entry["skipped"], entry["outcome"] = True, glog.REPEATED
                history.append(_history(round_no, plan.names, area.label(), "跟之前的名稱和範圍一樣，沒有查"))
                continue
            searched.append(key)
            tried.extend(plan.names)
            candidates, used, widened_from = await self._search_area(plan.names, area)
            entry["area_used"], entry["widened"] = used.to_log(), widened_from is not None
            entry["candidates"] = [_candidate_entry(i, c, candidates, ctx, used) for i, c in enumerate(candidates, 1)]
            where = used.label() + (f"（{area.label()} 裡沒有同名的，自動放寬）" if widened_from else "")
            log.info("  第 %d 輪查本機地圖「%s」%s：%d 筆%s", round_no, "、".join(plan.names), where, len(candidates),
                     "".join(f"\n    {i}. {ask_place.describe_candidate(c, candidates, ctx.same_day, used)}（{c.match}）"
                             for i, c in enumerate(candidates, 1)))
            if not candidates:
                entry["outcome"] = glog.NO_CANDIDATES
                history.append(_history(round_no, plan.names, where, "沒有候選"))
                continue
            trace.llm_calls += 1
            picked = await ask_place.pick(p, ctx, candidates, used, widened_from)
            entry["choice"], entry["pick_reason"] = picked.choice, picked.reason
            if picked.choice:
                entry["outcome"] = glog.PICKED
                chosen = candidates[picked.choice - 1]
                place_id = self._add(chosen, p, zh=not guessed, ja=(p.name_ja,))
                log.info("  用 %s %s「%s」：place %d", chosen.source, chosen.source_id, chosen.name, place_id)
                return self._done(trace, glog.map_result(round_no), place_id)
            entry["outcome"] = glog.NONE_PICKED
            history.append(_history(round_no, plan.names, where, f"{len(candidates)} 筆候選，AI 都不選：{picked.reason}"))
        log.info("  本機地圖查 %d 輪都沒有結果：上網查地址", config.GEOCODE_ROUNDS)
        return await self._web(p, ctx, trace, list(dict.fromkeys(tried)))

    async def _plan(self, p: PlaceRef, ctx: Context, history: list[str],
                    trace: glog.Trace) -> tuple[ask_place.Plan, Area | None, str]:
        """請 llm 給搜尋計畫，把範圍對到地圖。範圍不能用就把原因告訴 llm 重問，最多 `config.RETRIES` 次。

        回傳（最後一次的計畫，範圍，範圍不能用的原因）；重問完還不能用時範圍是 None。
        """
        error = ""
        for _ in range(1 + config.RETRIES):
            trace.llm_calls += 1
            plan = await ask_place.plan(p, ctx, history, error)
            try:
                return plan, await asyncio.to_thread(self._area, plan, ctx), ""
            except AreaError as e:
                error = str(e)
                log.info("  範圍不能用：%s", error)
        return plan, None, error

    def _area(self, plan: ask_place.Plan, ctx: Context) -> Area:
        """把計畫的範圍變成 `Area`；不能用丟 `AreaError`（訊息給 llm 看）。"""
        if not plan.names:
            raise AreaError("names 至少要給一個日文名稱。")
        if not plan.near:
            return self.map.admin_area(plan.pref, plan.municipality)
        if not 1 <= plan.near <= len(ctx.same_day):
            raise AreaError(f"near 要是 <same_day> 裡的編號（1～{len(ctx.same_day)}）；沒有可以當中心的地點就改用行政區。")
        center = ctx.same_day[plan.near - 1]
        if center.lat is None or center.lon is None:
            raise AreaError(f"「{center.name}」還沒定位，不能當中心；改用已定位的地點或行政區。")
        low, high = config.CIRCLE_KM
        return Area(anchor=center.name, lat=center.lat, lon=center.lon, km=min(max(plan.km or low, low), high))

    async def _search_area(self, names: list[str], area: Area) -> tuple[list[Candidate], Area, Area | None]:
        """在範圍內搜尋；範圍內沒有同名的（完全相同或包含）就放寬一次，放寬後有同名的才用放寬的結果。

        回傳（候選，實際用的範圍，放寬前的範圍；沒放寬是 None）。
        """
        found = await asyncio.to_thread(self.map.search, names, area)
        wider = _widen(area)
        if wider is None or _has_same_name(found):
            return found, area, None
        more = await asyncio.to_thread(self.map.search, names, wider)
        if _has_same_name(more):
            return more, wider, area
        return found, area, None

    async def _web(self, p: PlaceRef, ctx: Context, trace: glog.Trace, tried: list[str]) -> Resolution:
        """請 llm 上網查地址，再用國土地理院轉成經緯度。"""
        trace.llm_calls += 1
        web = await ask_place.web_address(p, ctx, tried)
        trace.web = {"found": web.found, "address": web.address, "source_url": web.source_url,
                     "gsi_title": None, "lat": None, "lon": None}
        if not web.found:
            return self._drop(trace, glog.WEB_NOT_FOUND)
        hit = await asyncio.to_thread(self.gsi.search, web.address)
        if hit is None:
            log.info("  國土地理院查不到「%s」", web.address)
            return self._drop(trace, glog.GSI_NOT_FOUND)
        trace.web.update(gsi_title=hit.title, lat=hit.lat, lon=hit.lon)
        place_id = self._store(
            source=place_table.GSI, source_id=f"{hit.title}｜{p.name_ja}", kind=p.kind, name_ja=p.name_ja,
            lat=hit.lat, lon=hit.lon, boundary=None, p=p, zh=p.located != LOCATED_GUESS, ja=(p.name_ja,))
        log.info("  國土地理院「%s」：place %d", hit.title, place_id)
        return self._done(trace, glog.WEB, place_id)

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

    def _drop(self, trace: glog.Trace, reason: str) -> Resolution:
        trace.result, trace.fail_reason = glog.DROPPED, reason
        log.info("  捨棄：%s", reason)
        return Resolution()

    def _write(self, trace: glog.Trace) -> None:
        if self.log_file is not None:
            self.log_file.write(trace.record())


def places_to_resolve(parsed: Parsed) -> list[PlaceRef]:
    """一篇文章要解析的地點，照解析的順序。

    沒有被行程、place_note、move 用到的地點不解析。`猜的` 排在 `是` 後面做，
    這樣問 llm 是哪一家分店時，同一天的其他地點已經有座標可以比遠近。
    """
    used = set(_stops(parsed)) | {n.place for n in parsed.place_notes} | {
        r for m in parsed.moves for r in (m.origin, m.dest)}
    return sorted((p for p in parsed.places if p.ref in used), key=lambda p: _ORDER[p.located])


def _stops(parsed: Parsed) -> dict[str, list[tuple[int, int]]]:
    """地點編號 → 它在行程裡的每個位置（第幾天的 index, 第幾站）。"""
    stops: dict[str, list[tuple[int, int]]] = {}
    for d, day in enumerate(parsed.days):
        for position, ref in enumerate(day.stops, 1):
            stops.setdefault(ref, []).append((d, position))
    return stops


def _widen(area: Area) -> Area | None:
    """自動放寬一次：圓的半徑 × `config.WIDEN_FACTOR`、市區町村 → 整個縣；只有縣或全日本不放寬（None）。"""
    if area.is_circle and area.km is not None:
        return Area(anchor=area.anchor, lat=area.lat, lon=area.lon, km=area.km * config.WIDEN_FACTOR)
    if area.municipality:
        return Area(pref=area.pref)
    return None


def _has_same_name(candidates: list[Candidate]) -> bool:
    return any(c.match in (EXACT, CONTAINS) for c in candidates)


def _history(round_no: int, names: list[str], where: str, result: str) -> str:
    """給 llm 看的一輪結果：`第 1 輪：名稱 新宿駅｜範圍 東京 新宿区｜沒有候選`。"""
    return f"第 {round_no} 輪：名稱 {'、'.join(names) or '（沒給）'}｜範圍 {where}｜{result}"


def _candidate_entry(no: int, c: Candidate, candidates: list[Candidate], ctx: Context, area: Area) -> dict:
    near = ask_place.nearest(c, ctx.same_day)
    center = ask_place.center_km(c, area)
    return {"no": no, "source": c.source, "source_id": c.source_id, "name": c.name, "category": c.category,
            "pref": c.pref, "municipality": c.municipality, "address": c.address, "lat": c.lat, "lon": c.lon,
            "match": c.match, "similarity": c.similarity,
            "center_km": round(center, 2) if center is not None else None,
            "nearest": {"name": near[0], "km": round(near[1], 2)} if near else None,
            "nearby": ask_place.nearby_count(c, candidates)}
