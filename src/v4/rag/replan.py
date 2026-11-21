"""⑦ 補規劃（規格 14）：組輸入（14.1）、呼叫 LLM、交給 `validate.py`；格式不合法時附上問題清單重問一次。

輸出格式跟 ③ 一樣，另外多「從哪個節點放寬」（`relax_from`）。不加任何節點就輸出空的，代表查完了。
"""
import json
import logging

from v4.extract.data_schema import Article

from . import config, plan, status
from .data_schema import REPLAN_SCHEMA, Batch, Question, QState
from .validate import Ctx

log = logging.getLogger(__name__)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def budget(state: QState) -> dict:
    """還剩幾輪、還能加幾個節點、這一輪最多加幾個。"""
    left = config.MAX_NODES - len(state.nodes)
    return {"這是第幾輪補規劃": state.replans + 1,
            "這一輪之後還剩幾輪": config.REPLAN_ROUNDS - state.replans - 1,
            "整題還能加幾個節點": left,
            "這一輪最多加幾個": min(config.REPLAN_NODES, left)}


def build_user(article: Article, question: Question, ctx: Ctx, state: QState) -> str:
    """14.1 的輸入。"""
    nodes = [status.result_brief(n, state.results.get(n.no)) for n in state.nodes]
    b = budget(state)
    return "\n\n".join([
        plan.article_block(article),
        plan.places_block(ctx.places),
        plan.itinerary_block(ctx.days, ctx.places),
        plan.question_block(question),
        "<nodes>\n" + (_json(nodes) if nodes else "（還沒有節點）") + "\n</nodes>",
        "<status_table>\n" + _json(status.table(state)) + "\n</status_table>",
        "<todo>\n" + (_json(status.todos(state)) if status.todos(state) else "（沒有）") + "\n</todo>",
        "<no_data>\n" + (_json(status.no_data(state)) if status.no_data(state) else "（沒有）") + "\n</no_data>",
        f"<limit>這是第 {b['這是第幾輪補規劃']} 輪補規劃，這一輪之後還剩 {b['這一輪之後還剩幾輪']} 輪；"
        f"整題還能加 {b['整題還能加幾個節點']} 個節點，這一輪最多加 {b['這一輪最多加幾個']} 個。"
        "其他條件下的筆數都沒有做過相關判斷。</limit>",
    ])


async def replan(article: Article, question: Question, ctx: Ctx, state: QState) -> Batch:
    """⑦：回傳這一輪的 `Batch`。`batch.empty` 是 LLM 一個節點都沒輸出（查完了）。"""
    system = (config.PROMPT_DIR / "replan.md").read_text()
    round_no = state.replans + 1
    known = {n.no for n in state.nodes}
    relaxable = {n.no for n in state.nodes if n.no in state.results and not state.results[n.no].found}
    batch = await plan.ask_nodes(system, build_user(article, question, ctx, state), REPLAN_SCHEMA, "replan.md",
                                 f"第 {question.no} 題 補規劃第 {round_no} 輪", ctx, state.nodes, replan=True,
                                 round_no=round_no, known=known, relaxable=relaxable)
    reasons = "；".join(f"{n.no}：{n.reason}" for n in batch.accepted)
    log.info("第 %d 題 補規劃第 %d 輪：%s", question.no, round_no,
             "輸出空的，查完了" if batch.empty else f"通過 {len(batch.accepted)} 個、拿掉 {len(batch.dropped)} 個"
             + (f"（{reasons}）" if reasons else ""))
    return batch
