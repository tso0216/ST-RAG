"""地點解析：一篇文章的地點從「找得到嗎」到有座標或解析不到的整個流程（流程圖在 `docs/geocode_mvp.drawio`）。

一篇文章的地點分批做，同一批一起問 llm（每次呼叫包好幾個地點，見 `ask_place`）：
- 先做 `located` 是 `是` 的，再做 `猜的`：問 llm 是哪一家分店時，同一天的其他地點已經有座標可以比遠近。
- 同一種一批最多 `config.GEOCODE_BATCH` 個，超過就分成好幾批，一批做完再做下一批。

一批裡的每個地點：
1. 查資料庫正名、別名（`city` 也要相同），只查到一個就用它。
2. 請 llm 給搜尋計畫：幾個日文名稱和一個範圍（行政區、同一天某個地點周圍的圓、或全日本）。
   範圍對不到地圖（縣市寫錯、中心還沒定位、llm 漏回）就把原因告訴 llm 請它改，最多重問 `config.RETRIES` 次。
3. 在範圍內用這些名稱搜尋本機地圖（OSM＋Overture）。範圍內沒有同名的（完全相同或包含）就自動放寬一次：
   市區町村 → 整個縣、圓的半徑 × `config.WIDEN_FACTOR`。
4. 有候選的地點一起問 llm 各是哪一個（每筆附上類別、地址、離中心和同一天地點多遠）；挑中就加入資料庫。
5. 沒有候選或都不是的：把這一輪的結果告訴 llm，一起回到 2。查滿 `config.GEOCODE_ROUNDS` 輪還沒有的到 6。
6. 一起請 llm 上網查地址，再用國土地理院把地址轉成經緯度；查不到就是解析不到。

`located` 是 `猜不出來` 的地點不查資料庫也不查地圖，直接算解析不到。
解析不到的地點 place id 是 None，照樣寫入（place id 留空，見 `store.write_article`）。

每個地點做完寫一段 log（`geocode_log`）；llm 或國土地理院出錯時，這一批還沒做完的地點各寫一段 `中斷`，再讓例外往外丟。
"""
import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from service.gsi import GsiClient
from v4 import place as place_table

from . import ask_place, config, usage
from . import geocode_log as glog
from .data_schema import (AREA, CONTAINS, EXACT, LOCATED_GUESS, LOCATED_UNKNOWN, LOCATED_YES, Area, Candidate,
                          Context, Nearby, Parsed, PlaceRef, Resolution)
from .local_map import AreaError, LocalMap, normalize

log = logging.getLogger(__name__)

_ORDER = {LOCATED_YES: 0, LOCATED_GUESS: 1, LOCATED_UNKNOWN: 2}


