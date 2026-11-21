"""③ 初始計畫（規格 10）：組輸入（10.1）、呼叫 LLM、交給 `validate.py`；格式不合法時附上問題清單重問一次。

`ask_nodes` 也給 ⑦（`replan.py`）用：呼叫 LLM → 格式檢查（不合法的重問一次，重新輸出的整份再檢查一次，
還不合法的節點拿掉）→ `validate.accept`（地點解析、改了什麼、去重複、數量）。
這裡另外放 ③、⑦、⑧ 共用的輸入區塊：發問文、地點列表、使用者的行程、問句。
"""
import logging

from v4.extract import config as extract_config
from v4.extract import feedback
from v4.extract.check import Problem
from v4.extract.data_schema import Article, PlaceRef

from . import config, trace, validate
from .data_schema import PLAN_SCHEMA, Batch, Node, Question, UserDay
from .validate import Ctx

log = logging.getLogger(__name__)


# ---------- 共用的輸入區塊 ----------

def article_block(article: Article) -> str:
    return f"<article>\n標題：{article.title}\n發文日期：{article.posted_date}\n主文：\n{article.text}\n</article>"


def places_block(places: dict[str, PlaceRef]) -> str:
    """① 的地點列表：編號、繁中名稱、日文名稱、城市、景點或區域。"""
    if not places:
        return "<places>\n（沒有）\n</places>"
    rows = [f"{p.ref}｜{p.name_zh}｜{p.name_ja}｜{p.city or '城市沒寫'}｜{p.kind}" for p in places.values()]
    return "<places>\n編號｜繁中名稱｜日文名稱｜城市｜景點或區域\n" + "\n".join(rows) + "\n</places>"


def itinerary_block(days: list[UserDay], places: dict[str, PlaceRef]) -> str:
    """使用者的行程：D1、D2……、原文寫法、月份、照順序的站（地點編號、繁中名稱、景點或住宿）。"""
    if not days:
        return "<itinerary>\n沒有按天排的行程\n</itinerary>"
    rows = []
    for i, d in enumerate(days, 1):
        stops = " → ".join(f"{s.ref} {places[s.ref].name_zh if s.ref in places else '？'}（{s.type}）"
                           for s in d.stops)
        rows.append(f"D{i}（原文：{d.label}）｜月份 {d.month or '沒寫'}｜{stops or '沒有站'}")
    return "<itinerary>\n" + "\n".join(rows) + "\n</itinerary>"


def question_block(question: Question) -> str:
    return f'<question type="{question.qtype}">{question.text}</question>'


def build_user(article: Article, question: Question, ctx: Ctx) -> str:
    """10.1 的輸入。"""
    return "\n\n".join([article_block(article), places_block(ctx.places), itinerary_block(ctx.days, ctx.places),
                        question_block(question), f"<limit>這一題最多建 {config.MAX_NODES} 個節點。</limit>"])


# ---------- 呼叫 LLM、交給 ④ ----------

async def ask_nodes(system: str, user: str, schema: dict, prompt: str, label: str, ctx: Ctx,
                    existing: list[Node], *, replan: bool, round_no: int, known: set[str] = frozenset(),
                    relaxable: set[str] | None = None) -> Batch:
    """呼叫 ③ 或 ⑦ 的 LLM，經過 ④ 回傳這一輪的 `Batch`（通過的、拿掉的、照輸出順序的項目、紀錄）。"""
    outputs: list[dict] = []
    attempts: list[list[str]] = []

    def check(out: dict) -> list[Problem]:
        outputs.append(out)
        problems = validate.check_format(out.get("nodes") or [], ctx, known=known, relaxable=relaxable)
        attempts.append([str(p) for p in problems])
        return problems

    start = len(ctx.resolver.log)
    with trace.step(label, config.MODEL):
        out, problems = await feedback.ask(system, user, schema, check, label, prompt, model=config.MODEL,
                                           effort=config.EFFORT, retries=config.PLAN_RETRIES)
    raws = out.get("nodes") or []
    ok, format_dropped = validate.format_drops(raws, problems, ctx, round_no)
    with trace.step(f"{label}：地點解析", extract_config.MODEL):
        batch = await validate.accept(ok, ctx, existing, replan=replan, round_no=round_no)
    batch.dropped = sorted([*format_dropped, *batch.dropped], key=lambda d: d.index)
    batch.order = sorted([*batch.order, *(("drop", d, d.index) for d in format_dropped)], key=lambda x: x[2])
    batch.empty = not raws
    batch.record = {
        "提示詞": prompt,
        "輸入": user,
        "每次的輸出": outputs,
        "每次檢查出的問題": attempts,
        "重問後還有的問題": [str(p) for p in problems],
        "地點解析": ctx.resolver.log[start:],
        "通過的節點": [trace.node_record(n) for n in batch.accepted],
        "拿掉的節點": [trace.dropped_record(d) for d in batch.dropped],
    }
    return batch


async def initial(article: Article, question: Question, ctx: Ctx) -> Batch:
    """③：每一題呼叫一次。"""
    system = (config.PROMPT_DIR / "plan.md").read_text()
    batch = await ask_nodes(system, build_user(article, question, ctx), PLAN_SCHEMA, "plan.md",
                            f"第 {question.no} 題 初始計畫", ctx, [], replan=False, round_no=0)
    log.info("第 %d 題 初始計畫：通過 %d 個、拿掉 %d 個", question.no, len(batch.accepted), len(batch.dropped))
    return batch
