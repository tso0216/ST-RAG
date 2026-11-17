"""兩個實驗的統計：評審的判斷進，數字出。不呼叫任何外部服務。

一筆判斷（`Item`）是一題的一條 expect（覆蓋）或一題的無支持，帶它被問 n 次的 noul。沒送評審的題沒有判斷。
"""
from dataclasses import dataclass

from ..baseline.judge import PASS, Verdict
from ..baseline.questions import Question

COVERAGE, UNSUPPORTED = "覆蓋", "無支持"
KINDS = (COVERAGE, UNSUPPORTED)
# 依 n 次 noul 的平均分段，看翻轉集中在哪一段
BANDS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    # 第幾條 expect（從 1 起算）；無支持是 0
    index: int
    nouls: tuple[float, ...]

    @property
    def label(self) -> str:
        return f"expect {self.index}" if self.kind == COVERAGE else UNSUPPORTED

    @property
    def yes(self) -> tuple[bool, ...]:
        return tuple(n >= PASS for n in self.nouls)

    @property
    def flipped(self) -> bool:
        return len(set(self.yes)) > 1

    @property
    def spread(self) -> float:
        return max(self.nouls) - min(self.nouls)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.id, self.kind, self.index)


def items(verdicts: list[Verdict]) -> list[Item]:
    out = []
    for v in verdicts:
        out += [Item(v.id, COVERAGE, i, tuple(ns)) for i, ns in enumerate(v.coverage, 1)]
        if v.unsupported:
            out.append(Item(v.id, UNSUPPORTED, 0, tuple(v.unsupported)))
    return out


@dataclass(frozen=True)
class Stability:
    total: int
    flips: int
    mean_spread: float
    max_spread: float
    # 每個分段：（判斷數, 翻轉數）
    bands: tuple[tuple[int, int], ...]

    @property
    def agreement(self) -> float | None:
        return 1 - self.flips / self.total if self.total else None


def _band(x: float) -> int:
    return min(int(x / 0.2), len(BANDS) - 1)


def stability(its: list[Item]) -> Stability:
    bands = [[0, 0] for _ in BANDS]
    for it in its:
        b = bands[_band(sum(it.nouls) / len(it.nouls))]
        b[0] += 1
        b[1] += it.flipped
    spreads = [it.spread for it in its]
    return Stability(len(its), sum(it.flipped for it in its),
                     sum(spreads) / len(spreads) if spreads else 0.0, max(spreads, default=0.0),
                     tuple((n, f) for n, f in bands))


def round_verdicts(verdicts: list[Verdict], i: int) -> list[Verdict]:
    """只留第 i 次（從 0 起算）的判斷，當成一份 n=1 的評審結果。"""
    return [Verdict(v.id, v.skipped, [[ns[i]] for ns in v.coverage], v.unsupported[i:i + 1]) for v in verdicts]


def answer_metrics(questions: list[Question], verdicts: list[Verdict]) -> tuple[float, float]:
    """（expect 覆蓋率, 無支持比例），算法同 baseline 的指標：逐題平均，沒送評審的題覆蓋率 0、無支持「沒有」。"""
    vs = {v.id: v for v in verdicts}
    cov, uns = [], []
    for q in questions:
        v = vs.get(q.id, Verdict(q.id, skipped=True))
        cov.append(sum(v.covered) / len(q.expect) if q.expect else 0.0)
        uns.append(float(v.has_unsupported))
    return sum(cov) / len(cov), sum(uns) / len(uns)


@dataclass(frozen=True)
class Confusion:
    """兩個評審對同一批判斷的是／否：a 是 → b 是、a 是 → b 否、a 否 → b 是、a 否 → b 否。"""
    yy: int = 0
    yn: int = 0
    ny: int = 0
    nn: int = 0

    @property
    def total(self) -> int:
        return self.yy + self.yn + self.ny + self.nn

    @property
    def agreement(self) -> float | None:
        return (self.yy + self.nn) / self.total if self.total else None

    @property
    def kappa(self) -> float | None:
        """Cohen's κ；兩個評審都全判同一邊時（期望一致率是 1）沒有定義。"""
        if not self.total:
            return None
        t = self.total
        expected = ((self.yy + self.yn) * (self.yy + self.ny) + (self.ny + self.nn) * (self.yn + self.nn)) / (t * t)
        return None if expected == 1 else ((self.yy + self.nn) / t - expected) / (1 - expected)


@dataclass(frozen=True)
class Pair:
    """同一筆判斷在兩個評審的結果（各看第 1 次）。"""
    a: Item
    b: Item

    @property
    def agree(self) -> bool:
        return self.a.yes[0] == self.b.yes[0]


def pairs(a: list[Verdict], b: list[Verdict]) -> list[Pair]:
    """兩邊都有的判斷，依 a 的順序。"""
    other = {it.key: it for it in items(b)}
    return [Pair(it, other[it.key]) for it in items(a) if it.key in other]


def confusion(ps: list[Pair]) -> Confusion:
    count = {(x, y): 0 for x in (True, False) for y in (True, False)}
    for p in ps:
        count[(p.a.yes[0], p.b.yes[0])] += 1
    return Confusion(count[(True, True)], count[(True, False)], count[(False, True)], count[(False, False)])
