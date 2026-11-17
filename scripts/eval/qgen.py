"""出題工具（配合 `.scratch/ptt-v2-questions/generate.md`）：候選片段、題目檔驗證。

候選片段和檢索用同一套規則（`Places.scope`、`TimeQuery.matches`、`TimeQuery.widened`、`Places.outer`），
所以「哪些片段要判」由程式決定，不靠人工篩。DB 以唯讀開啟。
評審校準在 `python -m strag_v3.eval.baseline calibrate`。

    python scripts/eval/qgen.py places [--min 3]               # 有片段的地點（id、種類、名稱、片段數）
    python scripts/eval/qgen.py areas                           # 有範圍的區域，和範圍內的片段數（找地點題用）
    python scripts/eval/qgen.py pool 50 --when '{"month": ["3"], "time": ["晚上"]}' [--widen]
    python scripts/eval/qgen.py outer 50                        # 空間退讓會去的外圍行政區
    python scripts/eval/qgen.py layers 50 [--when '<json>']     # 檢索依序試的每層：地點、時間條件、候選數、限定維度不明的筆數
    python scripts/eval/qgen.py frag 6 611                      # 印指定的片段（格式跟 pool 一樣）
    python scripts/eval/qgen.py usage [資料夾]                  # 題目檔用掉的地點（各題型、合計、到上限的）和出過的組合
    python scripts/eval/qgen.py merge <題目檔> <草稿>...        # 草稿依 id 排序合併成題目檔；id 重複就報錯、不寫檔
    python scripts/eval/qgen.py merge <資料夾> <草稿>...        # 按組別的草稿依題型併進 stay／move／find_place.json，反向答案寫進 calibration/
    python scripts/eval/qgen.py blind-input <題目檔> [題目 id...]   # 給盲審子代理的 JSON（不帶標註）
    python scripts/eval/qgen.py blind-diff <題目檔> <盲審檔>...     # 標註 × 盲審的對照表、一致率和不一致的片段
    python scripts/eval/qgen.py check <題目檔>...               # 機械檢查，全部通過才 exit 0
    python scripts/eval/qgen.py plan <題目檔>... [--save <檔>]  # 讓 planner 只讀題目文字，比對還原出的地點和時間；--save 把檢索計畫存檔
    python scripts/eval/qgen.py answer <題目檔> <題目 id>       # 只用證據原句拼成的正向答案
    python scripts/eval/qgen.py scan [--out <資料夾>] [--keep 2]  # 離線找邊界題的題材（.scratch/backoff/spec.md），寫 scan.md、scan.json 和 report.md〈可行性〉
    python scripts/eval/qgen.py jev-diff <題目檔>... --plans <檔> [--report <report.md>]  # 每層候選問 JEV 是否相關，列出跟標註不一致的

「題目檔」也可以是草稿（`ptt_backoff/drafts/{T,S,N}.json`），格式一樣。

規則依檔案所在的資料夾決定（`RULES`）：`ptt_backoff/`（含底下一層，例如 `drafts/`）用邊界題庫的規則，其餘用 ptt_v2 的。
邊界題庫（`.scratch/backoff/spec.md`）多出來的寫法：

- 檔案開頭可以寫 `"aspect_rules": {面向: 判準}`，各題不寫 `aspect_rule` 就用它；題目自己又寫了 `aspect_rule`，算補充，接在共用判準後面。
- 每題多 `boundary`（子類）。`far`、`day_type`、`stacked` 題另外寫 `related`：相關經驗的片段 id（在檢索不會去的地方）。
- `none` 題不用寫 `expect`、`expect_sources`、反向答案：`check` 會當成固定句型的揭露條，`merge` 時寫進題目檔和校準檔。
- T、S 組的反向答案寫在草稿每題的 `negative`，`merge` 時搬到 `calibration/<題型檔名>.json`。
- `plan --save` 的檔：`{"plans": {題目 id: {"question": 題目文字, "plan": 檢索計畫}}}`；題目文字沒變的題沿用檔裡的計畫，不再叫 planner。
- `jev-diff` 問 JEV 用的 aspect 取自存檔的檢索計畫（跟正式跑同一個快取鍵），結果寫進 `jev_cache`。
"""
import argparse
import asyncio
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from strag_v3.eval.baseline.calibrate import positive_answer  # noqa: E402
from strag_v3.eval.baseline.fragments import load_fragments  # noqa: E402
from strag_v3.eval.baseline.questions import Question  # noqa: E402
from strag_v3.place import Places  # noqa: E402
from strag_v3.rag.plan import parse_time  # noqa: E402
from strag_v3.rag.timematch import ALL_DAY, DAY_TYPES, SLOTS, TimeSpec, months_of  # noqa: E402

TYPES = ("停留經驗", "移動經驗", "找地點", "找時間", "比較")
COVERAGES = ("exact", "time_backoff", "space_backoff", "none")
REASONS = ("無關", "別處", "時間矛盾", "時間不明")
FIELDS = ("id", "article", "poi_id", "poi", "month", "day_type", "time", "content")
SELECT = "SELECT id, article_id, poi_id, poi_name, month, day_type, time, content FROM content"
MAX_RELEVANT = 8
MAX_POINTS = 8
# 一個題型（一個檔）內的上限
MAX_NON_EXACT = 2      # 退讓題和 none 題
MAX_SINGLE = 3         # 只收一個片段的題目
MAX_PER_PLACE_TYPE = 2
MAX_PER_PLACE = 6      # 跨題型
MAX_PER_PLACE_BACKOFF = 3  # 邊界題庫：整份題庫同一個地點最多幾題，而且不能都在同一組
NEEDS_WHEN = ("停留經驗", "移動經驗", "找地點")  # 時間是條件的題型
# 原句沒有、卻出現在 expect 裡就算錯的詞：推論、概括、建議、對答案的要求
HEDGES = ("通常", "大多", "大部分", "一般", "往往", "總是", "可能", "不一定", "建議", "應該", "最好",
          "結論", "總之", "整體", "倍", "要說明", "要講明", "要指出", "不能", "無法判斷")
NUM = re.compile(r"\d+(?:[.:]\d+)?")
ZH_NUM = re.compile(r"[一二三四五六七八九十兩]+\s*(?:月|點|分|小時|人|成|度|筆|號|日)")


@dataclass(frozen=True)
class Rules:
    """一個題庫資料夾的數量規則。"""
    name: str
    max_relevant: int = MAX_RELEVANT
    max_points: int = MAX_POINTS
    backoff: bool = False  # 邊界題庫：多檢查 boundary、要判的候選數，分布改成按組別算


PTT_V2_RULES = Rules("ptt_v2")
RULES = {"ptt_backoff": Rules("ptt_backoff", max_relevant=3, max_points=3, backoff=True)}  # 資料夾名稱 → 規則


def rules_for(path: Path) -> Rules:
    """檔案在 `RULES` 列的資料夾裡（或它底下一層，例如 drafts/）就用那份規則，其餘照 ptt_v2。"""
    for folder in list(path.resolve().parents)[:2]:
        if folder.name in RULES:
            return RULES[folder.name]
    return PTT_V2_RULES


def load_file(path: Path) -> list[dict]:
    """題目檔或草稿的題目，`aspect_rule` 已經換成實際用的判準（見 `with_rule`）。"""
    data = json.loads(path.read_text())
    return [with_rule(q, data.get("aspect_rules") or {}) for q in data["questions"]]


def with_rule(q: dict, shared: dict[str, str]) -> dict:
    """這題實際用的判準：檔案開頭 `aspect_rules` 裡這個面向的共用判準；題目自己寫的 `aspect_rule` 接在後面當補充。"""
    base, own = shared.get(q.get("aspect")), q.get("aspect_rule")
    rule = f"{base}；補充：{own}" if base and own else base or own
    return {**q, "aspect_rule": rule} if rule else q


