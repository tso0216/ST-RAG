"""程式檢查欄位：兩次 llm 解析各自的檢查，回傳問題清單。

檢查的是 llm 的原始輸出（照 `data_schema.LINE_SCHEMA`、`NOTE_SCHEMA` 的 dict）。
每個問題記下是哪一類的第幾筆，回報 llm 時當成問題清單，第二次解析重試後還有問題時用來拿掉那幾筆。
"""
import re
from dataclasses import dataclass
from functools import cache

from . import config
from .data_schema import DAY_TYPES, LOCATED_UNKNOWN, STOP_PREFIX

_HOUR = r"(?:\d|1\d|2[0-3])"
TIME_RE = re.compile(rf"^{_HOUR}(?:-{_HOUR})?$")
MONTH_RE = re.compile(r"^(?:[1-9]|1[0-2])$")
YEAR_RE = re.compile(r"^\d{4}$")
PLACE_REF_RE = re.compile(r"^p\d+$")
STOP_RE = re.compile(rf"^([{''.join(STOP_PREFIX)}])(\d+)$")   # 第一次解析的一站：p3、h3


@dataclass(frozen=True)
class Problem:
    kind: str       # places、days、place_notes、moves、day_notes
    index: int      # 那一類的第幾筆，從 0 開始
    message: str

    def __str__(self) -> str:
        return f"{self.kind}[{self.index}]：{self.message}"


@cache
def time_slots() -> dict[str, str]:
    """`time_slots.md` 的時段對到要存的範圍，例如 早上 → 5-10。"""
    slots = {}
    for line in config.TIME_SLOTS_PATH.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = len(cells) == 2 and re.match(r"(\d{1,2})\s*[–-]\s*(\d{1,2})", cells[1])
        if m:
            slots[cells[0]] = f"{m.group(1)}-{m.group(2)}"
    return slots


def check_line(out: dict, lines: list[str]) -> list[Problem]:
    """檢查第一次解析（線）的輸出。lines 是主文的每一行（行號從 1 起算）。"""
    problems = _check_places(out["places"], taken=set())
    for i, p in enumerate(out["places"]):
        if p["ref"].strip() and not PLACE_REF_RE.match(p["ref"]):
            problems.append(Problem("places", i, f"ref {p['ref']} 要是 p 加數字（p1、p2）"))
        if not 1 <= p["line"] <= len(lines):
            problems.append(Problem("places", i, f"line {p['line']} 不在主文的 1～{len(lines)} 行裡"))
        elif not lines[p["line"] - 1].strip():
            problems.append(Problem("places", i, f"line {p['line']} 是空行，要給提到這個地點的那一行"))
    refs = {p["ref"] for p in out["places"]}
    for i, day in enumerate(out["days"]):
        for stop in day["stops"]:
            m = STOP_RE.match(stop)
            if not m:
                problems.append(Problem("days", i, f"stops 的 {stop} 要是 p 或 h 加數字（景點 p3、住宿 h3）"))
            elif (ref := f"p{m.group(2)}") not in refs:
                why = "" if ref == stop else f"（沒有 {ref}）"
                problems.append(Problem("days", i, f"stops 的 {stop} 不在 places 裡{why}"))
        if day["month"] and not MONTH_RE.match(day["month"]):
            problems.append(Problem("days", i, f"month「{day['month']}」要是 1–12 的一個月份，或留空"))
    return problems


def check_note(out: dict, line: dict) -> list[Problem]:
    """檢查第二次解析（點、段、day_note）的輸出。line 是第一次解析的輸出。"""
    places = {p["ref"]: p for p in [*line["places"], *out["places"]]}
    problems = _check_places(out["places"], taken={p["ref"] for p in line["places"]})
    total = len(line["days"])

    def check_ref(kind: str, i: int, name: str, ref: str) -> None:
        if ref not in places:
            problems.append(Problem(kind, i, f"{name} 的 {ref} 不在 places 裡"))
        elif places[ref]["located"] == LOCATED_UNKNOWN:
            problems.append(Problem(kind, i, f"{name} 的 {ref} 是猜不出來的地點，不能引用"))

    for i, n in enumerate(out["place_notes"]):
        check_ref("place_notes", i, "place", n["place"])
        problems += _check_text("place_notes", i, n)
        problems += _check_times("place_notes", i, n["times"])
    for i, m in enumerate(out["moves"]):
        check_ref("moves", i, "from", m["from"])
        check_ref("moves", i, "to", m["to"])
        if m["from"] == m["to"]:
            problems.append(Problem("moves", i, "from 和 to 不能是同一個地點"))
        problems += _check_text("moves", i, m)
        problems += _check_times("moves", i, m["times"])
    for i, n in enumerate(out["day_notes"]):
        wrong = [d for d in n["days"] if not 1 <= d <= total]
        if wrong:
            problems.append(Problem("day_notes", i, f"days 的 {wrong} 不在 1～{total} 裡（`<line>` 每一天的 no）"))
        if len(set(n["days"])) != len(n["days"]):
            problems.append(Problem("day_notes", i, "days 有重複的天"))
        problems += _check_text("day_notes", i, n)
    return problems


def drop(out: dict, problems: list[Problem]) -> int:
    """把有問題的那幾筆從 out 拿掉，回傳拿掉幾筆。"""
    bad: dict[str, set[int]] = {}
    for p in problems:
        bad.setdefault(p.kind, set()).add(p.index)
    for kind, indexes in bad.items():
        out[kind] = [item for i, item in enumerate(out[kind]) if i not in indexes]
    return sum(len(indexes) for indexes in bad.values())


def _check_places(places: list[dict], taken: set[str]) -> list[Problem]:
    """`ref` 不重複（也不能跟 taken 裡已經用掉的重複），`name_zh`、`name_ja` 不是空的。"""
    problems, seen = [], set(taken)
    for i, p in enumerate(places):
        if not p["ref"].strip():
            problems.append(Problem("places", i, "ref 是空的"))
        elif p["ref"] in seen:
            problems.append(Problem("places", i, f"ref {p['ref']} 重複"))
        seen.add(p["ref"])
        for name in ("name_zh", "name_ja"):
            if not p[name].strip():
                problems.append(Problem("places", i, f"{name} 是空的"))
    return problems


def _check_text(kind: str, i: int, item: dict) -> list[Problem]:
    return [] if item["content"].strip() else [Problem(kind, i, "content 是空的")]


def _check_times(kind: str, i: int, times: list[dict]) -> list[Problem]:
    problems = []
    for t in times:
        if t["year"] and not YEAR_RE.match(t["year"]):
            problems.append(Problem(kind, i, f"times 的 year「{t['year']}」要是四位數的年份，或留空"))
        if t["month"] and not MONTH_RE.match(t["month"]):
            problems.append(Problem(kind, i, f"times 的 month「{t['month']}」要是 1–12 的一個月份，或留空；季節要一個月給一筆"))
        if t["day_type"] and t["day_type"] not in DAY_TYPES:
            problems.append(Problem(kind, i, f"times 的 day_type「{t['day_type']}」要留空，或是 {'、'.join(DAY_TYPES)}"))
        if t["time"] and not TIME_RE.match(t["time"]):
            slot = time_slots().get(t["time"])
            hint = f"；「{t['time']}」照時段表存 {slot}" if slot else ""
            problems.append(Problem(kind, i, f"times 的 time「{t['time']}」要是 0–23 的一個鐘點或 H-H，或留空{hint}"))
    return problems
