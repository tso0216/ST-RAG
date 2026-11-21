"""④ 驗證計畫（規格 11）：檢查節點、解析節點裡的地點、比對出改了什麼、去重複、數量上限。

③ 和 ⑦ 輸出的節點都經過這裡，依序檢查：
1. 格式（`check_format`）：不合法的由 `plan.py`／`replan.py` 附上問題清單請 LLM 重新輸出一次，還不合法的拿掉。
2. 地點解析（`Resolver`）：地點條件裡的地點都要解析得到；排法只要至少一站解析得到。解析不到的節點拿掉，不重問。
3. 改了什麼（`diff`）：⑦ 的節點填了「從哪個節點放寬」時，程式比對兩個節點列出哪些欄位不同。只記錄，不擋。
4. 不重複（`node_key`）：跟這一題已經有的節點（含查無資料清單裡的）一樣就拿掉。
5. 數量：③ 最多 40 個；⑦ 每輪最多 5 個；整題最多 40 個。超過的照輸出順序從後面拿掉。
`accept` 照輸出順序一個一個做 2–5；名額滿了之後的節點直接記成超過上限，不再解析地點。
"""
import logging
import re
from dataclasses import dataclass, field

from v4.extract.check import Problem
from v4.extract.data_schema import LOCATED_YES, Context, PlaceRef
from v4.extract.geocoding import Geocoder

from . import config
from .data_schema import (ANYWHERE, AROUND, DAY_TYPES, DROP_DUPLICATE, DROP_FORMAT, DROP_OVER, DROP_UNRESOLVED,
                          NO, NODE_DAY, NODE_KINDS, NODE_MOVE, NODE_POINT, PLACE_KINDS, SCOPE_MODES, SPOT, YES,
                          Batch, Dropped, Node, Scope, Stop, TimeCond, UserDay, Where)

log = logging.getLogger(__name__)

_DAY_RE = re.compile(r"^D(\d+)$")


class Resolver:
    """解析節點裡的地點（11.2）。同一篇發問文裡同一個地點只解析一次，結果共用（`cache`）。

    ① 已經解析過的（行程裡的站）直接用結果。編號的地點用 ① 地點列表的名稱、城市和原文那一行；
    新地點用節點裡寫的名稱和城市，原文句子填這一題的問句；located 都當成 `是`。
    每個地點的解析結果記在 `log`（紀錄用）。
    """

    def __init__(self, geocoder: Geocoder | None, article_id: str, places: dict[str, PlaceRef],
                 resolved: dict[str, int | None] | None = None):
        self.geocoder = geocoder
        self.article_id = article_id
        self.places = places
        self.cache: dict[tuple, int | None] = {("ref", r): pid for r, pid in (resolved or {}).items()}
        self.log: list[dict] = []
        self.calls = 0   # 實際交給 Geocoder 解析的次數

    async def resolve(self, where: Where, question: str) -> int | None:
        key = where.key()
        if key in self.cache:
            return self.cache[key]
        if where.ref:
            p = self.places[where.ref]
            ref = PlaceRef(where.ref, p.name_zh, p.name_ja, p.city, p.kind, LOCATED_YES, "", p.sentence)
        else:
            ref = PlaceRef("new", where.name_zh, where.name_ja, where.city, where.kind, LOCATED_YES, "", question)
        if self.geocoder is None:
            raise RuntimeError("沒有 Geocoder，不能解析地點")
        traces: list = []
        self.calls += 1
        result = await self.geocoder.resolve(ref, Context(article_id=self.article_id), on_done=traces.append)
        self.cache[key] = result.place_id
        t = traces[0] if traces else None
        self.log.append({"地點": where.label, "編號": where.ref, "日文名稱": ref.name_ja, "城市": ref.city,
                         "種類": ref.kind, "place_id": result.place_id,
                         "結果": t.result if t else None, "原因": t.fail_reason if t else None,
                         "用的": t.used if t else None})
        return result.place_id


@dataclass
class Ctx:
    """驗證要用的：① 的地點列表、使用者的行程、發文年份、問句、地點解析。"""
    places: dict[str, PlaceRef]
    days: list[UserDay]
    posted_year: int
    question: str
    resolver: Resolver
    extra: dict = field(default_factory=dict)


# ---------- 1. 格式 ----------

def _check_place(p: dict, field_name: str, ctx: Ctx) -> list[str]:
    ref = (p.get("ref") or "").strip()
    if ref:
        return [] if ref in ctx.places else [f"{field_name}.ref「{ref}」不在地點列表裡；列表裡沒有的地點 ref 留空，寫新地點"]
    out = []
    if not (p.get("name_zh") or "").strip() or not (p.get("name_ja") or "").strip():
        out.append(f"{field_name} 是新地點，name_zh 和 name_ja 都要填")
    if p.get("kind") not in PLACE_KINDS:
        out.append(f"{field_name} 是新地點，kind 要是{'或'.join(PLACE_KINDS)}")
    return out


