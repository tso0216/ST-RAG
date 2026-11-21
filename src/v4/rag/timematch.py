"""時間比對（規格 5）：鐘點範圍、經驗的時間列跟條件比、時間不明的判斷。

一筆經驗有零到多列時間（`TimeRow`）；只要一列符合所有條件就算時間相符。符合的那一列在條件有限制的維度上是空的，
那個維度記成不明；好幾列都符合時挑不明的維度最少的那一列。沒有任何時間列的經驗當成一列全空的。

天和 `day_note` 沒有時間表：天用 `day.month`（`day_rows`），`day_note` 用它講到的天的月份（`month_rows`），
年都看文章的發文年份。
"""
from dataclasses import dataclass

from .data_schema import TimeCond

YEAR = "年份"
MONTH = "月份"
DAY_TYPE = "平日／假日"
HOURS = "時段"


@dataclass(frozen=True)
class TimeRow:
    """經驗的一列時間。空的欄位是 None。month 是 `1`–`12`；time 是一個鐘點 `H` 或範圍 `H-H`。"""
    year: int | None = None
    month: str | None = None
    day_type: str | None = None
    time: str | None = None

    def label(self) -> str:
        parts = [f"{self.year} 年" if self.year else "",
                 f"{self.month} 月" if self.month else "",
                 self.day_type or "",
                 f"{self.time} 點" if self.time else ""]
        return " ".join(p for p in parts if p) or "沒寫時間"


@dataclass(frozen=True)
class Match:
    ok: bool
    unknown: tuple[str, ...] = ()   # 時間不明的維度（照 YEAR、MONTH、DAY_TYPE、HOURS 的順序）
    row: TimeRow | None = None      # 符合的那一列


def hour_range(start: int, end: int) -> set[int]:
    """H1–H2 的鐘點：H1 ≤ H2 是 H1 到 H2，H1 > H2 是跨夜（H1 到 23，加上 0 到 H2）。"""
    if start <= end:
        return set(range(start, end + 1))
    return set(range(start, 24)) | set(range(0, end + 1))


def hours_of(text: str | None) -> set[int]:
    """經驗的鐘點欄位（`15`、`14-16`、`22-2`）變成鐘點的集合；空的或讀不出來是空集合。"""
    text = (text or "").strip()
    if not text:
        return set()
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(text)
    except ValueError:
        return set()
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return set()
    return hour_range(start, end)


def cond_hours(cond: TimeCond) -> set[int]:
    """條件的時段：好幾個範圍取聯集。"""
    out: set[int] = set()
    for start, end in cond.hours:
        out |= hour_range(start, end)
    return out


def match_row(row: TimeRow, cond: TimeCond, posted_year: int | None) -> Match:
    unknown: list[str] = []
    if cond.year is not None:
        if row.year is not None:
            if row.year < cond.year:
                return Match(False)
        elif posted_year is not None and posted_year < cond.year:
            return Match(False)   # 經驗不會晚於發文
        else:
            unknown.append(YEAR)
    if cond.months:
        if not row.month:
            unknown.append(MONTH)
        elif _int(row.month) not in cond.months:
            return Match(False)
    if cond.day_type:
        if not row.day_type:
            unknown.append(DAY_TYPE)
        elif row.day_type != cond.day_type:
            return Match(False)
    if cond.hours:
        hours = hours_of(row.time)
        if not hours:
            unknown.append(HOURS)
        elif not hours & cond_hours(cond):
            return Match(False)
    return Match(True, tuple(unknown), row)


def match(rows: list[TimeRow], cond: TimeCond, posted_year: int | None) -> Match:
    """一筆經驗的時間列跟條件比。沒有任何時間列時當成一列全空的。"""
    best: Match | None = None
    for row in rows or [TimeRow()]:
        m = match_row(row, cond, posted_year)
        if m.ok and (best is None or len(m.unknown) < len(best.unknown)):
            best = m
    return best or Match(False)


def day_rows(month: str | None) -> list[TimeRow]:
    """一天的時間：只有 `day.month`（年看發文年份）。"""
    return [TimeRow(month=month or None)]


def month_rows(months: list[str | None]) -> list[TimeRow]:
    """`day_note` 的時間：講到的天（講整趟的是那篇所有天）的月份，任一個符合就算。"""
    return [TimeRow(month=m or None) for m in months] or [TimeRow()]


def year_of(posted_at: str | None) -> int | None:
    try:
        return int((posted_at or "")[:4])
    except ValueError:
        return None


def _int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
