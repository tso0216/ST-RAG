"""案例的流程圖（簡報、論文用）：一個問題從頭到尾走一遍主系統。

① 問題 → ② 規劃器（檢索計畫）→ 每個子查詢一列：③ 地點範圍內 → ④ 時間相符 → ⑤ 相關判斷通過
→ ⑥ 整理（compose）→ ⑦ 生成器的回答（節錄，一個框）。

片段數和計畫的內容都從結果檔算，不手寫。回答太長，節錄是人挑的，放在 `result/casestudy_<id>_excerpt.json`，
裡面記了它節錄的是哪一份結果檔；結果檔換了（重跑過）就不畫回答，要重新節錄。只畫對照題（C1 這種）。
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

FONTS = ["PingFang TC", "Heiti TC", "Arial Unicode MS"]
INK, MUTED, LINE, SURFACE, GREY = "#0b0b0b", "#52514e", "#c9c8c2", "#ffffff", "#f3f2ef"
# 每列一個顏色（框線）和淡底；文字一律用墨色
LANES = [("#2a78d6", "#e6f0fc"), ("#eb6834", "#fdeee7")]
UNKNOWN = "不明"
W = 16.6
# 一列的高度、欄名那一條的高度、問題那一條的高度（英吋）
PITCH, HEAD, QUESTION = 1.5, 0.55, 0.7
# 子查詢、③、④、⑤ 四欄的 x 起點、寬度、欄名
COLS = [(3.9, 1.9, "子查詢"), (6.4, 1.7, "③ 地點範圍內"), (8.7, 1.7, "④ 時間相符"), (11.0, 2.6, "⑤ 相關判斷通過")]
PLAN_W = 3.2
COMPOSE_X, COMPOSE_W = 14.2, 2.3


def _when(t: dict) -> str:
    return "".join(t["month"]) + "天" * all(m in "春夏秋冬" for m in t["month"]) + "、".join(t["time"])


def lanes(case: dict) -> list[dict]:
    """每個子查詢一列：片段數的變化，和通過的片段裡時段有標的、時段不明的片段 id。"""
    evidence = {e["content_id"]: e for e in case["evidence"]}
    out = []
    for t in case["trace"]:
        layer = t["layers"][-1]
        passed = [evidence[j["id"]] for j in layer["judged"] if j["passed"]]
        out.append({"when": _when(t["sub_query"]["time"]), "place": t["sub_query"]["space"]["name"],
                    "pool": layer["pool"], "time_matched": layer["time_matched"], "passed": layer["passed"],
                    "labelled": [e["content_id"] for e in passed if e["time"]["time"] != UNKNOWN],
                    "unknown": [e["content_id"] for e in passed if e["time"]["time"] == UNKNOWN]})
    return out


def load_excerpt(path: Path, run: str) -> dict | None:
    """回答的節錄；沒有這個檔，或它節錄的不是這份結果檔，就回傳 None。"""
    if not path.exists():
        return None
    excerpt = json.loads(path.read_text())
    return excerpt if excerpt["run"] == run else None


def _box(ax, x, y, w, h, edge=LINE, face=SURFACE):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.1", linewidth=2,
                                edgecolor=edge, facecolor=face))


def _number(ax, x, y, w, h, value, sub, edge=LINE, face=SURFACE):
    _box(ax, x, y, w, h, edge, face)
    ax.text(x + w / 2, y + h / 2 + 0.17, value, ha="center", va="center", color=INK, fontsize=28, fontweight="bold")
    ax.text(x + w / 2, y + 0.26, sub, ha="center", va="center", color=MUTED, fontsize=14)


def _arrow(ax, x0, y0, x1, y1):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=18, linewidth=2, color=MUTED,
                                 shrinkA=4, shrinkB=4))


def _lines(ax, x, y, lines, size=15, step=0.36, color=INK):
    for i, line in enumerate(lines):
        ax.text(x, y - step * i, line, ha="left", va="center", color=color, fontsize=size)


def _head(ax, x, y, text, ha="center"):
    ax.text(x, y, text, ha=ha, va="center", color=MUTED, fontsize=15)


def _answer(ax, excerpt: dict, top: float):
    """回答區：一個框，每行一個粗體的標籤加一句話。"""
    _head(ax, 0.1, top - 0.3, "⑦ 生成器的回答（節錄）", ha="left")
    lines = excerpt["lines"]
    h = 0.5 * len(lines) + 0.4
    _box(ax, 0.1, top - 0.6 - h, W - 0.2, h)
    for i, (label, text) in enumerate(lines):
        y = top - 0.6 - 0.45 - 0.5 * i
        ax.text(0.4, y, label, ha="left", va="center", color=INK, fontsize=16, fontweight="bold")
        ax.text(2.2, y, text, ha="left", va="center", color=INK, fontsize=16)


def draw(case: dict, path: Path, excerpt: dict | None = None) -> Path:
    plt.rcParams["font.sans-serif"] = FONTS
    plt.rcParams["font.family"] = "sans-serif"
    rows = lanes(case)
    plan = case["plan"]
    answer_h = 0.5 * len(excerpt["lines"]) + 1.15 if excerpt else 0
    lanes_top = answer_h + PITCH * len(rows)
    height = lanes_top + HEAD + QUESTION
    fig = plt.figure(figsize=(W, height), dpi=200)
    ax = fig.add_axes((0, 0, 1, 1))  # 座標的 1 單位就是 1 英吋
    ax.set_xlim(0, W)
    ax.set_ylim(0, height)
    ax.axis("off")

    _head(ax, 0.1, height - 0.35, "① 問題", ha="left")
    ax.text(1.2, height - 0.35, f"「{case['question']}」", ha="left", va="center", color=INK, fontsize=19,
            fontweight="bold")
    _head(ax, 0.1 + PLAN_W / 2, lanes_top + 0.28, "② 規劃器：檢索計畫")
    for x, w, name in COLS:
        _head(ax, x + w / 2, lanes_top + 0.28, name)
    _head(ax, COMPOSE_X + COMPOSE_W / 2, lanes_top + 0.28, "⑥ 整理")

    space = plan["sub_queries"][0]["space"]
    mid = answer_h + PITCH * len(rows) / 2
    span = PITCH * len(rows) - 0.3  # 跨所有列的框的高度
    _box(ax, 0.1, mid - span / 2, PLAN_W, span)
    _lines(ax, 0.3, mid + 0.9, [f"地點　{space['name']}", f"　　　{space['name_ja']}（{space['city']}，{space['kind']}）",
                                f"面向　{plan['sub_queries'][0]['aspect']}", f"拆成　{len(rows)} 個子查詢",
                                f"compose = {plan['compose']}"], size=14, step=0.45)

    box_h = PITCH - 0.3
    for i, r in enumerate(rows):
        edge, face = LANES[i % len(LANES)]
        y = lanes_top - PITCH * i - PITCH / 2
        _arrow(ax, 0.1 + PLAN_W, mid, COLS[0][0], y)
        x, w, _ = COLS[0]
        _box(ax, x, y - box_h / 2, w, box_h, edge, face)
        ax.text(x + w / 2, y + 0.2, r["place"], ha="center", va="center", color=INK, fontsize=19, fontweight="bold")
        ax.text(x + w / 2, y - 0.28, r["when"], ha="center", va="center", color=INK, fontsize=16)
        _number(ax, COLS[1][0], y - box_h / 2, COLS[1][1], box_h, str(r["pool"]), "片段")
        _number(ax, COLS[2][0], y - box_h / 2, COLS[2][1], box_h, str(r["time_matched"]), "片段", edge, face)
        _number(ax, COLS[3][0], y - box_h / 2, COLS[3][1], box_h, str(r["passed"]),
                f"時段相符 {len(r['labelled'])}　時段不明 {len(r['unknown'])}", edge, face)
        for (x0, w0, _), (x1, _, _) in zip(COLS, COLS[1:]):
            _arrow(ax, x0 + w0, y, x1, y)
        _arrow(ax, COLS[3][0] + COLS[3][1], y, COMPOSE_X, y)
    _box(ax, COMPOSE_X, mid - span / 2, COMPOSE_W, span)
    ax.text(COMPOSE_X + COMPOSE_W / 2, mid + 0.25, plan["compose"], ha="center", va="center", color=INK, fontsize=19,
            fontweight="bold")
    ax.text(COMPOSE_X + COMPOSE_W / 2, mid - 0.3, f"{len(rows)} 段分開交給生成器", ha="center", va="center",
            color=MUTED, fontsize=14)

    if excerpt:
        _arrow(ax, COMPOSE_X + COMPOSE_W / 2, mid - span / 2, COMPOSE_X + COMPOSE_W / 2, answer_h - 0.5)
        _answer(ax, excerpt, answer_h)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path
