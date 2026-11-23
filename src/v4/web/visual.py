"""工具呼叫 → 地圖場景（scene）。

畫什麼完全由程式決定，不經過 LLM：看工具名稱和參數決定範圍，看回傳裡的編號決定 LLM 實際拿到哪幾筆，
座標和內容再從資料庫查。所以翻頁、排序、篩選之後的結果，跟地圖上的點一致。
"""
import re
from typing import Any

from v4.rag import tools as T

BOUNDARY_TOLERANCE = 0.0008     # 區域邊界簡化的容許誤差（度，約 80 公尺）

_PLACE_LINE = re.compile(r"^id (\d+)｜", re.M)
_RECORD_LINE = re.compile(r"^\s*(?:(?:這天|整趟)的評語：)?\[(place_note|day_note|move|day) (\d+-\d+)\]", re.M)
_LEG_LINE = re.compile(r"^(\d+)→(\d+) (.+)$", re.M)
_KM = re.compile(r"直線 ([\d.]+) 公里")
CITATION = re.compile(r"\b(place_note|day_note|move|day)\s+(\d+-\d+)\b")


def _ring(ring: list) -> list | None:
    kept = [ring[0]]
    for x, y in ring[1:-1]:
        if abs(x - kept[-1][0]) > BOUNDARY_TOLERANCE or abs(y - kept[-1][1]) > BOUNDARY_TOLERANCE:
            kept.append((x, y))
    kept.append(ring[-1])
    return [[round(x, 5), round(y, 5)] for x, y in kept] if len(kept) >= 4 else None


def simplify(polygons: list) -> list:
    """縮小傳給瀏覽器的邊界；小到消失的島和洞拿掉，全部消失時用原本的。"""
    out = []
    for poly in polygons:
        outer = _ring(poly[0])
        if outer:
            out.append([outer, *(r for r in map(_ring, poly[1:]) if r)])
    return out or [[[[round(x, 5), round(y, 5)] for x, y in ring] for ring in poly] for poly in polygons]