@dataclass
class _Job:
    """一批裡的一個地點：解析到哪裡了。"""
    place: PlaceRef
    trace: glog.Trace
    history: list[str] = field(default_factory=list)   # 給 llm 看的每一輪結果
    tried: list[str] = field(default_factory=list)     # 查過的名稱，上網查地址用
    searched: list[tuple] = field(default_factory=list)  # 查過的（名稱, 範圍）
    result: Resolution | None = None

    @property
    def ref(self) -> str:
        return self.place.ref

    @property
    def ctx(self) -> Context:
        return self.trace.ctx


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
        """解析一篇文章用到的每個地點（`places_to_resolve`），分批做。

        回傳地點編號 → place id，解析不到的是 None。
        on_place 有給的話，每做完一個地點呼叫一次（第幾個做完、共幾個、這個地點的 log），印進度用。
        """
        stops = _stops(parsed)
        names = {p.ref: p.name_zh for p in parsed.places}
        place_ids: dict[str, int | None] = {}
        todo = places_to_resolve(parsed)

        def context(p: PlaceRef) -> Context:
            same_day = []
            for ref in dict.fromkeys(r for d, _ in stops.get(p.ref, []) for r in parsed.days[d].refs if r != p.ref):
                found = place_table.get(self.conn, place_ids[ref]) if place_ids.get(ref) else None
                same_day.append(Nearby(names.get(ref, ref), found and found.lat, found and found.lon))
            return Context(article_id=article_id, same_day=same_day)

        def done(job: _Job) -> None:
            assert job.result is not None
            place_ids[job.ref] = job.result.place_id
            if on_place is not None:
                on_place(len(place_ids), len(todo), job.trace)

        for batch in _batches(todo):
            await self._batch(batch, context, done)
        return place_ids

    async def resolve(self, p: PlaceRef, ctx: Context,
                      on_done: Callable[[glog.Trace], None] | None = None) -> Resolution:
        """只解析一個地點（一批只有它）。on_done 有給就把它的 log 交給它。"""
        [job] = await self._batch([p], lambda _: ctx, None if on_done is None else lambda job: on_done(job.trace))
        assert job.result is not None
        return job.result

    async def _batch(self, places: list[PlaceRef], context: Callable[[PlaceRef], Context],
                     on_done: Callable[[_Job], None] | None) -> list[_Job]:
        """解析一批地點。context(地點) 給它現在的上下文（同一天的地點有了座標就會帶上），每一輪重拿一次。"""
        jobs = [_Job(p, glog.Trace(p, context(p))) for p in places]
        if self.log_file is not None and any(p.located != LOCATED_UNKNOWN for p in places):
            self.log_file.batch(jobs[0].ctx.article_id, places)

        def finish(job: _Job, result: Resolution) -> None:
            job.result = result
            self._write(job.trace)
            if on_done is not None:
                on_done(job)

        try:
            await self._run(jobs, context, finish)
        except Exception as e:
            for job in jobs:
                if job.result is None:
                    job.trace.result, job.trace.fail_reason = glog.STOPPED, f"{type(e).__name__}: {e}"
                    self._write(job.trace)
            raise
        return jobs

    async def _run(self, jobs: list[_Job], context: Callable[[PlaceRef], Context],
                   finish: Callable[[_Job, Resolution], None]) -> None:
        for job in jobs:
            p = job.place
            log.info("%s 地點 %s：%s｜%s｜%s｜%s", job.ctx.article_id, p.ref, p.name_zh, p.name_ja,
                     p.city or "城市不明", p.located)
            if p.located == LOCATED_UNKNOWN:
                finish(job, self._drop(job.trace, glog.NO_GUESS))
                continue
            # `猜的` 的 name_zh 是連鎖店名，不是某一家分店，不拿來查
            found = place_table.find(self.conn, name_ja=p.name_ja,
                                     name_zh=None if p.located == LOCATED_GUESS else p.name_zh, city=p.city)
            if found is not None:
                log.info("  資料庫已有：place %d", found)
                finish(job, self._done(job.trace, glog.DATABASE, found))

        for round_no in range(1, config.GEOCODE_ROUNDS + 1):
            active = [j for j in jobs if j.result is None]
            if not active:
                return
            for job in active:
                job.trace.ctx = context(job.place)
            await self._round(round_no, active, finish)

        left = [j for j in jobs if j.result is None]
        if left:
            log.info("上網查地址（地圖 %d 輪沒有結果）：%s", config.GEOCODE_ROUNDS,
                     "、".join(j.place.name_zh for j in left))
            await self._web(left, finish)

    async def _round(self, round_no: int, jobs: list[_Job], finish: Callable[[_Job, Resolution], None]) -> None:
        """一輪：一起問搜尋計畫 → 各自查本機地圖 → 有候選的一起問挑哪一筆。"""
        plans = await self._plan(jobs)
        asks: list[tuple[_Job, list[Candidate], ask_place.PickAsk, str]] = []
        for job in jobs:
            plan, area, error = plans[job.ref]
            asked = _plan_area(plan, job.ctx)
            entry: dict[str, Any] = {"round": round_no, "names": plan.names, "area": asked,
                     "plan_reason": plan.reason, "area_error": None, "area_used": None, "widened": False,
                     "candidates": [], "choice": None, "pick_reason": None, "outcome": None}
            job.trace.rounds.append(entry)
            if area is None:
                log.info("  %s 第 %d 輪：範圍不能用，不查", job.place.name_zh, round_no)
                entry["area_error"], entry["outcome"] = error, glog.AREA_INVALID
                job.history.append(_history(round_no, plan.names, asked, f"範圍不能用：{error}"))
                continue
            key = (frozenset(normalize(n) for n in plan.names), area)
            if key in job.searched:
                log.info("  %s 第 %d 輪：名稱和範圍重複，略過", job.place.name_zh, round_no)
                entry["outcome"] = glog.REPEATED
                job.history.append(_history(round_no, plan.names, area.label(), "跟之前的名稱和範圍一樣，沒有查"))
                continue
            job.searched.append(key)
            job.tried.extend(plan.names)
            candidates, used, widened_from = await self._search_area(plan.names, area, job.place.kind)
            entry["area_used"], entry["widened"] = used.label(), widened_from is not None
            entry["candidates"] = [
                f"{ask_place.describe_candidate(c, job.ctx.same_day, used)}"
                f"｜{c.lat:.4f}, {c.lon:.4f}｜{c.source_id}｜{c.match}｜重要度 {c.importance:.2f}" for c in candidates]
            where = used.label() + (f"（{area.label()} 裡沒有同名的，自動放寬）" if widened_from else "")   # 給 llm 看
            log.info("  %s 第 %d 輪查本機地圖「%s」%s：%d 筆", job.place.name_zh, round_no, "、".join(plan.names),
                     used.label() + (f"（從 {area.label()} 放寬）" if widened_from else ""), len(candidates))
            if not candidates:
                entry["outcome"] = glog.NO_CANDIDATES
                job.history.append(_history(round_no, plan.names, where, "沒有候選"))
                continue
            asks.append((job, candidates,
                         ask_place.PickAsk(job.place, job.ctx, candidates, used, widened_from), where))
        if not asks:
            return

        for job, *_ in asks:
            job.trace.llm_calls += 1
        picks = await ask_place.pick([a for _, _, a, _ in asks])
        for job, candidates, _, where in asks:
            picked, entry = picks[job.ref], job.trace.rounds[-1]   # entry: 這一輪的 log
            entry["choice"], entry["pick_reason"] = picked.choice, picked.reason
            if picked.choice:
                entry["outcome"] = glog.PICKED
                chosen = candidates[picked.choice - 1]
                place_id = self._add(chosen, job.place, zh=job.place.located != LOCATED_GUESS, ja=(job.place.name_ja,))
                log.info("  %s 用 %s %s「%s」：place %d", job.place.name_zh, chosen.source, chosen.source_id,
                         chosen.name, place_id)
                finish(job, self._done(job.trace, glog.map_result(round_no), place_id))
                continue
            entry["outcome"] = glog.NONE_PICKED
            job.history.append(_history(round_no, entry["names"], where,
                                        f"{len(candidates)} 筆候選，AI 都不選：{picked.reason}"))

    async def _plan(self, jobs: list[_Job]) -> dict[str, tuple[ask_place.Plan, Area | None, str]]:
        """一起請 llm 給搜尋計畫，把範圍對到地圖。範圍不能用的（含 llm 漏回的）把原因告訴 llm，
        只拿這些再一起問，最多重問 `config.RETRIES` 次。

        回傳 地點編號 →（最後一次的計畫，範圍，範圍不能用的原因）；重問完還不能用時範圍是 None。
        """
        result: dict[str, tuple[ask_place.Plan, Area | None, str]] = {}
        errors = {j.ref: "" for j in jobs}
        todo = jobs
        for _ in range(1 + config.RETRIES):
            for job in todo:
                job.trace.llm_calls += 1
            plans = await ask_place.plan([ask_place.PlanAsk(j.place, j.ctx, j.history, errors[j.ref]) for j in todo])
            retry = []
            for job in todo:
                plan = plans[job.ref]
                try:
                    with usage.part(usage.MAP):
                        area = await asyncio.to_thread(self._area, plan, job.ctx)
                    result[job.ref] = (plan, area, "")
                except AreaError as e:
                    errors[job.ref] = str(e)
                    result[job.ref] = (plan, None, str(e))
                    retry.append(job)
                    log.info("  %s 範圍不能用：%s", job.place.name_zh, e)
            todo = retry
            if not todo:
                break
        return result

    def _area(self, plan: ask_place.Plan, ctx: Context) -> Area:
        """把計畫的範圍變成 `Area`；不能用丟 `AreaError`（訊息給 llm 看）。"""
        if not plan.answered:
            raise AreaError("上一次的回覆裡沒有這個地點，這次一定要回。")
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

    async def _search_area(self, names: list[str], area: Area, kind: str) -> tuple[list[Candidate], Area, Area | None]:
        """在範圍內搜尋（kind：要找的種類，排序用）；範圍內沒有同名的（完全相同或包含）就放寬一次，
        放寬後有同名的才用放寬的結果。

        回傳（候選，實際用的範圍，放寬前的範圍；沒放寬是 None）。
        """
        with usage.part(usage.MAP):
            found = await asyncio.to_thread(self.map.search, names, area, kind)
        wider = _widen(area)
        if wider is None or _has_same_name(found):
            return found, area, None
        with usage.part(usage.MAP):
            more = await asyncio.to_thread(self.map.search, names, wider, kind)
        if _has_same_name(more):
            return more, wider, area
        return found, area, None

    async def _web(self, jobs: list[_Job], finish: Callable[[_Job, Resolution], None]) -> None:
        """一起請 llm 上網查地址，再各自用國土地理院轉成經緯度。"""
        for job in jobs:
            job.trace.llm_calls += 1
        found = await ask_place.web_address(
            [ask_place.WebAsk(j.place, j.ctx, list(dict.fromkeys(j.tried))) for j in jobs])
        for job in jobs:
            p, web, trace = job.place, found[job.ref], job.trace
            trace.web = {"found": web.found, "address": web.address, "source_url": web.source_url,
                         "gsi_title": None, "lat": None, "lon": None}
            if not web.found:
                finish(job, self._drop(trace, glog.WEB_NOT_FOUND))
                continue
            with usage.part(usage.GSI):
                hit = await asyncio.to_thread(self.gsi.search, web.address)
            if hit is None:
                log.info("  國土地理院查不到「%s」", web.address)
                finish(job, self._drop(trace, glog.GSI_NOT_FOUND))
                continue
            trace.web.update(gsi_title=hit.title, lat=hit.lat, lon=hit.lon)
            place_id = self._store(
                source=place_table.GSI, source_id=f"{hit.title}｜{p.name_ja}", kind=p.kind, name_ja=p.name_ja,
                lat=hit.lat, lon=hit.lon, boundary=None, p=p, zh=p.located != LOCATED_GUESS, ja=(p.name_ja,))
            log.info("  %s 國土地理院「%s」：place %d", p.name_zh, hit.title, place_id)
            finish(job, self._done(trace, glog.WEB, place_id))

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
        log.info("  %s 解析不到：%s", trace.place.name_zh, reason)
        return Resolution()

    def _write(self, trace: glog.Trace) -> None:
        if self.log_file is not None:
            self.log_file.write(trace)