def _check_scope(sc: dict, field_name: str, ctx: Ctx) -> list[str]:
    mode = sc.get("mode")
    if mode not in SCOPE_MODES:
        return [f"{field_name}.mode 要是{'、'.join(SCOPE_MODES)}其中一個"]
    if mode == ANYWHERE:
        return []
    out = _check_place(sc.get("place") or {}, f"{field_name}.place", ctx)
    if mode == AROUND and not (isinstance(sc.get("km"), (int, float)) and sc["km"] > 0):
        out.append(f"{field_name} 是周圍，km（半徑，公里）要大於 0")
    return out


def _has_place(p: dict) -> bool:
    return any((p.get(k) or "").strip() for k in ("ref", "name_zh", "name_ja"))


def _check_stops(stops: list[dict], ctx: Ctx) -> list[str]:
    out = []
    for j, s in enumerate(stops):
        day = (s.get("day") or "").strip()
        place = s.get("place") or {}
        if day and _has_place(place):
            out.append(f"stops[{j}] 只能寫 day（天的編號）或 place（地點）其中一個")
        elif day:
            m = _DAY_RE.match(day)
            if not ctx.days:
                out.append(f"stops[{j}] 的 {day}：沒有按天排的行程，不能用天的編號，直接寫地點")
            elif not m or not 1 <= int(m.group(1)) <= len(ctx.days):
                out.append(f"stops[{j}] 的天的編號「{day}」要是 D1～D{len(ctx.days)}")
        elif _has_place(place):
            out += _check_place(place, f"stops[{j}].place", ctx)
        else:
            out.append(f"stops[{j}] 是空的：要寫 day 或 place")
    return out


def _check_time(n: dict, ctx: Ctx) -> list[str]:
    out = []
    year = (n.get("year") or "").strip()
    if year and (not re.fullmatch(r"\d{4}", year) or int(year) > ctx.posted_year):
        out.append(f"year「{year}」要是四位數的年，而且不晚於發文年份 {ctx.posted_year}；不限留空")
    for m in n.get("months") or []:
        if not isinstance(m, int) or not 1 <= m <= 12:
            out.append(f"months 的 {m} 不是 1–12")
    if (n.get("day_type") or "") not in ("", *DAY_TYPES):
        out.append(f"day_type 要是{'或'.join(DAY_TYPES)}，不限留空")
    for h in n.get("hours") or []:
        a, b = h.get("start"), h.get("end")
        if not all(isinstance(x, int) and 0 <= x <= 23 for x in (a, b)):
            out.append(f"hours 的 {a}–{b}：兩端都要是 0–23 的整數")
    if n.get("kind") == NODE_DAY and ((n.get("day_type") or "") or n.get("hours")):
        out.append("天節點只能填年份、月份，不能填平日／假日和時段")
    return out


def check_format(raws: list[dict], ctx: Ctx, *, known: set[str] = frozenset(),
                 relaxable: set[str] | None = None) -> list[Problem]:
    """11.1 第 1 項。known 是這一題已經有的節點編號，relaxable 是其中沒有證據的（⑦ 才給；None 是 ③，不能填放寬）。"""
    problems: list[Problem] = []
    for i, n in enumerate(raws):
        msgs: list[str] = []
        kind = n.get("kind")
        if kind not in NODE_KINDS:
            msgs.append(f"kind 要是{'、'.join(NODE_KINDS)}其中一個")
        elif kind == NODE_POINT:
            msgs += _check_scope(n.get("target") or {}, "target", ctx)
        elif kind == NODE_MOVE:
            origin, dest = n.get("origin") or {}, n.get("dest") or {}
            msgs += _check_scope(origin, "origin", ctx) + _check_scope(dest, "dest", ctx)
            if origin.get("mode") == ANYWHERE and dest.get("mode") == ANYWHERE:
                msgs.append("段的起點、終點不能都是不限")
            if n.get("both_ways") not in (YES, NO):
                msgs.append(f"both_ways（反方向也要）要是{YES}或{NO}")
        else:
            msgs += _check_stops(n.get("stops") or [], ctx)
        msgs += _check_time(n, ctx)
        if not (n.get("aspect") or "").strip():
            msgs.append("aspect（面向）一定要填")
        relax = (n.get("relax_from") or "").strip()
        if relax:
            if relaxable is None:
                msgs.append("初始計畫不能填 relax_from")
            elif relax not in known:
                msgs.append(f"relax_from「{relax}」不是這一題已經有的節點")
            elif relax not in relaxable:
                msgs.append(f"relax_from「{relax}」已經有證據，不用放寬；不是放寬就留空")
        problems += [Problem("nodes", i, m) for m in msgs]
    return problems