class DB:
    def __init__(self, path: Path | sqlite3.Connection):
        """path 也可以是已經開好的連線（測試用的假資料）。"""
        self.conn = path if isinstance(path, sqlite3.Connection) else sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.places = Places(self.conn)  # 沒給 nominatim：只讀 DB，不打網路
        self._layers: dict = {}
        self._pools: dict = {}
        self._scopes: dict[int, frozenset[int]] = {}
        self._all_places: list | None = None
        self._place: dict = {}
        self._rows: dict[int, list] = {}
        self._outer: dict[int, int | None] = {}

    def place(self, place_id: int):
        """`Places.get` 的快取版（每次 get 都要重新解析範圍的 geojson）。"""
        if place_id not in self._place:
            self._place[place_id] = self.places.get(place_id)
        return self._place[place_id]

    def scope(self, place_id: int) -> frozenset[int]:
        """跟 `Places.scope` 同一個結果，有快取。`scan` 要算幾百個區域，`Places.within` 每次都重讀整張 place 表，
        這裡只讀一次，判斷式照抄（測試會對照兩邊）。"""
        if place_id not in self._scopes:
            if self._all_places is None:
                self._all_places = [self.place(i) for (i,) in self.conn.execute("SELECT id FROM place").fetchall()]
            area = self.place(place_id)
            inside = () if area.kind == "景點" else (
                p.id for p in self._all_places
                if p.id != area.id and (p.kind == "景點" or p.rank > area.rank) and area.contains(p.lat, p.lon))
            self._scopes[place_id] = frozenset({place_id, *inside})
        return self._scopes[place_id]

    def frag(self, cid: int) -> dict:
        r = self.conn.execute(f"{SELECT} WHERE id = ?", (cid,)).fetchone()
        if r is None:
            raise KeyError(f"沒有片段 {cid}")
        return dict(zip(FIELDS, r))

    def pool(self, place_id: int, when: dict | None = None, widen: bool = False) -> list[dict]:
        """跟檢索同一套規則的候選：景點只取自己，區域取範圍內所有地點；時間照 TimeQuery.matches（不明算相符）。
        widen=True 是時間退讓的條件（時段、月份各往外擴一格）。
        每筆附 `unknown_dims`：題目有限定、片段卻標成不明的維度（沒限定的維度不明不算）。"""
        key = (place_id, json.dumps(when, sort_keys=True), widen)
        if key not in self._pools:
            self._pools[key] = self._pool(place_id, when, widen)
        return [dict(f) for f in self._pools[key]]

    def _pool(self, place_id: int, when: dict | None, widen: bool) -> list[dict]:
        if place_id not in self._rows:
            ids = sorted(self.scope(place_id))
            rows = self.conn.execute(f"{SELECT} WHERE poi_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
            self._rows[place_id] = [(f, TimeSpec(f["month"], f["day_type"], f["time"]))
                                    for f in (dict(zip(FIELDS, r)) for r in rows)]
        q = parse_time(when)
        if widen:
            q = q.widened() if q else None
        return [{**f, "unknown_dims": unknown_dims(f, when, widen=widen)}
                for f, spec in self._rows[place_id] if q is None or q.matches(spec)]

    def content_places(self) -> list:
        return [self.places.get(pid) for (pid,) in self.conn.execute("SELECT DISTINCT poi_id FROM content")]

    def outer(self, place_id: int) -> int | None:
        if place_id not in self._outer:
            o = self.places.outer(self.place(place_id))
            self._outer[place_id] = o.id if o else None
        return self._outer[place_id]

    def layers(self, place_id: int, when: dict | None) -> list[tuple[str, set[int]]]:
        """檢索依序嘗試的候選，跟 Retriever._retrieve_one 同順序：原條件 → 時間退讓 → 空間退讓（用原本的時間）。
        時間不限就沒有時間退讓那層，沒有外圍行政區就沒有空間退讓那層。"""
        key = (place_id, json.dumps(when, sort_keys=True))
        if key not in self._layers:
            out = [("exact", {f["id"] for f in self.pool(place_id, when)})]
            if (q := parse_time(when)) and q.widened():
                out.append(("time_backoff", {f["id"] for f in self.pool(place_id, when, widen=True)}))
            if (o := self.outer(place_id)) is not None:
                out.append(("space_backoff", {f["id"] for f in self.pool(o, when)}))
            self._layers[key] = out
        return self._layers[key]

    def judged_layers(self, space: dict, when: dict | None, coverage: str) -> list[tuple[str, set[int]]]:
        """必須逐筆判的層：從原條件試到 coverage 那層為止（none 是每一層）。"""
        lay = self.layers(space["place_id"], when)
        names = [n for n, _ in lay]
        stop = names.index(coverage) + 1 if coverage in names else len(lay)
        return lay[:stop]

    def must_judge(self, space: dict, when: dict | None, coverage: str) -> set[int]:
        """必須逐筆判過的候選：從原條件試到 coverage 那層為止的所有候選（none 是每一層）。"""
        return set().union(*(ids for _, ids in self.judged_layers(space, when, coverage)))


DIMS = {"month": "月份", "day_type": "平日／假日", "time": "時段"}


def unknown_dims(f: dict, when: dict | None, asks: str | None = None, widen: bool = False) -> list[str]:
    """題目限定的維度（when 有填的，加上找時間題問的 asks）裡，片段說不準是否符合的：
    標成不明，或月份標籤比條件粗、不完全落在條件裡（例：問 3 月，片段標「春」，可能是 4、5 月）。
    widen=True 時用時間退讓後的條件判斷。"""
    q = parse_time(when)
    if widen and q:
        q = q.widened()
    out = []
    if q and q.months is not None and (f["month"] == "不明" or not set(months_of(f["month"])) <= q.months):
        out.append("月份" if f["month"] == "不明" else f"月份（標「{f['month']}」比條件粗）")
    elif asks == "month" and f["month"] == "不明":
        out.append("月份")
    if (q and q.day_type or asks == "day_type") and f["day_type"] == "不明":
        out.append("平日／假日")
    if (q and q.slots is not None or asks == "time") and f["time"] == "不明":
        out.append("時段")
    return out


def frag_line(f: dict) -> str:
    """`pool`、`frag` 印一筆片段的格式；有 `unknown_dims` 才標「限定維度不明」。"""
    flag = f" | 限定維度不明：{'、'.join(f['unknown_dims'])}" if f.get("unknown_dims") else ""
    return f"{f['id']} | {f['poi']} | {f['month']}/{f['day_type']}/{f['time']}{flag} | {f['content']}"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def when_text(f: dict) -> str:
    """片段的時間標籤寫成一句話（不明的維度不寫）。"""
    m = f["month"]
    month = "" if m == "不明" else f"{m} 月" if m[0].isdigit() else f"{m}天"
    parts = [month, *(x for x in (f["day_type"], f["time"]) if x != "不明")]
    return " ".join(p for p in parts if p) or "時間沒寫"


# ---------- 檢索的層 ----------

def layer_place(db: DB, place_id: int, layer: str) -> int:
    """這層撈哪個地點：空間退讓撈外圍行政區，其餘撈原地點。"""
    return db.outer(place_id) if layer == "space_backoff" else place_id


def layer_when(when: dict | None, layer: str) -> dict | None:
    """這層用的時間條件，寫成 when 的格式：時間退讓層是擴大後的條件，其餘是原條件。"""
    if layer != "time_backoff":
        return when
    w = parse_time(when).widened()
    return {k: v for k, v in (("month", list(w.month)), ("day_type", w.day_type), ("time", list(w.time))) if v}


def place_text(db: DB, place_id: int) -> str:
    p = db.places.get(place_id)
    return f"{place_id} {p.name}（{p.kind}）"


def cond_text(when: dict | None) -> str:
    return json.dumps(when, ensure_ascii=False) if when else "時間不限"


# ---------- 驗證 ----------

def check(db: DB, q: dict, rules: Rules = PTT_V2_RULES) -> list[str]:
    """一題的機械檢查；回傳錯誤清單，空的才算通過。"""
    k = q.get("id", "?")
    e: list[str] = []
    if rules.backoff and q.get("coverage") == "none":  # 揭露條由腳本產生：沒寫就補上，寫了要一樣
        try:
            made = none_fields(q)
        except Exception as ex:  # noqa: BLE001
            return [f"{k}：產生不了 none 題的揭露條，space、when、aspect 要寫齊（{ex!r}）"]
        if "expect" in q and (q["expect"], q.get("expect_sources")) != (made["expect"], made["expect_sources"]):
            e.append(f"{k}：none 題的 expect 由腳本產生，不用寫；要寫就得是「{made['expect'][0]}」，expect_sources 是 [{{}}]")
        q = {**q, **made}
    try:
        tq = parse_time(q.get("when"))
    except Exception as ex:  # noqa: BLE001
        return [f"{k}：when 看不懂（{ex}）"]
    if q.get("type") not in TYPES:
        e.append(f"{k}：type 只能是 {'、'.join(TYPES)}")
    if q.get("type") in NEEDS_WHEN and tq is None:
        e.append(f"{k}：{q.get('type')}題的時間是條件，when 不能是空的")
    cov = q.get("coverage")
    if cov not in COVERAGES:
        return e + [f"{k}：coverage 只能是 {'、'.join(COVERAGES)}"]
    if cov != "exact" and q.get("type") in ("找時間", "比較"):
        e.append(f"{k}：{q.get('type')}題只出 exact")
    rel = set(q.get("relevant_contents") or [])
    excluded = {int(i): r for i, r in (q.get("excluded") or {}).items()}
    if bad := {i: r for i, r in excluded.items() if r not in REASONS}:
        e.append(f"{k}：excluded 的理由只能是 {'、'.join(REASONS)}：{bad}")

    # 候選：每筆都要判過，收的要在候選裡
    units = q.get("options") if q.get("type") == "比較" else [q]
    if not units or any("space" not in u for u in units):
        return e + [f"{k}：缺 space（比較題寫在每個 options 裡）"]
    must = set().union(*(db.must_judge(u["space"], u.get("when"), cov) for u in units))
    if miss := must - rel - set(excluded):
        e.append(f"{k}：這些候選沒判：{sorted(miss)}")
    if extra := (rel | set(excluded)) - must:
        e.append(f"{k}：判了候選以外的片段：{sorted(extra)}")
    if both := rel & set(excluded):
        e.append(f"{k}：同時收又不收：{sorted(both)}")
    if cov == "none" and rel:
        e.append(f"{k}：coverage=none 不能有 relevant_contents")
    if cov != "none" and not 1 <= len(rel) <= rules.max_relevant:
        e.append(f"{k}：relevant_contents 有 {len(rel)} 筆，要 1–{rules.max_relevant}")
    if q.get("type") != "比較":
        e += check_layers(db, q, cov, rel, excluded)
    if q.get("type") == "找地點" and cov != "none" and len({db.frag(c)["poi_id"] for c in rel}) < 2:
        e.append(f"{k}：找地點題收的片段要涵蓋至少 2 個地點")
    if q.get("type") == "比較":
        for o in units:
            orel = set(o.get("relevant_contents") or [])
            if not orel or not orel <= rel:
                e.append(f"{k}：選項「{o.get('label')}」的 relevant_contents 要非空，且包含在題目的 relevant_contents 裡")
            elif out := orel - {f["id"] for f in db.pool(o["space"]["place_id"], o.get("when"))}:
                e.append(f"{k}：選項「{o.get('label')}」收了不在它原條件候選裡的片段：{sorted(out)}")
        if set().union(*(set(o.get("relevant_contents") or []) for o in units)) != rel:
            e.append(f"{k}：各選項 relevant_contents 的聯集要等於題目的 relevant_contents")
    if sorted({db.frag(c)["article"] for c in rel}) != sorted(q.get("relevant_articles") or []):
        e.append(f"{k}：relevant_articles 要由 relevant_contents 推得")

    # 時間不明：題目限定（或找時間題問的）維度標成不明的片段不收；內文明寫了符合的時間才例外，要記進 label_issues
    asks = q.get("asks")
    if q.get("type") == "找時間" and asks not in DIMS:
        e.append(f"{k}：找時間題要寫 asks（問的是哪個維度：{'、'.join(DIMS)}）")
    issues = {int(x["id"]) for x in q.get("label_issues") or []}
    widen = cov == "time_backoff"
    for u in units:
        mine = set(u.get("relevant_contents") or []) if q.get("type") == "比較" else rel
        for c in sorted(mine):
            if (dims := unknown_dims(db.frag(c), u.get("when"), asks, widen)) and c not in issues:
                e.append(f"{k}：片段 {c} 的{'、'.join(dims)}說不準卻收了；原句明寫了符合條件的時間才能收，並記進 label_issues")
    for c, why in excluded.items():
        f = db.frag(c)
        if why == "時間不明" and not any(unknown_dims(f, u.get("when"), asks, w) for u in units for w in (False, True)):
            e.append(f"{k}：片段 {c} 限定的時間維度都確定，不能用「時間不明」排除")
    if rules.backoff:
        e += check_backoff(db, q, cov, rel, must)

    # expect
    exp, src = q.get("expect") or [], q.get("expect_sources") or []
    if len(exp) != len(src):
        return e + [f"{k}：expect 和 expect_sources 條數不同"]
    # 揭露條：空間退讓要先講明問的地點本身沒有；none 只講沒有。時間退讓不寫揭露條，靠事實條開頭的實際時間
    disclose = {"exact": 0, "time_backoff": 0, "space_backoff": 1, "none": 1}[cov]
    facts = len(exp) - disclose
    if cov == "none" and facts:
        e.append(f"{k}：coverage=none 只有一條揭露條")
    if cov != "none" and not 1 <= facts <= rules.max_points:
        e.append(f"{k}：事實條有 {facts} 條，要 1–{rules.max_points}")
    cited: set[int] = set()
    for i, (p, s) in enumerate(zip(exp, src)):
        tag = f"{k}：expect[{i}]"
        if i < disclose:
            if s or not p.startswith("說明"):
                e.append(f"{tag} 應是揭露條：以「說明」開頭，expect_sources 是 {{}}")
            continue
        if not s:
            hint = "（時間退讓不寫揭露條，實際時間寫在事實條開頭）" if cov == "time_backoff" and p.startswith("說明") else ""
            e.append(f"{tag} 沒有來源{hint}")
            continue
        ids = {int(c) for c in s}
        if not ids <= rel:
            e.append(f"{tag} 引用了 relevant_contents 以外的片段：{sorted(ids - rel)}")
            continue
        cited |= ids
        frs = {c: db.frag(c) for c in ids}
        quotes = ""
        for c, quote in s.items():
            if not quote or _norm(quote) not in _norm(frs[int(c)]["content"]):
                e.append(f"{tag} 的 quote 不是片段 {c} 的原文子字串")
            quotes += _norm(quote) + "\n"
        allowed = set(NUM.findall(quotes + " ".join(f"{f['month']} " for f in frs.values())))
        if bad := sorted(set(NUM.findall(_norm(p))) - allowed):
            e.append(f"{tag} 有原句和月份標籤裡都沒有的數字：{bad}")
        if bad := [w for w in HEDGES if w in p and w not in quotes]:
            e.append(f"{tag} 有原句沒有的推論／概括／要求用語：{bad}")
        if bad := [w for w in ZH_NUM.findall(p) if w not in quotes]:
            e.append(f"{tag} 數字要用阿拉伯數字：{bad}")
    if cited != rel:
        e.append(f"{k}：relevant_contents 裡沒被任何 expect 用到的：{sorted(rel - cited)}")
    return e


def check_layers(db: DB, q: dict, cov: str, rel: set[int], excluded: dict[int, str]) -> list[str]:
    """coverage 要停在檢索第一個有相關片段的那層：前面的層不能有收進來的片段，也不能有「內容相關但時間不明」的片段
    （不明算相符，系統會在那層就拿它們回答）；收的片段要在停的那層的候選裡。"""
    k = q.get("id", "?")
    lay = db.layers(q["space"]["place_id"], q.get("when"))
    names = [n for n, _ in lay]
    if cov != "none" and cov not in names:
        return [f"{k}：沒有 {cov} 這層（時間不限就沒有時間退讓，沒有外圍行政區就沒有空間退讓）"]
    stop = names.index(cov) if cov != "none" else len(lay)
    e = []
    for name, ids in lay[:stop]:
        if hit := ids & rel:
            e.append(f"{k}：{name} 這層就有收進來的片段 {sorted(hit)}，coverage 應該停在 {name}")
        if unk := sorted(i for i in ids if excluded.get(i) == "時間不明"):
            e.append(f"{k}：{name} 這層有內容相關但時間不明的片段 {unk}，系統會在這層就拿它們回答，"
                     f"跟 coverage={cov} 對不起來，換題")
    if cov != "none" and (out := rel - lay[stop][1]):
        e.append(f"{k}：收進來的片段不在 {cov} 這層的候選裡：{sorted(out)}")
    return e


def places_of(q: dict) -> set[int]:
    units = q.get("options") if q.get("type") == "比較" else [q]
    return {u["space"]["place_id"] for u in units or [] if "space" in u}


def check_set(qs: list[dict]) -> list[str]:
    """一個題型（一個檔）內的分布上限。"""
    e = []
    ids = [q.get("id") for q in qs]
    e += [f"id 重複：{i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if (n := sum(q.get("coverage") != "exact" for q in qs)) > MAX_NON_EXACT:
        e.append(f"不是 exact 的題目有 {n} 題，上限 {MAX_NON_EXACT}")
    if (n := sum(len(q.get("relevant_contents") or []) == 1 for q in qs)) > MAX_SINGLE:
        e.append(f"只收一個片段的題目有 {n} 題，上限 {MAX_SINGLE}")
    per = Counter(p for q in qs for p in places_of(q))
    if over := {p: n for p, n in per.items() if n > MAX_PER_PLACE_TYPE}:
        e.append(f"同一個地點在這個題型超過 {MAX_PER_PLACE_TYPE} 題（地點 id: 題數）：{over}")
    return e


def check_all(qs: list[dict]) -> list[str]:
    """所有題型合起來的上限。"""
    e = []
    per = Counter(p for q in qs for p in places_of(q))
    if over := {p: n for p, n in per.items() if n > MAX_PER_PLACE}:
        e.append(f"同一個地點跨題型超過 {MAX_PER_PLACE} 題（地點 id: 題數）：{over}")
    return e + check_combos(qs)


def check_combos(qs: list[dict]) -> list[str]:
    keys = Counter((q["space"]["place_id"], q.get("aspect"), json.dumps(q.get("when"), sort_keys=True, ensure_ascii=False))
                   for q in qs if q.get("type") != "比較" and "space" in q)
    if dup := [x for x, n in keys.items() if n > 1]:
        return [f"地點、面向、時間條件都一樣的題目重複：{dup}"]
    return []


def check_files(db: DB, paths: list[Path]) -> int:
    n_err = 0
    every: dict[Rules, list[dict]] = defaultdict(list)
    for path in paths:
        rules = rules_for(path)
        qs = load_file(path)
        every[rules] += qs
        errs = [m for q in qs for m in check(db, q, rules)] + ([] if rules.backoff else check_set(qs))
        print("\n".join(errs) if errs else f"{path}：{len(qs)} 題全部通過")
        n_err += len(errs)
    for rules, qs in every.items():
        if errs := check_backoff_set(qs) if rules.backoff else check_all(qs):
            print("\n".join(errs))
            n_err += len(errs)
    return n_err


# ---------- 邊界題庫的檢查（.scratch/backoff/spec.md〈出題規則〉） ----------

def group_of(q: dict) -> str | None:
    """T、S、N：由 coverage 決定。"""
    return next((g for g, (_, cov) in GROUPS.items() if cov == q.get("coverage")), None)


def check_backoff(db: DB, q: dict, cov: str, rel: set[int], must: set[int]) -> list[str]:
    """邊界題庫一題多出來的檢查：題型、判準、要判的候選數，和 `boundary` 跟各層候選的實際情況一致。"""
    k = q.get("id", "?")
    e = []
    if q.get("type") not in BACKOFF_TYPES:
        e.append(f"{k}：邊界題庫只出{'、'.join(BACKOFF_TYPES)}")
    if cov == "exact":
        return e + [f"{k}：邊界題庫不出 exact 題（對照組用 ptt_v2 的）"]
    if not q.get("aspect_rule"):
        e.append(f"{k}：沒有 aspect_rule，檔案開頭的 aspect_rules 也沒有「{q.get('aspect')}」")
    if len(must) > MAX_JUDGE:
        e.append(f"{k}：要判的候選有 {len(must)} 筆，上限 {MAX_JUDGE}")
    b = q.get("boundary")
    mine = [x for x, (g, _, _) in BOUNDARIES.items() if GROUPS[g][1] == cov]
    if b not in mine:
        return e + [f"{k}：coverage={cov} 的 boundary 只能是 {'、'.join(mine)}，不是 {b!r}"]
    when = q.get("when") or {}
    month, slot = when.get("month"), when.get("time")
    if not (isinstance(month, list) and len(month) == 1 and str(month[0]).isdigit()
            and isinstance(slot, list) and len(slot) == 1 and slot[0] in SLOTS):
        return e + [f"{k}：邊界題的 when 要剛好一個月份（數字）、一個時段（{'、'.join(SLOTS)}），寫成 list"]
    pid = q["space"]["place_id"]
    related = {int(c) for c in q.get("related") or []}

    if b in ("month", "slot", "ring"):
        for c in sorted(rel):
            f = db.frag(c)
            gap = None if "不明" in (f["month"], f["time"]) else time_gap(when, f)
            if gap != b:
                e.append(f"{k}：片段 {c}（{when_text(f)}）跟原條件差的是 {gap or '兩個維度，或標籤不明'}，不是 boundary={b}")
    elif b in ("poi_to_area", "area_to_area"):
        want = "景點" if b == "poi_to_area" else "區域"
        if (kind := db.place(pid).kind) != want:
            e.append(f"{k}：boundary={b} 問的要是{want}，{place_text(db, pid)} 是{kind}")
    elif b in ("far", "day_type", "stacked"):
        if b == "day_type" and when.get("day_type") not in DAY_TYPES:
            return e + [f"{k}：boundary=day_type 的 when 要限定平日或假日"]
        outer = db.outer(pid)
        if b == "far":
            where, desc = db.pool(pid), "這個地點（不限時間）的片段"
        elif b == "day_type":
            other = next(d for d in DAY_TYPES if d != when["day_type"])
            where, desc = db.pool(pid, {**when, "day_type": other}), f"這個地點同月份同時段、{other}的片段"
        else:
            where, desc = (db.pool(outer, when, widen=True) if outer is not None else []), "外圍行政區相鄰時間的片段"
        where_ids = {f["id"]: f for f in where}
        if not related:
            e.append(f"{k}：boundary={b} 要寫 related（相關經驗的片段 id）")
        if hit := sorted(related & must):
            e.append(f"{k}：related 的 {hit} 是檢索會去的候選，不算「檢索到不了的相關經驗」")
        if out := sorted(related - where_ids.keys()):
            e.append(f"{k}：boundary={b} 的 related 要是{desc}，這些不是：{out}")
        if b == "day_type":
            if bad := sorted(c for c in related & where_ids.keys() if where_ids[c]["day_type"] != other):
                e.append(f"{k}：boundary=day_type 的 related 要標成{other}，這些不是：{bad}")
        if b == "far":
            for c in sorted(related & where_ids.keys() - must):
                d_month, d_slot = time_steps(when, where_ids[c])
                if max(d_month, d_slot) < 2:
                    e.append(f"{k}：片段 {c}（{when_text(where_ids[c])}）離原條件月份 {d_month} 格、時段 {d_slot} 格，"
                             f"不在兩格以外，不是 boundary=far")
    else:  # aspect、empty
        if related:
            e.append(f"{k}：boundary={b} 沒有相關經驗，不寫 related")
        lay = db.layers(pid, q.get("when"))
        if b == "empty" and must:
            e.append(f"{k}：boundary=empty 要每一層的候選都是 0，現在有 {len(must)} 筆")
        if b == "aspect" and (not all(ids for _, ids in lay) or len(must) < MIN_ASPECT_POOL):
            sizes = "、".join(f"{n} {len(ids)}" for n, ids in lay)
            e.append(f"{k}：boundary=aspect 要每一層都有候選、合計至少 {MIN_ASPECT_POOL} 筆（{sizes}，合計 {len(must)}）")
    return e


def check_backoff_set(qs: list[dict], avoid: Path | None = None) -> list[str]:
    """邊界題庫的分布，給的檔案全部合起來算：每組三種題型都要有、單一題型不超過該組一半、
    同一地點最多 3 題且不能三題同組、id 和（地點、面向、時間條件）不重複，也不能跟 avoid（預設 ptt_v2）出過的組合一樣。"""
    e = []
    ids = [q.get("id") for q in qs]
    e += [f"id 重複：{i}" for i in sorted({str(i) for i in ids if ids.count(i) > 1})]
    for g, (title, _) in GROUPS.items():
        mine = [q for q in qs if group_of(q) == g]
        if not mine:
            continue
        types = Counter(q.get("type") for q in mine)
        if miss := [t for t in BACKOFF_TYPES if not types[t]]:
            e.append(f"{g} 組（{title}）沒有{'、'.join(miss)}題，每組三種題型都要有")
        if over := {t: n for t, n in types.items() if n * 2 > len(mine)}:
            e.append(f"{g} 組（{title}）{len(mine)} 題，單一題型超過一半（題型: 題數）：{over}")
    per = Counter(p for q in qs for p in places_of(q))
    if over := {p: n for p, n in per.items() if n > MAX_PER_PLACE_BACKOFF}:
        e.append(f"同一個地點超過 {MAX_PER_PLACE_BACKOFF} 題（地點 id: 題數）：{over}")
    same = Counter((p, group_of(q)) for q in qs for p in places_of(q))
    if bad := sorted(f"{p}（{g} 組）" for (p, g), n in same.items() if n >= MAX_PER_PLACE_BACKOFF):
        e.append(f"同一個地點的 {MAX_PER_PLACE_BACKOFF} 題不能都在同一組：{'、'.join(bad)}")
    e += check_combos(qs)
    used = used_combos(ROOT / "src/strag_v3/eval/questions/ptt_v2" if avoid is None else avoid)
    for q in qs:
        try:
            key = (q["space"]["place_id"], q.get("aspect"), _time_key(parse_time(q.get("when"))))
        except Exception:  # noqa: BLE001  when 看不懂，check 會報
            continue
        if key in used:
            e.append(f"{q.get('id')}：（地點、面向、時間條件）跟 ptt_v2 出過的一樣")
    return e


# ---------- none 題的揭露條和反向答案（固定句型） ----------

# 面向 → 反向答案裡編出來的經驗
MADE_UP = {
    "人潮多寡": "人很多，到處都很擠",
    "排隊／等待時間": "排隊排了 60 分鐘才進去",
    "開放狀況": "有正常開放，可以進去",
    "景觀能見度": "視野很清楚，遠處都看得到",
    "花況": "花正在盛開",
    "交通狀況": "車班很多，等不到 5 分鐘就有車",
}


def when_phrase(when: dict) -> str:
    """時間條件寫進句子裡：「3 月平日早上」。"""
    months = [str(m) for m in when.get("month") or []]
    month = "、".join(months) + (" 月" if months and all(m.isdigit() for m in months) else "")
    return month + (when.get("day_type") or "") + "、".join(when.get("time") or [])


def none_fields(q: dict) -> dict:
    """none 題由 space、when、aspect 產生的欄位：一條揭露條和它的空來源。"""
    where = f"{q['space']['name']} {when_phrase(q['when'])}"
    return {"expect": [f"說明資料裡沒有{where}{q['aspect']}的經驗"], "expect_sources": [{}]}


def none_negative(q: dict) -> str:
    """none 題的反向答案：把編出來的經驗說成問的地點和時間。面向不在 `MADE_UP` 裡就用題目自己寫的 `negative`。"""
    claim = MADE_UP.get(q["aspect"])
    if claim is None:
        if q.get("negative"):
            return q["negative"]
        raise KeyError(f"面向「{q['aspect']}」沒有固定句型，在題目寫 negative")
    return f"{q['space']['name']} {when_phrase(q['when'])}{claim}。"


# ---------- 題目檔 ----------

ANNOTATION_NOTE = "依 src/strag_v3/eval/questions/generate.md 出題與標註"


def _id_key(qid: str) -> list:
    """stay-2 排在 stay-10 前面。"""
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", qid)]


def _units(q: dict) -> list[dict]:
    """有自己 space／when 的單位：比較題是每個選項，其餘是題目本身。"""
    return (q.get("options") or []) if q.get("type") == "比較" else [q]


def usage(db: DB, folder: Path) -> None:
    """資料夾裡所有題目檔用掉的地點：各題型和合計的題數（到上限的標出來），和出過的（地點、面向、時間條件）組合。"""
    qs = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("questions"), list):
            qs += data["questions"]
    if not qs:
        print("還沒有題目")
        return

    def mark(n: int, cap: int) -> str:
        return f"{n}（到上限）" if n >= cap else str(n)

    per: dict[int, Counter] = defaultdict(Counter)
    for q in qs:
        for p in places_of(q):
            per[p][q.get("type")] += 1
    print(f"各地點的題數（同一題型上限 {MAX_PER_PLACE_TYPE} 題，跨題型合計上限 {MAX_PER_PLACE} 題）：")
    for p, c in sorted(per.items(), key=lambda x: (-x[1].total(), x[0])):
        types = [*TYPES, *sorted(str(t) for t in c if t not in TYPES)]
        counts = "、".join(f"{t} {mark(c[t], MAX_PER_PLACE_TYPE)}" for t in types if c[t])
        print(f"{place_text(db, p)}：{counts}；合計 {mark(c.total(), MAX_PER_PLACE)}")

    combos: dict[tuple, list[str]] = defaultdict(list)
    for q in qs:
        for u in _units(q):
            if "space" in u:
                combos[(u["space"]["place_id"], q.get("aspect") or "", cond_text(u.get("when")))].append(str(q.get("id")))
    print("已出過的（地點、面向、時間條件）組合：")
    for (p, aspect, when), ids in sorted(combos.items()):
        print(f"{place_text(db, p)}｜{aspect}｜{when}｜{'、'.join(sorted(ids, key=_id_key))}")


def merge(out: Path, drafts: list[Path]) -> int:
    """把草稿的題目依 id 排序寫成題目檔；id 重複就報錯，不寫檔。"""
    qs, where = [], {}
    for path in drafts:
        for q in json.loads(path.read_text())["questions"]:
            qs.append(q)
            where.setdefault(q.get("id"), []).append(str(path))
    if dup := {i: ps for i, ps in where.items() if len(ps) > 1}:
        print("\n".join(f"id 重複：{i}（{'、'.join(ps)}）" for i, ps in dup.items()) + f"\n沒有寫入 {out}")
        return 1
    qs.sort(key=lambda q: _id_key(str(q.get("id"))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"annotation_note": ANNOTATION_NOTE, "questions": qs}, ensure_ascii=False, indent=2) + "\n")
    print(f"{out}：{len(qs)} 題（{'、'.join(str(q.get('id')) for q in qs)}）")
    return 0


