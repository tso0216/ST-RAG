"""gate 比較的成本表（markdown）。品質表直接用 baseline 的 `report`。

每一列是一份結果檔的每題平均。full（JEV）的結果檔只記了判斷的片段數，沒記 token、金額、時間，那幾格留空。
"""
from ..baseline.report import _table
from ..baseline.results import Outcome

COST_HEAD = ("gate", "判斷片段／題", "通過片段／題", "gate 輸入 token／題", "gate 輸出 token／題",
             "gate 估計金額（USD／題）", "gate API 時間（秒／題）", "embedding 字元／題",
             "生成器輸入 token／題", "LLM 呼叫／題", "JEV 呼叫／題")


def gate_label(config: dict) -> str:
    gate = config.get("gate")
    if gate is None:  # baseline 的 strag
        return "jev（full）"
    if gate["name"] == "llm":
        return f"llm（{gate['model']}，{gate['effort']}）"
    return f"semantic（δ={gate['delta']}）"


def _mean(xs: list) -> str:
    """有任何一題沒記就留空。"""
    if not xs or any(x is None for x in xs):
        return ""
    m = sum(xs) / len(xs)
    return f"{m:.4f}" if 0 < abs(m) < 1 else f"{m:.1f}"


def cost_row(config: dict, outcomes: list[Outcome]) -> list[str]:
    done = [o for o in outcomes if o.error is None]  # 出錯的題沒有成本可算
    gates = [o.extra.get("gate") or {} for o in done]
    is_full = config.get("gate") is None

    def col(key: str, scale: float = 1.0) -> str:
        return _mean([g[key] * scale if key in g else None for g in gates])

    candidates = _mean([o.cost.jev_calls for o in done]) if is_full else col("candidates")
    return [gate_label(config), candidates, _mean([len(o.contents) for o in done]),
            col("input_tokens"), col("output_tokens"), col("cost_usd"), col("duration_ms", 0.001), col("embed_chars"),
            _mean([o.cost.input_tokens for o in done]), _mean([o.cost.llm_calls for o in done]),
            _mean([o.cost.jev_calls for o in done])]


def cost_table(runs: list[tuple[dict, list[Outcome]]]) -> str:
    return _table(COST_HEAD, [cost_row(config, outcomes) for config, outcomes in runs])


NOTE = ("判斷片段是交給 gate 的候選片段數（三組用同一份檢索計畫，沒有退讓時三組相同）；"
        "llm 的 token、金額、時間是各次呼叫加總，讀快取的照當初那次呼叫的算，金額是照 API 定價估的；"
        "jev（full）的結果檔沒有記 gate 的 token、金額、時間。出錯的題不算進成本。")