# ---------- 把 LLM 的輸出變成節點 ----------

def _where(p: dict, ctx: Ctx) -> Where:
    ref = (p.get("ref") or "").strip()
    if ref:
        x = ctx.places[ref]
        return Where(ref=ref, name_zh=x.name_zh, name_ja=x.name_ja, city=x.city, kind=x.kind)
    return Where(name_zh=p["name_zh"].strip(), name_ja=p["name_ja"].strip(), city=(p.get("city") or "").strip(),
                 kind=p["kind"])


def _scope(sc: dict, ctx: Ctx) -> Scope:
    mode = sc["mode"]
    if mode == ANYWHERE:
        return Scope(ANYWHERE)
    return Scope(mode, _where(sc["place"], ctx), float(sc["km"]) if mode == AROUND else None)


def time_cond(n: dict) -> TimeCond:
    year = (n.get("year") or "").strip()
    return TimeCond(year=int(year) if year else None,
                    months=tuple(sorted(set(n.get("months") or []))),
                    day_type=(n.get("day_type") or "").strip(),
                    hours=tuple((h["start"], h["end"]) for h in n.get("hours") or []))


def raw_label(n: dict, ctx: Ctx | None = None) -> str:
    """LLM 輸出的一個節點給人看的寫法（拿掉的節點用）。"""
    def place(p: dict) -> str:
        ref = (p.get("ref") or "").strip()
        if ref and ctx and ref in ctx.places:
            return ctx.places[ref].name_zh
        return (p.get("name_zh") or p.get("name_ja") or ref or "？").strip()

    def scope_text(sc: dict) -> str:
        mode = (sc or {}).get("mode")
        if mode == AROUND:
            return f"{place(sc.get('place') or {})} 周圍 {sc.get('km', 0):g} 公里"
        if mode == ANYWHERE:
            return ANYWHERE
        return place((sc or {}).get("place") or {})

    kind = n.get("kind", "？")
    if kind == NODE_POINT:
        obj = scope_text(n.get("target"))
    elif kind == NODE_MOVE:
        obj = f"{scope_text(n.get('origin'))} → {scope_text(n.get('dest'))}"
    else:
        obj = "、".join((s.get("day") or "").strip() or place(s.get("place") or {})
                       for s in n.get("stops") or []) or "沒有排法"
    return f"{kind}｜{obj}｜面向：{(n.get('aspect') or '').strip()}"


async def _build(raw: dict, ctx: Ctx) -> tuple[Node | None, str]:
    """解析地點，組成節點（還沒編號）。地點條件的地點解析不到、排法一站都解析不到時回傳（None, 原因）。"""
    kind = raw["kind"]
    node = Node(no="", kind=kind, aspect=raw["aspect"].strip(), reason=(raw.get("reason") or "").strip(),
                time=time_cond(raw), relax_from=(raw.get("relax_from") or "").strip(), raw=raw)
    scopes = {NODE_POINT: ("target",), NODE_MOVE: ("origin", "dest")}.get(kind, ())
    for name in scopes:
        sc = _scope(raw[name], ctx)
        if sc.where is not None:
            pid = await ctx.resolver.resolve(sc.where, ctx.question)
            if pid is None:
                return None, f"地點解析不到：{sc.where.label}"
            sc = Scope(sc.mode, _with_id(sc.where, pid), sc.km)
        setattr(node, name, sc)
    if kind == NODE_MOVE:
        node.both_ways = raw["both_ways"] == YES
    if kind == NODE_DAY:
        labels, stops = [], []
        for s in raw.get("stops") or []:
            day = (s.get("day") or "").strip()
            if day:
                labels.append(day)
                user_day = ctx.days[int(day[1:]) - 1]
                for us in user_day.stops:
                    where = _where({"ref": us.ref}, ctx)
                    pid = await ctx.resolver.resolve(where, ctx.question)
                    stops.append(Stop(_with_id(where, pid), day=day, type=us.type))
            else:
                where = _where(s["place"], ctx)
                labels.append(where.label)
                pid = await ctx.resolver.resolve(where, ctx.question)
                stops.append(Stop(_with_id(where, pid), type=SPOT))
        if labels and not any(s.resolved for s in stops):
            return None, f"排法的站都解析不到：{'、'.join(labels)}"
        node.arrangement, node.stops = tuple(labels), tuple(stops)
    return node, ""


def _with_id(w: Where, pid: int | None) -> Where:
    return Where(w.ref, w.name_zh, w.name_ja, w.city, w.kind, pid)


# ---------- 3. 改了什麼、4. 不重複 ----------

