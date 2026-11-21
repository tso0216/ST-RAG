"""分流評估（規格 19.1）：每題的「問題內容」當成一句問題輸入，只跑 ① 的拆題（第 1、2 步），
拿拆出的第一題的題型跟人工標的比。不解析行程和站。輸出正確率、混淆矩陣、每一題的結果。
"""
import asyncio
from collections.abc import Callable

from service import llm
from v4.rag import config, parse
from v4.rag.data_schema import QTYPES

from .questions import LabeledQuestion


async def classify(q: LabeledQuestion) -> dict:
    article = parse.from_question(q.text)
    record: dict = {}
    split = await parse.split(article, record)
    return {"key": q.key, "問題內容": q.text, "人工": q.label, "預測": split[0].qtype,
            "拆出的題": [{"問句": x.text, "題型": x.qtype} for x in split],
            "重問次數": record.get("重問次數", 0), "修正": record.get("修正", [])}


async def evaluate(items: list[LabeledQuestion], concurrency: int = config.LLM_CONCURRENCY,
                   on_done: Callable[[int, int, dict], None] | None = None) -> tuple[list[dict], str | None]:
    """逐題拆題。回傳（做完的結果（照輸入的順序），錯誤）。LLM 出錯時取消還沒做完的，已經做完的留著。"""
    sem = asyncio.Semaphore(concurrency)
    results: dict[int, dict] = {}

    async def one(i: int, q: LabeledQuestion) -> None:
        async with sem:
            r = await classify(q)
        results[i] = r
        if on_done:
            on_done(len(results), len(items), r)

    tasks = [asyncio.ensure_future(one(i, q)) for i, q in enumerate(items)]
    error = None
    try:
        await asyncio.gather(*tasks)
    except llm.LLMError as e:
        error = f"{type(e).__name__}: {e}"
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return [results[i] for i in sorted(results)], error


def score(results: list[dict]) -> dict:
    """正確率和混淆矩陣（列是人工標的題型，欄是預測的題型）。"""
    matrix = {a: {b: 0 for b in QTYPES} for a in QTYPES}
    for r in results:
        matrix[r["人工"]][r["預測"]] += 1
    correct = sum(matrix[a][a] for a in QTYPES)
    per = {}
    for a in QTYPES:
        n = sum(matrix[a].values())
        per[a] = {"題數": n, "答對": matrix[a][a], "正確率": round(matrix[a][a] / n, 4) if n else None}
    return {"題數": len(results), "答對": correct,
            "正確率": round(correct / len(results), 4) if results else None,
            "各題型": per, "混淆矩陣（列：人工，欄：預測）": matrix}


def lines(summary: dict) -> list[str]:
    """印在終端機的總結。"""
    out = [f"正確率：{summary['答對']}/{summary['題數']}"
           + (f"（{summary['正確率']:.1%}）" if summary["正確率"] is not None else "")]
    matrix = summary["混淆矩陣（列：人工，欄：預測）"]
    width = max(len(t) for t in QTYPES) + 2
    out.append("人工＼預測".ljust(width) + "".join(t.rjust(width) for t in QTYPES))
    for a in QTYPES:
        out.append(a.ljust(width) + "".join(str(matrix[a][b]).rjust(width) for b in QTYPES))
    return out
