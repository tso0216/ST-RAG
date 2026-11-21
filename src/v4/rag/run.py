"""⑤ 的執行順序（規格 12.1）：同一輪的節點同時跑；排法的計算等同一輪的其他節點都跑完才跑；
每一輪最後，這一題所有天節點的排法的計算都用到目前為止的點、段節點重算一次（不算新節點）。

同時請求數由 `Env` 裡的 `Gate`（JEV）和 `llm_sem`（LLM）限制，整題共用。
"""
import asyncio

from . import days, nodes
from .data_schema import NODE_DAY, NODE_MOVE, NODE_POINT, Node, NodeResult, QState
from .nodes import Env


async def run_node(node: Node, env: Env) -> NodeResult:
    if node.kind == NODE_POINT:
        return await nodes.point(node, env)
    if node.kind == NODE_MOVE:
        return await nodes.move(node, env)
    assert node.kind == NODE_DAY
    return await days.day(node, env)


async def run_round(batch: list[Node], env: Env, state: QState) -> None:
    """跑這一輪的節點，結果寫進 state.results；其中一個出錯時取消其他的，再把錯誤往上丟。"""
    tasks = [asyncio.ensure_future(run_node(n, env)) for n in batch]
    try:
        done = await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    for node, result in zip(batch, done):
        state.results[node.no] = result
    recalc(state, env)


def recalc(state: QState, env: Env) -> None:
    """所有天節點的排法的計算重算一次（排法至少兩站解析得到才有）。"""
    pairs = state.pairs()
    for node, result in pairs:
        if node.kind == NODE_DAY:
            result.calc = nodes.calc(node, pairs, env.conn)