TYPE_FILES = {"停留經驗": "stay", "移動經驗": "move", "找地點": "find_place"}
BACKOFF_NOTE = "依 .scratch/backoff/spec.md 出題與標註；none 題的揭露條和反向答案由 qgen.py merge 產生"


def merge_groups(folder: Path, drafts: list[Path]) -> int:
    """按組別的草稿（T、S、N）依題型併進 folder 的 stay／move／find_place.json，三個檔都重寫。
    none 題補上揭露條；每題的反向答案（T、S 組是草稿的 `negative`，none 題用固定句型）搬到 calibration/<題型檔名>.json。
    共用判準各草稿寫的一樣就留在檔案開頭；不一樣的面向，把實際用的判準寫回各題。有錯就不寫檔。"""
    items: list[tuple[dict, dict]] = []  # (題目, 它那份草稿的共用判準)
    where: dict = defaultdict(list)
    said: dict[str, set[str]] = defaultdict(set)
    for path in drafts:
        data = json.loads(path.read_text())
        shared = data.get("aspect_rules") or {}
        for aspect, rule in shared.items():
            said[aspect].add(rule)
        for q in data["questions"]:
            items.append((q, shared))
            where[q.get("id")].append(str(path))
    errs = [f"id 重複：{i}（{'、'.join(ps)}）" for i, ps in where.items() if len(ps) > 1]
    agreed = {a: next(iter(rules)) for a, rules in said.items() if len(rules) == 1}
    files: dict[str, list[dict]] = {name: [] for name in TYPE_FILES.values()}
    negatives: dict[str, dict[str, str]] = {name: {} for name in TYPE_FILES.values()}
    for q, shared in items:
        k = q.get("id")
        if q.get("type") not in TYPE_FILES:
            errs.append(f"{k}：題型「{q.get('type')}」沒有對應的題目檔（只有{'、'.join(TYPE_FILES)}）")
            continue
        name = TYPE_FILES[q["type"]]
        q = dict(q) if q.get("aspect") in agreed else with_rule(q, shared)
        try:
            if q.get("coverage") == "none":
                q |= none_fields(q)
                negative = none_negative(q)
            else:
                negative = q.get("negative")
        except Exception as ex:  # noqa: BLE001
            errs.append(f"{k}：產生不了 none 題的揭露條或反向答案（{ex}）")
            continue
        if not negative:
            errs.append(f"{k}：沒有反向答案（在草稿這題寫 negative）")
            continue
        q.pop("negative", None)
        files[name].append(q)
        negatives[name][k] = negative
    if errs:
        print("\n".join(errs) + f"\n沒有寫入 {folder}")
        return 1
    (folder / "calibration").mkdir(parents=True, exist_ok=True)
    for name, qs in files.items():
        qs.sort(key=lambda q: _id_key(str(q.get("id"))))
        rules = {a: agreed[a] for a in dict.fromkeys(q.get("aspect") for q in qs) if a in agreed}
        data = {"annotation_note": BACKOFF_NOTE, **({"aspect_rules": rules} if rules else {}), "questions": qs}
        (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        ordered = {q["id"]: negatives[name][q["id"]] for q in qs}
        (folder / "calibration" / f"{name}.json").write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")
        print(f"{folder / f'{name}.json'}：{len(qs)} 題（{'、'.join(str(q.get('id')) for q in qs)}）")
    return 0


# ---------- 盲審 ----------

BLIND_FIELDS = ("id", "type", "question", "when", "asks", "aspect", "aspect_rule")  # 盲審只看得到這些欄位
BLIND_NOTE = [
    "每題照 generate.md〈收不收片段〉，逐筆判 layers[].judge 列的片段：收，或不收並附理由碼（無關、別處、時間矛盾、時間不明）。",
    "每層的 space、when 是這層撈片段的地點範圍和時間條件，判這層的片段就照它們判（time_backoff 是擴大後的時間，"
    "space_backoff 是外圍行政區）。片段全文從專案根目錄跑這層的 pool 指令看；pool 印的是整層的候選，"
    "只判 judge 列的：同一筆只列在第一次出現的那層。",
    "比較題每個選項各有自己的層（option 是選項標籤）；in_options 標出同時是好幾個選項候選的片段，只判一次。",
    "結果寫成 JSON：{題目 id: {\"relevant\": {片段 id: 算數那句原文}, \"excluded\": {片段 id: 理由碼}}}。"
    "原文從片段逐字複製，可以截短，不能改字；每筆 judge 列的片段都要出現在 relevant 或 excluded 其中一邊。",
]


def scope_text(db: DB, place_id: int) -> str:
    """撈片段的地點範圍：景點只撈自己，區域撈本身和範圍內所有地點。"""
    p = db.places.get(place_id)
    return f"{place_id} {p.name}（{'區域：區域本身和範圍內所有地點' if p.kind == '區域' else p.kind}）"


def pool_cmd(place_id: int, when: dict | None, widen: bool) -> str:
    cmd = f".venv/bin/python scripts/eval/qgen.py pool {place_id}"
    if when:
        cmd += f" --when '{json.dumps(when, ensure_ascii=False)}'"
    return cmd + (" --widen" if widen else "")


def blind_input(db: DB, q: dict) -> dict:
    """給盲審子代理的一題：只有題目要素（BLIND_FIELDS），和跟 must_judge 一樣的要判的層。
    標註（coverage、收不收、expect…）一律不帶。"""
    compare = q.get("type") == "比較"
    layers: list[dict] = []
    first: dict[int, dict] = {}            # 片段 → 它第一次出現的那層
    owners: dict[int, list[str]] = {}      # 比較題：片段 → 它是哪些選項的候選
    for u in _units(q):
        for name, ids in db.judged_layers(u["space"], u.get("when"), q.get("coverage")):
            pid = layer_place(db, u["space"]["place_id"], name)
            entry = {"layer": name, **({"option": u.get("label")} if compare else {}),
                     "space": scope_text(db, pid), "when": layer_when(u.get("when"), name),
                     "pool": pool_cmd(pid, u.get("when"), name == "time_backoff"),
                     "judge": sorted(ids - first.keys())}
            first |= {i: entry for i in entry["judge"]}
            if compare:
                for i in ids:
                    if u.get("label") not in owners.setdefault(i, []):
                        owners[i].append(u.get("label"))
            layers.append(entry)
    for i in sorted(owners):
        if len(owners[i]) > 1:
            first[i].setdefault("in_options", {})[str(i)] = owners[i]
    return {**{k: q[k] for k in BLIND_FIELDS if k in q}, "layers": layers}


CATEGORIES = ("收", "不計分", "其他不收")  # 不計分＝時間不明；其他不收＝無關、別處、時間矛盾


def _judgement(kind: str, value: str) -> tuple[str, str]:
    """(三類之一, 顯示用的判斷)；kind 是 relevant 或 excluded，value 是引文或理由碼。"""
    if kind == "relevant":
        return "收", "收"
    return ("不計分" if value == "時間不明" else "其他不收"), f"不收（{value}）"


def _content(db: DB, cid: int) -> str:
    try:
        return db.frag(cid)["content"]
    except KeyError:
        return ""


def blind_diff(db: DB, path: Path, blind_paths: list[Path]) -> int:
    """標註 × 盲審的三類對照表和一致率，逐筆列出不一致的；再列盲審漏判、判到候選以外、引文不是原文的。
    只對照盲審檔裡有的題目。全部一致、沒有其他問題才 exit 0。"""
    qs = {q.get("id"): q for q in load_file(path)}
    blind: dict[str, dict[int, tuple[str, str]]] = {}
    odd: list[str] = []
    for bp in blind_paths:
        for qid, j in json.loads(bp.read_text()).items():
            rel = {int(c): t for c, t in (j.get("relevant") or {}).items()}
            exc = {int(c): r for c, r in (j.get("excluded") or {}).items()}
            odd += [f"{qid} | {c} | 同時寫在 relevant 和 excluded" for c in sorted(rel.keys() & exc.keys())]
            odd += [f"{qid} | {c} | 理由碼「{r}」不是 {'、'.join(REASONS)}" for c, r in exc.items() if r not in REASONS]
            blind.setdefault(qid, {}).update({c: ("excluded", r) for c, r in exc.items()}
                                             | {c: ("relevant", t) for c, t in rel.items()})

    table: Counter = Counter()
    diffs, missed, outside, bad_quotes = [], [], [], []
    odd += [f"{qid} | 題目檔裡沒有這題" for qid in blind if qid not in qs]
    for qid in sorted((i for i in blind if i in qs), key=_id_key):
        q, mine = qs[qid], blind[qid]
        cands = set().union(*(db.must_judge(u["space"], u.get("when"), q.get("coverage")) for u in _units(q)))
        rel = set(q.get("relevant_contents") or [])
        exc = {int(i): r for i, r in (q.get("excluded") or {}).items()}
        for c in sorted(cands & mine.keys() & (rel | exc.keys())):  # 標註漏判的不對照，check 會抓
            ours, ours_text = _judgement("relevant", "") if c in rel else _judgement("excluded", exc[c])
            theirs, theirs_text = _judgement(*mine[c])
            table[ours, theirs] += 1
            if ours != theirs:
                quote = mine[c][1] if mine[c][0] == "relevant" else "—"
                diffs.append(f"{qid} | {c} | 標註：{ours_text} | 盲審：{theirs_text} | 盲審引文：{quote}")
        if miss := sorted(cands - mine.keys()):
            missed.append(f"{qid}：{'、'.join(map(str, miss))}")
        if out := sorted(mine.keys() - cands):
            outside.append(f"{qid}：{'、'.join(map(str, out))}")
        for c, (kind, quote) in sorted(mine.items()):
            if kind == "relevant" and not (quote and _norm(quote) in _norm(_content(db, c))):
                bad_quotes.append(f"{qid} | {c} | {quote}")

    n = table.total()
    agree = sum(table[x, x] for x in CATEGORIES)
    print("標註＼盲審 | " + " | ".join(CATEGORIES))
    for x in CATEGORIES:
        print(f"{x} | " + " | ".join(str(table[x, y]) for y in CATEGORIES))
    print(f"一致率：{agree}/{n}（{agree / n:.1%}）" if n else "一致率：沒有可以對照的片段")
    sections = [("不一致的", diffs), ("盲審漏判的候選", missed), ("判了候選以外的片段", outside),
                ("盲審引文不是片段原文的", bad_quotes), ("盲審檔看不懂的", odd)]
    for title, lines in sections:
        print(f"{title}：" + ("\n" + "\n".join(lines) if lines else "無"))
    return 1 if any(lines for _, lines in sections) else 0


# ---------- planner 讀題 ----------

def _time_key(t) -> tuple:
    """語意相同就算一樣（例：春＝3、4、5 月；全天＝不限時段）。"""
    return (None, None, None) if t is None else (t.months, t.day_type, t.slots)


def _time_text(t) -> str:
    if t is None or _time_key(t) == (None, None, None):
        return "時間不限"
    return "／".join(("、".join(t.month) or "月份不限", t.day_type or "平假日不限", "、".join(t.time) or "時段不限"))


def load_plans(path: Path) -> dict[str, dict]:
    """`plan --save` 存的檔：題目 id → {"question": 題目文字, "plan": 檢索計畫（`Plan.to_dict` 的格式）}。"""
    return json.loads(path.read_text())["plans"] if path.exists() else {}


async def plan_check(db: DB, paths: list[Path], concurrency: int, save: Path | None = None) -> int:
    """讓 planner 只讀題目文字：每個子查詢解析出的地點和時間條件，要跟標註的 space／when（比較題是各選項）一樣。
    不一樣代表題目文字可能有別的讀法。地點只用快取解析（不打 Nominatim）。
    save：把每題的檢索計畫存檔給 `eval.backoff run --plans` 用；檔裡已經有、題目文字也沒變的題直接沿用，不叫 planner。"""
    from service.env import load_env
    from strag_v3.rag import llm
    from strag_v3.rag.plan import Plan

    load_env()
    sem = asyncio.Semaphore(concurrency)
    problems: list[str] = []
    old = load_plans(save) if save else {}
    plans: dict[str, dict] = {}

    async def one(q: dict):
        kept = old.get(q["id"])
        if kept and kept.get("question") == q["question"]:
            plan = Plan.from_dict(kept["plan"])
        else:
            async with sem:
                try:
                    plan = await llm.plan(q["question"])
                except Exception as ex:  # noqa: BLE001
                    problems.append(f"[planner 失敗] {q['id']}｜{ex}")
                    return
        plans[q["id"]] = {"question": q["question"], "plan": plan.to_dict()}
        got = []
        for sq in plan.sub_queries:
            pid, name = None, "不限地點"
            if sq.space:
                name = f"{sq.space.name}（{sq.space.name_ja}，{sq.space.kind}）"
                try:
                    p = db.places.resolve(sq.space.name_ja, sq.space.city, sq.space.kind)
                    pid = p.id if p else None
                except Exception:  # noqa: BLE001  快取沒有，實際跑會去查 Nominatim
                    pid = None
            got.append(((pid, _time_key(sq.time)), f"{pid or '快取解析不到'} {name} {_time_text(sq.time)}"))
        units = q["options"] if q.get("type") == "比較" else [q]
        want = [((u["space"]["place_id"], _time_key(parse_time(u.get("when")))),
                 f"{u['space']['place_id']} {u['space'].get('name')} {_time_text(parse_time(u.get('when')))}")
                for u in units]
        if Counter(x for x, _ in got) != Counter(x for x, _ in want):
            problems.append(f"[讀法不同] {q['id']}｜{q['question']}\n  標註：{'；'.join(s for _, s in want)}"
                            f"\n  planner：{'；'.join(s for _, s in got)}")

    qs = [q for path in paths for q in load_file(path)]
    await asyncio.gather(*(one(q) for q in qs))
    print("\n".join(sorted(problems)) if problems else f"{len(qs)} 題的讀法都跟標註一樣")
    if save:
        ordered = {q["id"]: plans[q["id"]] for q in qs if q["id"] in plans}
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(json.dumps({"plans": ordered}, ensure_ascii=False, indent=2) + "\n")
        reused = sum(old.get(i, {}).get("question") == p["question"] for i, p in ordered.items())
        print(f"{save}：{len(ordered)} 題的檢索計畫（沿用 {reused} 題，新問 {len(ordered) - reused} 題）")
    return len(problems)


# ---------- 標註 × JEV（.scratch/backoff/spec.md〈出題流程〉第 9 步） ----------

async def jev_diff(db: DB, gate, qs: list[dict], plans: dict[str, dict], floor: float) -> tuple[list[str], list[str], int]:
    """每題從第一層到 coverage 那層（none 是每一層）的候選，各問一次相關判斷，跟主系統同一個問法：
    原問題、檢索計畫裡子查詢的 aspect、片段內容（`gate.nouls`，結果進快取）。noul 不低於 floor 算 JEV 判相關。
    回傳 (跟標註不一致的片段和停層, 略過的題, 問了幾筆)。計畫沒有、題目文字改過、子查詢不只一個的題略過。"""
    from strag_v3.rag.plan import Plan
    from strag_v3.rag.store import Content

    diffs, skipped, asked = [], [], 0
    for q in sorted(qs, key=lambda q: _id_key(str(q.get("id")))):
        k = q["id"]
        saved = plans.get(k)
        if saved is None or saved.get("question") != q["question"]:
            skipped.append(f"{k}｜{'沒有存檔的檢索計畫' if saved is None else '題目文字跟存檔時不一樣'}，先跑 plan --save")
            continue
        subs = Plan.from_dict(saved["plan"]).sub_queries
        if len(subs) != 1:
            skipped.append(f"{k}｜檢索計畫有 {len(subs)} 個子查詢，對不到標註的層")
            continue
        rel = set(q.get("relevant_contents") or [])
        excluded = {int(i): r for i, r in (q.get("excluded") or {}).items()}
        nouls: dict[int, float] = {}
        stop = "none"
        for name, ids in db.judged_layers(q["space"], q.get("when"), q.get("coverage")):
            new = sorted(ids - nouls.keys())
            if not new:
                continue
            frags = [db.frag(c) for c in new]
            contents = [Content(f["id"], f["article"], f["poi_id"], f["poi"],
                                TimeSpec(f["month"], f["day_type"], f["time"]), f["content"] or "") for f in frags]
            nouls |= dict(zip(new, await gate.nouls(q["question"], subs[0].aspect, contents)))
            asked += len(new)
            for c in new:
                if (c in rel) != (nouls[c] >= floor):
                    ours = "收" if c in rel else f"不收（{excluded.get(c, '沒判')}）"
                    diffs.append(f"{k} | {name} | {c} | 標註：{ours} | JEV：{'相關' if nouls[c] >= floor else '不相關'}"
                                 f"（noul {nouls[c]:.2f}）")
            if stop == "none" and any(nouls[c] >= floor for c in ids):
                stop = name
        if stop != q.get("coverage"):
            diffs.append(f"{k} | 停層 | 標註：{q.get('coverage')} | JEV：{stop}")
    return diffs, skipped, asked


async def jev_diff_cmd(db: DB, db_path: Path, paths: list[Path], plans_path: Path, report: Path | None) -> int:
    from service.env import load_env
    from service.jev import JEVClient
    from strag_v3.rag.params import DEFAULT
    from strag_v3.rag.relevance import JEVGate

    load_env()
    qs = [q for path in paths for q in load_file(path)]
    conn = sqlite3.connect(db_path)  # jev_cache 要寫，另開一條可寫的連線
    client = JEVClient()
    try:
        gate = JEVGate(conn, client, DEFAULT.jev_model, DEFAULT.jev_concurrency)
        diffs, skipped, asked = await jev_diff(db, gate, qs, load_plans(plans_path), DEFAULT.jev_pass)
    finally:
        await client.aclose()
        conn.close()
    lines = [f"{len(qs)} 題、{asked} 筆候選問過 JEV（{DEFAULT.jev_model}，noul ≥ {DEFAULT.jev_pass} 算相關）；"
             f"跟標註不一致 {len(diffs)} 筆。只當參考，不是把關的依據。", "",
             "不一致的：" + ("" if diffs else "無"), *(f"- {d}" for d in diffs), "",
             "略過的題：" + ("" if skipped else "無"), *(f"- {s}" for s in skipped)]
    print("\n".join(lines))
    if report:
        write_section(report, "JEV 不一致清單", "\n".join(lines), "ptt_backoff 邊界題庫報告")
        print(f"已寫進 {report}〈JEV 不一致清單〉")
    return 0


# ---------- 找題材（邊界題庫，規則見 .scratch/backoff/spec.md〈出題流程〉第 1 步） ----------

# 面向 → 字詞。只用來找題材：片段含任何一個字詞就算「可能講到這個面向」，收不收仍照 generate.md〈收不收片段〉判。
# 寧可寬：N 組靠「每一層都沒有含關鍵字的候選」，漏字詞會把其實有資料的組合當成沒有。
ASPECTS: dict[str, tuple[str, ...]] = {
    "人潮多寡": ("人多", "人少", "人潮", "人山人海", "人滿為患", "人龍", "人很", "人超", "人不", "人也", "人非常", "人還",
                 "沒什麼人", "沒有人", "沒人", "沒幾個人", "遊客", "觀光客", "旅客", "擁擠", "很擠", "好擠", "超擠", "擠爆", "擠滿",
                 "爆滿", "客滿", "空蕩", "冷清", "包場", "滿滿的人", "熱門"),
    "排隊／等待時間": ("排隊", "排了", "排到", "要排", "在排", "隊伍", "人龍", "大排長龍", "等了", "等待", "等候", "候位",
                       "現場等", "免排", "不用排", "不用等", "沒排", "待ち", "快速通關", "抽號碼", "號碼牌", "整理券"),
    "開放狀況": ("開放", "沒開", "未開", "有開", "沒有開", "休館", "休園", "休息", "公休", "定休", "店休", "關門", "關了", "打烊",
                 "營業", "閉館", "閉園", "開門", "開園", "開館", "點燈", "停駛", "停運", "停開", "運休", "封閉", "封鎖", "整修",
                 "施工", "停業", "最後入場", "不能進", "無法進", "禁止進入"),
    "景觀能見度": ("能見度", "看得到", "看不到", "看得見", "看不見", "看到富士", "富士山", "夜景", "視野", "景色", "風景", "眺望",
                   "遠眺", "展望", "起霧", "霧", "雲", "清楚", "清晰", "日落", "夕陽", "日出"),
    "花況": ("櫻", "花況", "滿開", "盛開", "開花", "花期", "花季", "落花", "花吹雪", "花苞", "幾分", "紅葉", "楓", "銀杏", "轉紅",
             "變紅", "變色", "落葉", "見頃", "梅", "紫藤", "繡球", "賞花", "花海", "凋", "花開", "花都", "花已"),
    "交通狀況": ("電車", "地鐵", "公車", "巴士", "班次", "班車", "末班", "首班", "等車", "候車", "轉乘", "換車", "轉車", "搭車",
                 "車程", "車上", "車廂", "塞車", "誤點", "延誤", "月台", "接駁", "計程車", "新幹線", "列車", "搭乘", "利木津",
                 "JR", "站票", "位子", "座位", "直達", "分鐘"),
}
MOVE_ASPECT = "交通狀況"  # 這個面向出移動經驗，其餘出停留經驗或找地點
# generate.md〈選地點〉列的標錯地點，加上 ptt_v2/report.md 後來發現的兩個；id 是 data/ptt.db 的
BAD_PLACES = {647: "目黑川", 1006: "東京大學", 640: "池袋 Animate", 815: "合羽橋", 423: "江之島", 93: "隅田川 Terrace",
              478: "隅田川河岸", 471: "淺草橫町", 786: "函館山", 634: "東武淺草站",
              892: "江戶川競艇場", 622: "成田第二航廈"}
MAX_JUDGE = 12       # 要判的候選（第一層到停下的那層）合計上限；S 組的外圍層也不超過這個數
MAX_HITS = 3         # T、S 組停下的那層最多幾筆含關鍵字
MIN_ASPECT_POOL = 5  # N-面向：各層候選合計至少幾筆，否則跟 N-空沒有差別
GROUPS = {"T": ("時間退讓", "time_backoff"), "S": ("空間退讓", "space_backoff"), "N": ("沒有資料", "none")}
# 子類 → (組, 說明, spec 要求的最低題數)
BOUNDARIES = {
    "month": ("T", "只差月份一格", 2),
    "slot": ("T", "只差時段一格", 2),
    "ring": ("T", "跨環：12↔1 月，或晚上↔早上", 1),
    "poi_to_area": ("S", "問景點、退到行政區", 5),
    "area_to_area": ("S", "問區域、退到外圍區域", 2),
    "far": ("N", "相關經驗在隔兩格以上的月份或時段", 1),
    "day_type": ("N", "相關經驗只在另一種日型，月份時段相同", 1),
    "stacked": ("N", "外圍行政區有相關經驗，但只在相鄰的時間", 1),
    "aspect": ("N", "每一層都有候選，但沒有一筆含關鍵字", 1),
    "empty": ("N", "每一層的候選都是 0", 1),
}
GROUP_TARGET, GROUP_MIN = 8, 5
# 這三類幾乎每個地點的每個時間都符合（實測合計十萬列以上），清單裡同一個（地點、面向）只留幾列；可行性的數字仍算全部
CAPPED = ("far", "stacked", "empty")
KEEP = 2
BACKOFF_TYPES = ("停留經驗", "移動經驗", "找地點")


_MENTIONS: dict[tuple[str, str], bool] = {}


def mentions(f: dict, aspect: str) -> bool:
    key = (f["content"] or "", aspect)
    if key not in _MENTIONS:
        text = _norm(key[0])
        _MENTIONS[key] = any(w in text for w in ASPECTS[aspect])
    return _MENTIONS[key]


def time_gap(when: dict, f: dict) -> str | None:
    """時間退讓層的片段跟原條件差在哪：`month`、`slot`（只差那個維度一格）或 `ring`（那一格跨 12↔1 月或晚上↔早上）。
    兩個維度都差（或都沒差）不屬於任何子類，回傳 None。when 是單一月份、單一時段。"""
    m, s = int(when["month"][0]), when["time"][0]
    months = set(months_of(f["month"]))
    month_off, slot_off = m not in months, f["time"] not in (s, ALL_DAY)
    if month_off == slot_off:
        return None
    if month_off:
        return "ring" if {m, *months} == {12, 1} else "month"
    return "ring" if {s, f["time"]} == {SLOTS[0], SLOTS[-1]} else "slot"


def time_steps(when: dict, f: dict) -> tuple[int, int]:
    """片段的標籤離原條件幾格：(月份, 時段)，都是環狀的；標 `不明`、`全天` 的維度算 0。when 是單一月份、單一時段。"""
    m, s = int(when["month"][0]), when["time"][0]
    months = months_of(f["month"]) if f["month"] != "不明" else (m,)
    d_month = min(min((m - x) % 12, (x - m) % 12) for x in months)
    d_slot = 0 if f["time"] not in SLOTS else min((SLOTS.index(s) - SLOTS.index(f["time"])) % 4,
                                                   (SLOTS.index(f["time"]) - SLOTS.index(s)) % 4)
    return d_month, d_slot


def used_combos(folder: Path) -> set[tuple]:
    """資料夾的題目檔已經出過的（地點、面向、時間條件）；時間條件比語意（春＝3、4、5 月）。"""
    out = set()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = json.loads(path.read_text())
        for q in data.get("questions", []) if isinstance(data, dict) else []:
            out |= {(u["space"]["place_id"], q.get("aspect"), _time_key(parse_time(u.get("when"))))
                    for u in _units(q) if "space" in u}
    return out


def scan_spaces(db: DB, bad=BAD_PLACES) -> list[int]:
    """可以當 `space` 的地點：有片段的地點，加上範圍內有片段的區域；標錯的地點不算。"""
    with_content = {pid for (pid,) in db.conn.execute("SELECT DISTINCT poi_id FROM content")}
    areas = [pid for (pid,) in db.conn.execute("SELECT id FROM place WHERE kind = '區域' AND boundary IS NOT NULL")]
    return sorted((with_content | {a for a in areas if db.scope(a) & with_content}) - set(bad))


def scan(db: DB, used: set[tuple] = frozenset(), bad=BAD_PLACES) -> list[dict]:
    """掃全部「地點 × 月份 × 時段」，每個面向照 spec 的篩選條件歸到 T、S、N 的某個子類；不符合的不輸出。
    平日／假日不限；N-日型另外掃限定日型的組合（那些組合只找 `day_type`）。不呼叫模型。"""
    rows = []
    for pid in scan_spaces(db, bad):
        for m in range(1, 13):
            for slot in SLOTS:
                rows += _scan_combo(db, pid, {"month": [str(m)], "time": [slot]}, used)
                for day in DAY_TYPES:
                    rows += _scan_combo(db, pid, {"month": [str(m)], "day_type": day, "time": [slot]}, used)
    rows.sort(key=_row_key)
    return rows


def _row_key(r: dict) -> tuple:
    """依子類、地點、時間排：同一個地點的列排在一起，挑題時好顧「同一地點最多 3 題」。"""
    return (list(BOUNDARIES).index(r["boundary"]), r["space"]["place_id"], int(r["when"]["month"][0]),
            SLOTS.index(r["when"]["time"][0]), r["when"].get("day_type", ""), list(ASPECTS).index(r["aspect"]))


def shortlist(rows: list[dict], keep: int = KEEP) -> list[dict]:
    """要列出來的：`CAPPED` 的子類同一個（地點、面向）只留 keep 列，其餘全留。keep=0 是全部列出。
    `far` 留相關經驗離得最近的（剛好在退讓範圍外，最能測「只擴一格」），其餘照時間順序留前幾列。"""
    if not keep:
        return rows
    groups: dict[tuple, list[dict]] = defaultdict(list)
    out = []
    for r in rows:
        if r["boundary"] in CAPPED:
            groups[r["boundary"], r["space"]["place_id"], r["aspect"]].append(r)
        else:
            out.append(r)
    for rs in groups.values():
        out += sorted(rs, key=lambda r: sum(r.get("gap", {}).values()))[:keep]  # sorted 是穩定的
    return sorted(out, key=_row_key)


def _scan_combo(db: DB, pid: int, when: dict, used: set[tuple]) -> list[dict]:
    place = db.place(pid)
    if len(db.pool(pid, when)) > MAX_JUDGE:
        return []  # 第一層就超過要判的上限，T、S、N 都不可能
    names = [name for name, _ in db.layers(pid, when)]
    pools = {name: db.pool(layer_place(db, pid, name), when, name == "time_backoff") for name in names}
    day = when.get("day_type")
    outer = db.outer(pid)
    # 相關經驗可能落在的、檢索不會去的地方
    beyond = {"day_type": db.pool(pid, {**when, "day_type": next(d for d in DAY_TYPES if d != day)})} if day else {
        "stacked": db.pool(outer, when, widen=True) if outer is not None else [],
        "far": db.pool(pid),
    }
    out = []
    for aspect in ASPECTS:
        if (pid, aspect, _time_key(parse_time(when))) in used:
            continue
        hits = {n: [f for f in pools[n] if mentions(f, aspect)] for n in names}
        sure = {n: [f for f in hits[n] if not f["unknown_dims"]] for n in names}  # 標籤確定的才可能收
        related = {k: [f for f in fs if mentions(f, aspect) and not f["unknown_dims"]] for k, fs in beyond.items()}
        found = _classify(place.kind, when, names, pools, hits, sure, related)
        if found is None:
            continue
        boundary, stop, rel = found
        group = BOUNDARIES[boundary][0]
        area_type = "找地點" if group == "N" or len({f["poi_id"] for f in rel}) >= 2 else "停留經驗"  # 找地點要收到 2 個地點
        out.append({
            "group": group, "coverage": GROUPS[group][1], "boundary": boundary,
            "type": "移動經驗" if aspect == MOVE_ASPECT else area_type if place.kind == "區域" else "停留經驗",
            "space": {"place_id": pid, "name": place.name, "kind": place.kind},
            "when": when, "aspect": aspect,
            "judge": len({f["id"] for n in names[:stop] for f in pools[n]}),
            "layers": [{"layer": n, "place_id": layer_place(db, pid, n), "when": layer_when(when, n),
                        "candidates": [f["id"] for f in pools[n]], "hits": [f["id"] for f in hits[n]]}
                       for n in names[:stop]],
            "related": sorted(f["id"] for f in rel),
            # far：最近的相關經驗離原條件幾格
            **({"gap": dict(zip(("month", "slot"), min((time_steps(when, f) for f in rel), key=sum)))}
               if boundary == "far" else {}),
        })
    return out


def _classify(kind: str, when: dict, names: list[str], pools: dict, hits: dict, sure: dict,
              related: dict) -> tuple[str, int, list[dict]] | None:
    """一個（地點、時間條件、面向）屬於哪個子類：(boundary, 要判到第幾層, 相關經驗的片段)；都不符合是 None。
    pools／hits／sure：每層的候選、含關鍵字的、其中標籤確定的。related：檢索不會去的地方含關鍵字的片段
    （`far` 是這個地點不限時間、`stacked` 是外圍行政區的相鄰時間、`day_type` 是另一種日型）。"""
    def judged(stop: int) -> int:
        return len({f["id"] for n in names[:stop] for f in pools[n]})

    if not any(hits.values()):  # N：每一層都沒有含關鍵字的候選
        if judged(len(names)) > MAX_JUDGE:
            return None
        if "day_type" in when:
            order = ("day_type",)
        else:
            order = ("stacked", "far")
        for b in order:  # 相關經驗在哪就歸哪類；兩種都有時 stacked 優先（far 的題材多）
            if related.get(b):
                return b, len(names), related[b]
        if "day_type" in when:
            return None
        if all(pools[n] for n in names) and judged(len(names)) >= MIN_ASPECT_POOL:
            return "aspect", len(names), []
        return ("empty", len(names), []) if not judged(len(names)) else None
    if "day_type" in when or hits["exact"]:
        return None
    if "time_backoff" in names and hits["time_backoff"]:  # T
        stop = names.index("time_backoff") + 1
        gaps = {time_gap(when, f) for f in sure["time_backoff"]}
        if len(hits["time_backoff"]) <= MAX_HITS and len(gaps) == 1 and None not in gaps and judged(stop) <= MAX_JUDGE:
            return gaps.pop(), stop, sure["time_backoff"]
        return None
    if "space_backoff" in names and hits["space_backoff"]:  # S
        if (len(hits["space_backoff"]) <= MAX_HITS and sure["space_backoff"]
                and len(pools["space_backoff"]) <= MAX_JUDGE and judged(len(names)) <= MAX_JUDGE):
            return ("poi_to_area" if kind == "景點" else "area_to_area"), len(names), sure["space_backoff"]
    return None


def when_label(when: dict) -> str:
    return " ".join([f"{when['month'][0]} 月", *([when["day_type"]] if when.get("day_type") else []), when["time"][0]])


def scan_markdown(rows: list[dict], totals: Counter | None = None) -> str:
    """給人看的表：依組別和子類分段。N-空同一個（地點、時間條件）的各面向併成一列。
    totals 是每個子類篩選前符合的列數（`shortlist` 之前）。"""
    totals = totals or Counter(r["boundary"] for r in rows)
    layer_names = ("exact", "time_backoff", "space_backoff")
    out = ["# 邊界題庫的候選題材（`qgen.py scan`）", "",
           "每層寫「候選數（其中含關鍵字的筆數）」，沒有那層或不用判到那層寫 —。「要判」是從第一層到停下那層的候選合計。"
           "「相關片段」是標籤確定、含關鍵字的片段：T、S 組在停下的那層，N 組在檢索不會去的地方。"
           "完整的候選片段 id 在 `scan.json`。",
           f"`{'`、`'.join(CAPPED)}` 符合的組合太多，同一個（地點、面向）只列 {KEEP} 列"
           "（`far` 列相關經驗離得最近的，括號是最近那筆離原條件幾格）；要全部用 `scan --keep 0`。", ""]
    for g, (title, _) in GROUPS.items():
        out += [f"## {g} {title}", ""]
        for b, (bg, desc, _) in BOUNDARIES.items():
            mine = [r for r in rows if r["boundary"] == b]
            if bg != g:
                continue
            cut = f"，符合的共 {totals[b]} 列" if totals[b] != len(mine) else ""
            out += [f"### {b}：{desc}（{len(mine)} 列{cut}）", ""]
            if not mine:
                out += ["沒有符合的組合。", ""]
                continue
            out += ["| 地點 | 種類 | 時間條件 | 面向 | 題型 | " + " | ".join(layer_names) + " | 要判 | 相關片段 |",
                    "|---|---|---|---|---|" + "---|" * (len(layer_names) + 2)]
            merged: dict[tuple, list[dict]] = defaultdict(list)
            for r in mine:
                merged[(r["space"]["place_id"], when_label(r["when"])) if b == "empty" else id(r)].append(r)
            for rs in merged.values():
                r = rs[0]
                lay = {x["layer"]: f"{len(x['candidates'])}（{len(x['hits'])}）" for x in r["layers"]}
                out.append("| " + " | ".join([
                    f"{r['space']['place_id']} {r['space']['name']}", r["space"]["kind"], when_label(r["when"]),
                    "、".join(x["aspect"] for x in rs), "、".join(dict.fromkeys(x["type"] for x in rs)),
                    *(lay.get(n, "—") for n in layer_names), str(r["judge"]),
                    ("、".join(map(str, r["related"])) or "—")
                    + (f"（月份 {r['gap']['month']} 格、時段 {r['gap']['slot']} 格）" if "gap" in r else "")]) + " |")
            out.append("")
    return "\n".join(out)


def feasibility(rows: list[dict]) -> str:
    """report.md〈可行性〉的內文：每組、每個子類有幾個候選題材，哪些不到 spec 的最低題數。"""
    def places(rs: list[dict]) -> int:
        return len({r["space"]["place_id"] for r in rs})

    out = ["`qgen.py scan` 的結果（候選題材是機械篩出來的，還沒有人判過片段；清單在 `scan.md`、`scan.json`）。"
           "一列是一個（地點、時間條件、面向）。同一個地點在整份題庫最多 3 題，所以也列不同地點數。", "",
           "| 組 | 子類 | 候選題材 | 不同地點 | " + " | ".join(BACKOFF_TYPES) + " | 最低題數 | 夠不夠 |", "|---|---|---|---|---|---|---|---|---|"]
    short = []
    for g, (title, _) in GROUPS.items():
        mine = [r for r in rows if r["group"] == g]
        for b, (bg, desc, need) in BOUNDARIES.items():
            if bg != g:
                continue
            rs = [r for r in mine if r["boundary"] == b]
            ok = len(rs) >= need
            if not ok:
                short.append(f"{g} 組 `{b}`（{desc}）：{len(rs)} 個候選，最低要 {need} 題")
            out.append(f"| {g} | `{b}` | {len(rs)} | {places(rs)} | "
                       + " | ".join(str(sum(r["type"] == t for r in rs)) for t in BACKOFF_TYPES)
                       + f" | {need} | {'夠' if ok else '**不夠**'} |")
        types = Counter(r["type"] for r in mine)
        out.append(f"| {g} | 合計 | {len(mine)} | {places(mine)} | " + " | ".join(str(types[t]) for t in BACKOFF_TYPES)
                   + f" | {GROUP_TARGET}（底線 {GROUP_MIN}） | {'夠' if len(mine) >= GROUP_TARGET else '**不夠**'} |")
        if len(mine) < GROUP_TARGET:
            short.append(f"{g} 組合計：{len(mine)} 個候選，目標 {GROUP_TARGET} 題"
                         + ("，低於底線，要跟使用者確認合併或放棄" if len(mine) < GROUP_MIN else ""))
        if miss := [t for t in BACKOFF_TYPES if not types[t]]:
            short.append(f"{g} 組沒有{'、'.join(miss)}的候選（每組三種題型都要有）")
    out += ["", "不到最低題數的：" + ("無。" if not short else ""), *(f"- {s}" for s in short)]
    return "\n".join(out)


def write_section(path: Path, heading: str, body: str, title: str) -> None:
    """把 markdown 檔裡 `## heading` 那一節換成 body（到下一個 `## ` 為止）；沒有那一節就加在最後，沒有檔就新建。"""
    text = path.read_text() if path.exists() else f"# {title}\n"
    section = f"## {heading}\n\n{body.strip()}\n"
    pattern = re.compile(rf"^## {re.escape(heading)}\n.*?(?=^## |\Z)", re.S | re.M)
    if pattern.search(text):
        text = pattern.sub(lambda _: section + "\n", text, count=1).rstrip("\n") + "\n"
    else:
        text = text.rstrip("\n") + "\n\n" + section
    path.write_text(text)


def scan_files(db: DB, out: Path, used_folder: Path, keep: int = KEEP) -> tuple[list[dict], list[dict]]:
    """寫 scan.json、scan.md 和 report.md〈可行性〉；回傳 (符合的全部, 列出來的)。可行性算全部。"""
    rows = scan(db, used_combos(used_folder))
    shown = shortlist(rows, keep)
    totals = Counter(r["boundary"] for r in rows)
    out.mkdir(parents=True, exist_ok=True)
    head = {"說明": "qgen.py scan 的候選題材，一列一個（地點、時間條件、面向）。layers 只列要判的層；hits 是含面向關鍵字的候選；"
                    "related 是標籤確定、含關鍵字的片段（T、S 組在停下的那層，N 組在檢索不會去的地方）；"
                    "gap 是 far 最近的相關經驗離原條件幾格。"
                    f"{'、'.join(CAPPED)} 同一個（地點、面向）只列 {keep} 列，totals 是篩選前的列數。" if keep else
                    "qgen.py scan 的候選題材（全部）。",
            "totals": {b: totals[b] for b in BOUNDARIES}, "keywords": ASPECTS}
    body = ",\n".join(json.dumps(r, ensure_ascii=False) for r in shown)  # 一列一行，檔案才不會太大
    (out / "scan.json").write_text(json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "rows": [\n' + body + "\n ]\n}\n")
    (out / "scan.md").write_text(scan_markdown(shown, totals) + "\n")
    write_section(out / "report.md", "可行性", feasibility(rows), "ptt_backoff 邊界題庫報告")
    return rows, shown


# ---------- 命令列 ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=ROOT / "data/ptt.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("places")
    s.add_argument("--min", type=int, default=1, help="至少幾筆片段")
    sub.add_parser("areas")
    s = sub.add_parser("pool")
    s.add_argument("place_id", type=int)
    s.add_argument("--when", type=json.loads, default=None)
    s.add_argument("--widen", action="store_true")
    s = sub.add_parser("outer")
    s.add_argument("place_id", type=int)
    s = sub.add_parser("layers")
    s.add_argument("place_id", type=int)
    s.add_argument("--when", type=json.loads, default=None)
    s = sub.add_parser("frag")
    s.add_argument("ids", type=int, nargs="+")
    s = sub.add_parser("usage")
    s.add_argument("folder", type=Path, nargs="?", default=ROOT / "src/strag_v3/eval/questions/ptt_v2")
    s = sub.add_parser("merge")
    s.add_argument("file", type=Path, help="要寫的題目檔；給資料夾（不是 .json）就依題型併進 stay／move／find_place.json")
    s.add_argument("drafts", type=Path, nargs="+")
    s = sub.add_parser("blind-input")
    s.add_argument("file", type=Path)
    s.add_argument("qids", nargs="*", help="只帶這些題目（預設全部）")
    s = sub.add_parser("blind-diff")
    s.add_argument("file", type=Path)
    s.add_argument("blinds", type=Path, nargs="+", help="盲審檔；同一題同一筆，後面的檔蓋過前面的")
    s = sub.add_parser("check")
    s.add_argument("files", type=Path, nargs="+")
    s = sub.add_parser("plan")
    s.add_argument("files", type=Path, nargs="+")
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--save", type=Path, default=None,
                   help="把每題的檢索計畫存檔（給 eval.backoff run --plans）；檔裡題目文字沒變的題沿用，不再叫 planner")
    s = sub.add_parser("jev-diff")
    s.add_argument("files", type=Path, nargs="+")
    s.add_argument("--plans", type=Path, required=True, help="plan --save 存的檔：問 JEV 用它的 aspect")
    s.add_argument("--report", type=Path, default=None, help="把清單寫進這個 report.md 的〈JEV 不一致清單〉")
    s = sub.add_parser("answer")
    s.add_argument("file", type=Path)
    s.add_argument("qid")
    s = sub.add_parser("scan")
    s.add_argument("--out", type=Path, default=ROOT / "src/strag_v3/eval/questions/ptt_backoff")
    s.add_argument("--used", type=Path, default=ROOT / "src/strag_v3/eval/questions/ptt_v2",
                   help="這個資料夾的題目檔用過的（地點、面向、時間條件）不再出")
    s.add_argument("--keep", type=int, default=KEEP,
                   help=f"{'、'.join(CAPPED)} 同一個（地點、面向）列幾列；0 是全部（檔案會有上百 MB）")
    a = ap.parse_args(argv)
    db = DB(a.db)

    if a.cmd == "places":
        rows = db.conn.execute(
            "SELECT p.id, p.kind, p.name_zh, p.name_ja, p.city, COUNT(c.id) n FROM place p "
            "JOIN content c ON c.poi_id = p.id GROUP BY p.id HAVING n >= ? ORDER BY n DESC", (a.min,))
        for r in rows:
            print(" | ".join(str(x) for x in r))
    elif a.cmd == "areas":
        # 跟 Places.within 同一個判斷，但只看有片段的地點（沒片段的地點不影響片段數），快很多
        counts = dict(db.conn.execute("SELECT poi_id, COUNT(*) FROM content GROUP BY poi_id"))
        spots = db.content_places()
        rows = []
        for (pid,) in db.conn.execute("SELECT id FROM place WHERE kind = '區域' AND boundary IS NOT NULL"):
            area = db.places.get(pid)
            inside = [p for p in spots if p.id != pid and (p.kind == "景點" or p.rank > area.rank)
                      and area.contains(p.lat, p.lon)]
            if n := counts.get(pid, 0) + sum(counts[p.id] for p in inside):
                rows.append((n, pid, area.name_ja, area.city, len(inside)))
        for n, pid, name, city, k in sorted(rows, reverse=True):
            print(f"{pid} | {name} | {city} | 範圍內有片段的地點 {k} | 片段 {n}")
    elif a.cmd == "pool":
        for f in db.pool(a.place_id, a.when, a.widen):
            print(frag_line(f))
    elif a.cmd == "outer":
        o = db.outer(a.place_id)
        print(o and f"{o} | {db.places.get(o).name_ja}")
    elif a.cmd == "layers":
        for name, ids in db.layers(a.place_id, a.when):
            widen = name == "time_backoff"
            unk = sum(bool(unknown_dims(db.frag(i), a.when, widen=widen)) for i in ids)
            print(f"{name} | {place_text(db, layer_place(db, a.place_id, name))} | "
                  f"{cond_text(layer_when(a.when, name))} | 候選 {len(ids)} | 限定維度不明 {unk}")
    elif a.cmd == "frag":
        missing = 0
        for cid in a.ids:
            try:
                print(frag_line(db.frag(cid)))
            except KeyError as ex:
                print(ex.args[0])
                missing += 1
        return 1 if missing else 0
    elif a.cmd == "usage":
        usage(db, a.folder)
    elif a.cmd == "merge":
        return merge(a.file, a.drafts) if a.file.suffix == ".json" else merge_groups(a.file, a.drafts)
    elif a.cmd == "blind-input":
        qs = load_file(a.file)
        if miss := [i for i in a.qids if i not in {q.get("id") for q in qs}]:
            print(f"{a.file} 裡沒有這些題目：{'、'.join(miss)}")
            return 1
        items = [blind_input(db, q) for q in qs if not a.qids or q.get("id") in a.qids]
        print(json.dumps({"說明": BLIND_NOTE, "題目": items}, ensure_ascii=False, indent=2))
    elif a.cmd == "blind-diff":
        return blind_diff(db, a.file, a.blinds)
    elif a.cmd == "check":
        return 1 if check_files(db, a.files) else 0
    elif a.cmd == "plan":
        return 1 if asyncio.run(plan_check(db, a.files, a.concurrency, a.save)) else 0
    elif a.cmd == "jev-diff":
        return asyncio.run(jev_diff_cmd(db, a.db, a.files, a.plans, a.report))
    elif a.cmd == "answer":
        q = next(q for q in load_file(a.file) if q["id"] == a.qid)
        print(positive_answer(Question.from_dict(q), load_fragments(db.conn)))
    elif a.cmd == "scan":
        rows, shown = scan_files(db, a.out, a.used, a.keep)
        count, listed = Counter(r["boundary"] for r in rows), Counter(r["boundary"] for r in shown)
        print(f"{a.out}：scan.md、scan.json、report.md〈可行性〉；符合 {len(rows)} 列，列出 {len(shown)} 列")
        print("、".join(f"{b} {count[b]}" + (f"（列 {listed[b]}）" if listed[b] != count[b] else "") for b in BOUNDARIES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
