"""兩個實驗的報告（markdown）和逐筆的 CSV 列。"""
from ..baseline.judge import Verdict
from ..baseline.questions import Question
from ..baseline.report import _table
from .compare import (BANDS, COVERAGE, KINDS, Item, answer_metrics, confusion, items, pairs, round_verdicts,
                      stability)


def _rate(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def _num(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def _nouls(it: Item) -> str:
    return " ".join(f"{n:.2f}" for n in it.nouls)


def _point(questions: dict[str, Question], it: Item) -> str:
    """這筆判斷在問什麼：覆蓋是那條 expect，無支持沒有。"""
    return questions[it.id].expect[it.index - 1] if it.kind == COVERAGE else ""


def stability_report(questions: list[Question], repeat: list[Verdict], model: str) -> str:
    its = items(repeat)
    n = len(its[0].nouls) if its else 0
    by_id = {q.id: q for q in questions}
    rows, band_rows = [], []
    for kind, group in [(k, [it for it in its if it.kind == k]) for k in KINDS] + [("合計", its)]:
        s = stability(group)
        rows.append([kind, str(s.total), str(s.flips), _rate(s.agreement), _num(s.mean_spread), _num(s.max_spread)])
        band_rows.append([kind] + [f"{f} / {t}" for t, f in s.bands])
    metric_rows = []
    for i in range(n):
        cov, uns = answer_metrics(questions, round_verdicts(repeat, i))
        metric_rows.append([f"第 {i + 1} 次", _num(cov), _num(uns)])
    flips = [[it.id, it.label, _nouls(it), _point(by_id, it)] for it in its if it.flipped]
    return "\n\n".join([
        f"## 一、JEV 重問 {n} 次（{model}）",
        f"同一筆判斷用同樣的輸入問 {n} 次。不一致是 {n} 次的是／否（noul ≥ 0.5）不全相同；"
        f"noul 全距是 {n} 次裡最大減最小。",
        _table(("判斷", "筆數", "不一致", "一致率", "noul 全距平均", "noul 全距最大"), rows),
        f"依 {n} 次 noul 的平均分段，每格是「不一致／筆數」：",
        _table(("判斷",) + tuple(f"{lo:.1f}–{hi:.1f}" for lo, hi in BANDS), band_rows),
        "每一次單獨當成評審結果時的兩個答案層指標（50 題逐題平均）：",
        _table(("", "expect 覆蓋率", "無支持比例"), metric_rows),
        f"### 不一致的判斷（{len(flips)} 筆）",
        _table(("題目", "判斷", f"{n} 次的 noul", "重點"), flips) if flips else "沒有。",
    ])


def compare_report(questions: list[Question], jev: list[Verdict], llm: list[Verdict],
                   repeat: list[Verdict] | None, jev_label: str, llm_label: str) -> str:
    ps = pairs(jev, llm)
    by_id = {q.id: q for q in questions}
    unstable = {it.key for it in items(repeat or []) if it.flipped}
    rows = []
    for kind, group in [(k, [p for p in ps if p.a.kind == k]) for k in KINDS] + [("合計", ps)]:
        c = confusion(group)
        rows.append([kind, str(c.total), str(c.yy), str(c.yn), str(c.ny), str(c.nn), _rate(c.agreement), _num(c.kappa)])
    metric_rows = [[name, *map(_num, answer_metrics(questions, vs))] for name, vs in ((jev_label, jev), (llm_label, llm))]
    differ = [p for p in ps if not p.agree]
    both = sum(p.a.key in unstable for p in differ)
    differ_rows = [[p.a.id, p.a.label, f"{p.a.nouls[0]:.2f}", "是" if p.b.yes[0] else "否",
                    ("是" if p.a.key in unstable else "") if repeat is not None else "—", _point(by_id, p.a)]
                   for p in differ]
    note = (f"其中 {both} 筆是 JEV 重問時自己也不一致的判斷。" if repeat is not None else "")
    return "\n\n".join([
        f"## 二、LLM 評審對照（{llm_label} 對 {jev_label}）",
        "同樣的問法和輸入，各問一次。「是」在覆蓋是「有涵蓋這條重點」，在無支持是「有無支持的說法」。",
        _table(("判斷", "筆數", "都是", "JEV 是／LLM 否", "JEV 否／LLM 是", "都否", "一致率", "Cohen's κ"), rows),
        "兩個評審各自算出來的答案層指標（50 題逐題平均）：",
        _table(("評審", "expect 覆蓋率", "無支持比例"), metric_rows),
        f"### 兩個評審不一致的判斷（{len(differ)} 筆）",
        note,
        _table(("題目", "判斷", "JEV noul", "LLM", "JEV 重問不一致", "重點"), differ_rows) if differ_rows else "沒有。",
    ])


STABILITY_HEAD = ("id", "kind", "index", "nouls", "flipped", "spread")
COMPARE_HEAD = ("id", "kind", "index", "jev_noul", "jev_yes", "llm_yes", "agree", "jev_repeat_flipped")


def stability_rows(repeat: list[Verdict]) -> list[list]:
    return [[it.id, it.kind, it.index, " ".join(map(str, it.nouls)), int(it.flipped), round(it.spread, 4)]
            for it in items(repeat)]


def compare_rows(jev: list[Verdict], llm: list[Verdict], repeat: list[Verdict] | None) -> list[list]:
    unstable = {it.key for it in items(repeat or []) if it.flipped}
    return [[p.a.id, p.a.kind, p.a.index, p.a.nouls[0], int(p.a.yes[0]), int(p.b.yes[0]), int(p.agree),
             "" if repeat is None else int(p.a.key in unstable)] for p in pairs(jev, llm)]