def places_to_resolve(parsed: Parsed) -> list[PlaceRef]:
    """一篇文章要解析的地點，照解析的順序。

    沒有被行程、place_note、move 用到的地點不解析。`猜的` 排在 `是` 後面做，
    這樣問 llm 是哪一家分店時，同一天的其他地點已經有座標可以比遠近。
    """
    used = set(_stops(parsed)) | {n.place for n in parsed.place_notes} | {
        r for m in parsed.moves for r in (m.origin, m.dest)}
    return sorted((p for p in parsed.places if p.ref in used), key=lambda p: _ORDER[p.located])


def _batches(places: list[PlaceRef]) -> list[list[PlaceRef]]:
    """照 `located` 分開（`places` 已經照 `_ORDER` 排好），每一種再切成最多 `config.GEOCODE_BATCH` 個一批。"""
    batches: list[list[PlaceRef]] = []
    for p in places:
        last = batches[-1] if batches else None
        if last and last[0].located == p.located and len(last) < config.GEOCODE_BATCH:
            last.append(p)
        else:
            batches.append([p])
    return batches


def _stops(parsed: Parsed) -> dict[str, list[tuple[int, int]]]:
    """地點編號 → 它在行程裡的每個位置（第幾天的 index, 第幾站）。"""
    stops: dict[str, list[tuple[int, int]]] = {}
    for d, day in enumerate(parsed.days):
        for position, ref in enumerate(day.refs, 1):
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


def _plan_area(plan: ask_place.Plan, ctx: Context) -> str:
    """llm 給的範圍寫成一句話：`東京 台東区`、`同一天第 1 個「淺草寺」方圓 1 公里`、`全日本`。"""
    if plan.near:
        anchor = ctx.same_day[plan.near - 1].name if 1 <= plan.near <= len(ctx.same_day) else "？"
        return f"同一天第 {plan.near} 個「{anchor}」方圓 {plan.km:g} 公里"
    return " ".join(x for x in (plan.pref, plan.municipality) if x) or "全日本"


def _history(round_no: int, names: list[str], where: str, result: str) -> str:
    """給 llm 看的一輪結果：`第 1 輪：名稱 新宿駅｜範圍 東京 新宿区｜沒有候選`。"""
    return f"第 {round_no} 輪：名稱 {'、'.join(names) or '（沒給）'}｜範圍 {where}｜{result}"