class Scene:
    def __init__(self, env: T.Env):
        self.idx = env.index
        self.points: dict[int, dict] = {}
        self.areas: dict[int, dict] = {}
        self.lines: dict[tuple, dict] = {}
        self.circles: list[dict] = []
        self.evidence: dict[str, dict] = {}
        self.missing: list[str] = []        # 答案引用了、資料庫卻沒有的編號
        self._notes = {n.id: n for n in self.idx.notes}
        self._moves = {m.id: m for m in self.idx.moves}
        self._days: set[str] = set()

    # ---------- 圖形 ----------

    def point(self, pid: int, *, main: bool = False, seq: int | None = None, ref: str | None = None) -> None:
        """`main`：這次查詢的目標，畫大一點並標名稱。"""
        p = self.idx.places.get(pid)
        if p is None:
            return
        pt = self.points.setdefault(pid, {"id": pid, "name": p.name, "lat": p.lat, "lon": p.lon,
                                          "main": False, "seq": [], "refs": []})
        pt["main"] = pt["main"] or main
        if seq is not None:
            pt["seq"].append(seq)
        if ref and ref not in pt["refs"]:
            pt["refs"].append(ref)

    def area(self, pid: int) -> None:
        p = self.idx.places.get(pid)
        if p is not None and p.polygons and pid not in self.areas:
            self.areas[pid] = {"id": pid, "coordinates": simplify(p.polygons)}

    def scope(self, cond: Any) -> None:
        """地點條件（`地點`、`周圍`）畫成範圍。"""
        if not isinstance(cond, dict) or cond.get("mode") not in ("地點", "周圍"):
            return
        pid = cond.get("place_id")
        p = self.idx.places.get(pid)
        if p is None:
            return
        self.point(pid, main=True)
        if cond["mode"] == "周圍":
            self.circle(p, cond.get("km"))
        elif p.kind == T.AREA:
            self.area(pid)

    def circle(self, center: T.Place, km: Any) -> None:
        if isinstance(km, (int, float)) and not isinstance(km, bool) and km > 0:
            self.circles.append({"lat": center.lat, "lon": center.lon, "km": km})

    def link(self, kind: str, a: int, b: int, *, tag: str = "", ref: str | None = None) -> None:
        """兩地之間一條線；來回算同一條。"""
        pa, pb = self.idx.places.get(a), self.idx.places.get(b)
        if pa is None or pb is None or a == b:
            return
        line = self.lines.setdefault((kind, min(a, b), max(a, b)), {
            "kind": kind, "coords": [[pa.lat, pa.lon], [pb.lat, pb.lon]],
            "title": f"{pa.name} – {pb.name}", "tag": tag, "refs": []})
        if ref and ref not in line["refs"]:
            line["refs"].append(ref)

    def route(self, key: str, ids: list[int], title: str, ref: str | None = None) -> None:
        coords: list[list[float]] = []
        last = None
        for pid in ids:
            p = self.idx.places.get(pid)
            if p is not None and pid != last:
                coords.append([p.lat, p.lon])
                last = pid
        if len(coords) >= 2:
            self.lines[("route", key)] = {"kind": "route", "coords": coords, "title": title, "tag": "",
                                          "refs": [ref] if ref else []}

    # ---------- 經驗 ----------

    def note(self, rid: str) -> bool:
        n = self._notes.get(rid)
        if n is None:
            return False
        ref = f"place_note {rid}"
        name = self.idx.places[n.place].name if n.poi_id is not None else n.poi_name
        self.evidence[ref] = {"ref": ref, "title": name, "text": n.content,
                              "place_ids": [n.place] if n.place is not None else []}
        if n.place is not None:
            self.point(n.place, ref=ref)
        return True

    def move(self, rid: str) -> bool:
        m = self._moves.get(rid)
        if m is None:
            return False
        ref = f"move {rid}"
        origin = T.move_name(self.idx, m.origin, m.from_id, m.from_name)
        dest = T.move_name(self.idx, m.dest, m.to_id, m.to_name)
        ends = [pid for pid in (m.origin, m.dest) if pid is not None]
        self.evidence[ref] = {"ref": ref, "title": f"{origin} → {dest}", "text": m.content, "place_ids": ends}
        for pid in ends:
            self.point(pid, ref=ref)
        if len(ends) == 2:
            self.link("move", m.origin, m.dest, ref=ref)
        return True

    def day(self, did: str, cite: bool = True) -> bool:
        d = self.idx.days.get(did)
        if d is None:
            return False
        ref = f"day {did}"
        ids = []
        for st in d.stops:
            if st.place is not None:
                self.point(st.place, ref=ref if cite else None)
                ids.append(st.place)
        title = f"第 {T.day_no(did)} 天"
        self.route(did, ids, title, ref)
        self._days.add(did)
        if cite:
            self.evidence[ref] = {"ref": ref, "title": title, "text": T.day_stop_line(self.idx, d),
                                  "place_ids": list(dict.fromkeys(ids))}
        return True

    def day_note(self, rid: str, draw: bool = True) -> bool:
        """`draw=False`：只掛到已經畫出來的天，不多畫（search_days 的整趟評語不該帶出沒回傳的天）。"""
        x = self.idx.day_notes_by_id.get(rid)
        if x is None:
            return False
        ref = f"day_note {rid}"
        pids: list[int] = []
        for did in x.day_ids or self.idx.article_days.get(x.article_id, []):
            if draw and did not in self._days:
                self.day(did, cite=False)
            if did not in self._days:
                continue
            if ("route", did) in self.lines:
                self.lines[("route", did)]["refs"].append(ref)
            for st in self.idx.days[did].stops:
                if st.place is not None:
                    pids.append(st.place)
                    if st.place in self.points and ref not in self.points[st.place]["refs"]:
                        self.points[st.place]["refs"].append(ref)
        self.evidence[ref] = {"ref": ref, "title": T.day_note_span(self.idx, x, False), "text": x.content,
                              "place_ids": list(dict.fromkeys(pids))}
        return True

    def dump(self) -> dict:
        return {"points": list(self.points.values()), "areas": list(self.areas.values()), "circles": self.circles,
                "lines": list(self.lines.values()), "evidence": self.evidence, "missing": self.missing}


# ---------- 每個工具怎麼畫 ----------

def _records(result: str, kind: str) -> list[str]:
    return list(dict.fromkeys(rid for k, rid in _RECORD_LINE.findall(result) if k == kind))


def _find_place(s: Scene, env: T.Env, args: dict, result: str) -> None:
    ids = [int(x) for x in _PLACE_LINE.findall(result)]
    for pid in ids:
        s.point(pid, main=len(ids) == 1)
        s.area(pid)