def _object_fields(n: Node) -> dict[str, str]:
    if n.kind == NODE_POINT:
        return {"地點": n.target.label if n.target else ""}
    if n.kind == NODE_MOVE:
        return {"起點": n.origin.label if n.origin else "", "終點": n.dest.label if n.dest else "",
                "反方向也要": YES if n.both_ways else NO}
    return {"排法": "、".join(n.arrangement) or "沒有排法"}


def diff(old: Node, new: Node) -> list[str]:
    """兩個節點哪些欄位不同、各從什麼改成什麼，例如「月份 10 → 10、11」「地點 清水寺 → 東山區」。"""
    out = []
    if old.kind != new.kind:
        out.append(f"種類 {old.kind} → {new.kind}")
        out.append(f"對象 {old.object_label()} → {new.object_label()}")
    else:
        a, b = _object_fields(old), _object_fields(new)
        out += [f"{k} {a[k]} → {b[k]}" for k in a if a[k] != b[k]]
    ta, tb = old.time.parts(), new.time.parts()
    out += [f"{k} {ta[k]} → {tb[k]}" for k in ta if ta[k] != tb[k]]
    if old.aspect != new.aspect:
        out.append(f"面向 {old.aspect} → {new.aspect}")
    return out


def node_key(n: Node) -> tuple:
    """同樣條件的節點：種類、對象（段的反方向也要是「是」時不分方向）、反方向也要、排法（天的編號展開後）、
    時間條件、面向（去掉頭尾空白）都一樣。"""
    if n.kind == NODE_POINT:
        obj: object = n.target.key() if n.target else None
    elif n.kind == NODE_MOVE:
        a = n.origin.key() if n.origin else None
        b = n.dest.key() if n.dest else None
        obj = frozenset((a, b)) if n.both_ways else (a, b)
    else:
        obj = tuple(s.key() for s in n.stops)
    t = n.time
    return (n.kind, obj, n.both_ways if n.kind == NODE_MOVE else None,
            (t.year, tuple(sorted(t.months)), t.day_type, tuple(sorted(t.hours))), n.aspect.strip())


# ---------- 2–5 ----------

async def accept(raws: list[tuple[int, dict]], ctx: Ctx, existing: list[Node], *, replan: bool,
                 round_no: int) -> Batch:
    """格式已經通過的節點（（輸出的第幾個, 節點））照順序做 2–5，回傳通過的、拿掉的，和照輸出順序的項目。"""
    batch = Batch()
    quota = min(config.REPLAN_NODES if replan else config.MAX_NODES, config.MAX_NODES - len(existing))
    by_no = {n.no: n for n in existing}
    keys = {node_key(n): n.no for n in existing}
    next_no = len(existing) + 1

    def drop(i: int, raw: dict, why: str, reason: str) -> None:
        d = Dropped(i, why, reason, raw_label(raw, ctx), raw, round_no)
        batch.dropped.append(d)
        batch.order.append(("drop", d, i))
        log.info("拿掉 %s：%s", d.label, reason)

    for i, raw in raws:
        if len(batch.accepted) >= quota:
            if replan and len(batch.accepted) >= config.REPLAN_NODES:
                why = f"超過每輪的上限（補規劃每輪最多 {config.REPLAN_NODES} 個）"
            else:
                why = f"超過節點上限（一題最多 {config.MAX_NODES} 個）"
            drop(i, raw, DROP_OVER, why)
            continue
        node, reason = await _build(raw, ctx)
        if node is None:
            drop(i, raw, DROP_UNRESOLVED, reason)
            continue
        if node.relax_from in by_no:
            node.changes = tuple(diff(by_no[node.relax_from], node))
        key = node_key(node)
        if key in keys:
            drop(i, raw, DROP_DUPLICATE, f"跟 {keys[key]} 的條件一樣")
            continue
        node.no, node.round = f"N{next_no}", round_no
        next_no += 1
        keys[key] = node.no
        by_no[node.no] = node
        batch.accepted.append(node)
        batch.order.append(("node", node.no, i))
        log.info("通過 %s（理由：%s）", node.describe(), node.reason)
    return batch


def format_drops(raws: list[dict], problems: list[Problem], ctx: Ctx, round_no: int) -> tuple[list[tuple[int, dict]], list[Dropped]]:
    """格式還不合法的節點拿掉並附原因；回傳（通過格式的（第幾個, 節點），拿掉的）。"""
    bad: dict[int, list[str]] = {}
    for p in problems:
        bad.setdefault(p.index, []).append(p.message)
    ok = [(i, n) for i, n in enumerate(raws) if i not in bad]
    dropped = [Dropped(i, DROP_FORMAT, "；".join(msgs), raw_label(raws[i], ctx), raws[i], round_no)
               for i, msgs in sorted(bad.items())]
    return ok, dropped