def _list_places(s: Scene, env: T.Env, args: dict, result: str) -> None:
    within = args.get("within") or {}
    if "area" in within:
        s.scope({"mode": "地點", "place_id": within["area"]})
    elif "around" in within:
        s.scope({"mode": "周圍", "place_id": within["around"], "km": within.get("km")})
    for pid in _PLACE_LINE.findall(result):
        s.point(int(pid))


def _measure(s: Scene, env: T.Env, args: dict, result: str) -> None:
    stops, _ = T.expand_stops(env, args.get("stops"), 2)
    for i, st in enumerate(stops, 1):
        if st.place_id is None:
            continue
        s.point(st.place_id, main=True, seq=i)
        if s.idx.places[st.place_id].kind == T.AREA:
            s.area(st.place_id)
    for a, b, rest in _LEG_LINE.findall(result):
        start, end = stops[int(a) - 1], stops[int(b) - 1]
        if start.place_id is not None and end.place_id is not None:
            km = _KM.search(rest)
            s.link("leg", start.place_id, end.place_id, tag=f"{km.group(1)} km" if km else "")


def _search_notes(s: Scene, env: T.Env, args: dict, result: str) -> None:
    s.scope(args.get("place"))
    for rid in _records(result, "place_note"):
        s.note(rid)


def _search_moves(s: Scene, env: T.Env, args: dict, result: str) -> None:
    s.scope(args.get("origin"))
    s.scope(args.get("dest"))
    for rid in _records(result, "move"):
        s.move(rid)


def _search_days(s: Scene, env: T.Env, args: dict, result: str) -> None:
    stops, _ = T.expand_stops(env, args.get("stops"), 1)
    for st in stops:
        if st.place_id is None:
            continue
        s.point(st.place_id, main=True)
        if s.idx.places[st.place_id].kind == T.AREA:
            s.area(st.place_id)
    for did in _records(result, "day"):
        s.day(did)
    for rid in _records(result, "day_note"):
        s.day_note(rid, draw=False)


def _search_day_notes(s: Scene, env: T.Env, args: dict, result: str) -> None:
    for pid in args.get("places") or []:
        s.point(pid, main=True)
        s.area(pid)
    for rid in _records(result, "day_note"):
        s.day_note(rid)


def _read_article(s: Scene, env: T.Env, args: dict, result: str) -> None:
    for did in _records(result, "day"):
        s.day(did)
    for rid in _records(result, "place_note"):
        s.note(rid)
    for rid in _records(result, "move"):
        s.move(rid)
    for rid in _records(result, "day_note"):
        s.day_note(rid, draw=False)


_DRAW = {"find_place": _find_place, "list_places": _list_places, "measure": _measure, "search_notes": _search_notes,
         "search_moves": _search_moves, "search_days": _search_days, "search_day_notes": _search_day_notes,
         "read_article": _read_article}


def tool_scene(env: T.Env, name: str, args: dict, result: str, is_error: bool) -> dict:
    s = Scene(env)
    # 工具錯誤通常沒有結果可畫；find_place 找不到時會附名稱相近的候選，照樣畫
    if name in _DRAW and (not is_error or name == "find_place"):
        _DRAW[name](s, env, args or {}, result)
    return s.dump()


def itinerary_scene(env: T.Env) -> dict:
    s = Scene(env)
    for up in env.places.values():
        if up.place_id is not None:
            s.point(up.place_id, main=True)
            s.area(up.place_id)
    for day in env.days.values():
        ids = []
        for i, st in enumerate(day.stops, 1):
            if st.place_id is not None:
                s.point(st.place_id, main=True, seq=i if len(env.days) == 1 else None)
                ids.append(st.place_id)
        s.route(day.code, ids, f"{day.code} {day.label}".strip())
    return s.dump()


def answer_scene(env: T.Env, answer: str) -> dict:
    """答案實際引用到的經驗。引用了資料庫沒有的編號放進 `missing`。"""
    s = Scene(env)
    draw = {"place_note": s.note, "move": s.move, "day": s.day, "day_note": s.day_note}
    refs = list(dict.fromkeys((kind, rid) for kind, rid in CITATION.findall(answer)))
    for kind, rid in sorted(refs, key=lambda r: r[0] == "day_note"):    # 天先畫，評語才掛得上去
        if not draw[kind](rid):
            s.missing.append(f"{kind} {rid}")
    return s.dump()
