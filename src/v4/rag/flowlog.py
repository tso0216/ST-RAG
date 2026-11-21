"""看得懂的流程紀錄（`ask --log`）：兩個檔案。

- 主檔（`--log` 給的路徑）：用白話寫每一輪查了什麼、為什麼、找到什麼、還缺什麼，最後是答案和花費。
- 細節檔（同資料夾，檔名加 `.detail`，例如 `ask.log` → `ask.detail.log`）：參數、每一步的 LLM 用量，
  和跑的時候 logger `v4` 的訊息（每次 llm 的完整輸入和輸出、重問、地點解析每一輪怎麼查、怎麼挑）。

- `render(record, detail_name)`：主檔的內容，從 `pipeline.run_article` 的紀錄（`Result.record`）組出來。
- `render_detail(record, details)`：細節檔的內容。
- `Capture`：跑的時候收集 logger `v4` 的訊息，當細節檔的 details。
- `write(path, record, details)`：寫兩個檔案。

這裡只讀紀錄、不改紀錄。紀錄缺的鍵（出錯時停在半路）就不寫那一段。
"""
import logging
from pathlib import Path

from .data_schema import AROUND, ANYWHERE, HAS_NO_NOTE, NODE_DAY, NODE_MOVE, NODE_POINT, NONE, ST_HAS, ST_RELAXED

MAX_POINTS = 3   # 主檔每個節點最多列幾點找到的重點（全部在答案和細節檔裡）


class Capture(logging.Handler):
    """`with Capture() as c:` 期間把 logger `v4` 開到 INFO、收集訊息；出來時恢復原本的層級。"""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))
        self.lines: list[str] = []
        self._level = logging.NOTSET

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def __enter__(self) -> "Capture":
        logger = logging.getLogger("v4")
        self._level = logger.level
        logger.addHandler(self)
        logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc) -> None:
        logger = logging.getLogger("v4")
        logger.removeHandler(self)
        logger.setLevel(self._level)

    def text(self) -> str:
        return "\n".join(self.lines)


def detail_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.detail{path.suffix}")


def write(path: Path, record: dict, details: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    detail = detail_path(path)
    path.write_text(render(record, detail.name), encoding="utf-8")
    detail.write_text(render_detail(record, details), encoding="utf-8")


# ---------- 主檔 ----------

def render(record: dict, detail_name: str = "") -> str:
    out: list[str] = []
    _input(out, record.get("輸入") or {})
    first = record.get("拆題與解析") or {}
    if "拆題" in first:
        _split(out, first["拆題"])
    if "行程解析" in first:
        _itinerary(out, first["行程解析"])
    for q in record.get("每一題") or []:
        _question(out, q)
    if "錯誤" in record:
        out += ["", f"✗ 出錯，停在這裡：{record['錯誤']}"]
    if "用量" in record:
        out += ["", _cost(record["用量"])]
    if detail_name:
        out.append(f"每次 LLM 的完整輸入和輸出、參數：{detail_name}")
    return "\n".join(out).strip("\n") + "\n"


def _input(out: list[str], inp: dict) -> None:
    if "問題文字" in inp:
        out.append(f"問題：{inp['問題文字']}")
    else:
        out.append(f"文章：{inp.get('標題', '')}（{inp.get('文章 id', '')}）")
        out += ["  " + line for line in str(inp.get("主文", "")).splitlines()]


def _retried(attempts: list[list[str]], what: str) -> list[str]:
    """LLM 輸出有問題、重問過才寫一行。"""
    if not any(attempts):
        return []
    return [f"（{what}的 LLM 輸出有問題，重問 {len(attempts) - 1} 次）"]


def _split(out: list[str], s: dict) -> None:
    questions = s.get("題目") or []
    if len(questions) == 1:
        out.append(f"題目：{questions[0]['問句']}（{questions[0]['題型']}）")
    else:
        out.append(f"拆成 {len(questions)} 題：")
        out += [f"  {q['第幾題']}. {q['問句']}（{q['題型']}）" for q in questions]
    out += _retried(s.get("每次的問題") or [], "拆題")


def _itinerary(out: list[str], it: dict) -> None:
    if not it.get("做了嗎"):
        return
    places = it.get("地點列表") or []
    if places:
        out.append("提到的地點：" + "、".join(p["繁中名稱"] for p in places))
        for p in places:
            if p.get("找得到嗎") != "是":
                out.append(f"  （{p['繁中名稱']}是{p.get('找得到嗎')}的：{p.get('依據', '')}）")
    days = it.get("使用者的行程") or []
    if days:
        out.append("你的行程：")
        names = {p["編號"]: p["繁中名稱"] for p in places}
        for d in days:
            stops = " → ".join(names.get(s.split("（")[0], s) for s in d["站"])
            out.append(f"  {d['原文']}：{stops}")
    bad = [s for s in it.get("行程裡的站的解析") or [] if not s.get("place_id")]
    for s in bad:
        out.append(f"  ✗ 行程裡的 {s['名稱']} 在地圖上找不到（{s.get('原因')}）")


# ---------- 每一題 ----------

def _question(out: list[str], q: dict) -> None:
    out += ["", f"━━ 第 {q.get('第幾題')} 題：{q.get('問句')} ━━"]
    for r in q.get("每一輪") or []:
        _round(out, r)
    if "為什麼停" in q:
        out += ["", f"停下來：{q['為什麼停']}"]
    _used(out, q)
    if "答案" in q:
        out += ["", "答案：", str(q["答案"])]


def _round(out: list[str], r: dict) -> None:
    n = r.get("第幾輪", 0)
    out += ["", f"── 第 {n + 1} 輪：{'先查' if n == 0 else '補查'} ──"]
    plan = r.get("計畫") or {}
    accepted = plan.get("通過的節點") or []
    dropped = plan.get("拿掉的節點") or []
    if "節點" not in r and not accepted and not dropped:
        out.append("LLM 認為不用再查了")
        return
    out += _retried(plan.get("每次檢查出的問題") or [], "規劃")
    places = plan.get("地點解析") or []
    if places:
        out.append("地點對到：" + "、".join(_place(e) for e in places))
    if accepted:
        out.append("要查：")
        for node in accepted:
            out.append(f"  {node['編號']} {_what(node)}")
            if node.get("面向"):
                out.append(f"     想知道：{node['面向']}")
            if node.get("理由"):
                out.append(f"     為什麼：{node['理由']}")
    for d in dropped:
        out.append(f"  ✗ 不查：{d.get('節點')}（{d.get('說明') or d.get('原因')}）")
    results = r.get("節點") or []
    if results:
        out.append("結果：")
        for x in results:
            _result(out, x["節點"], x["結果"])
    new = [t for t in r.get("還沒查的事") or [] if t.get("第一次出現") == n]
    if new:
        out.append("還缺：")
        out += [f"  · {t['說明']}" for t in new]


def _place(e: dict) -> str:
    used = e.get("用的")
    if e.get("place_id") is None:
        return f"{e['地點']} → 找不到（{e.get('原因') or e.get('結果')}）"
    target = used["name"] if used else f"place {e['place_id']}"
    return e["地點"] if target == e["地點"] else f"{e['地點']} → {target}"


def _where(sc: dict | None) -> str:
    if not sc:
        return "？"
    if sc.get("寫法") == ANYWHERE:
        return "任何地方"
    if sc.get("寫法") == AROUND:
        return f"{sc.get('地點')}周圍 {sc.get('半徑（公里）')} 公里"
    return str(sc.get("地點"))


def _what(n: dict) -> str:
    """一個節點要查什麼，白話。"""
    kind = n.get("種類")
    if kind == NODE_POINT:
        text = f"{_where(n.get('對象'))}的經驗"
    elif kind == NODE_MOVE:
        text = f"{_where(n.get('起點'))} → {_where(n.get('終點'))} 怎麼移動"
        if n.get("反方向也要"):
            text += "（兩個方向都算）"
    elif n.get("排法"):
        text = f"同一天去過{'、'.join(n['排法'])}的行程"
    else:
        text = "所有行程的每日評語"
    times = [f"{k} {v}" for k, v in (n.get("時間條件") or {}).items() if v != "不限"]
    if times:
        text += f"｜時間：{'、'.join(times)}"
    if n.get("從哪個節點放寬"):
        changes = [c for c in n.get("改了什麼") or [] if not c.startswith("面向")]
        text += f"（放寬 {n['從哪個節點放寬']}：{'；'.join(changes)}）" if changes else f"（{n['從哪個節點放寬']} 換個問法）"
    return text


def _result(out: list[str], node: dict, res: dict) -> None:
    no = node["編號"]
    evidence = len(res.get("證據") or [])
    arranged = node.get("種類") == NODE_DAY and node.get("排法展開")
    found = res.get("狀態") != NONE
    if found:
        if arranged:
            text = f"✓ 找到 {len(res.get('天') or [])} 天別人的行程（加上評語共 {evidence} 筆）"
        else:
            text = f"✓ 找到 {evidence} 筆"
            if (res.get("候選筆數") or 0) > evidence:
                text += f"（從 {res['候選筆數']} 筆裡挑出相關的）"
        if res.get("狀態") == HAS_NO_NOTE:
            text += "，但沒有評語"
    else:
        candidates, matched = res.get("候選筆數") or 0, res.get("時間相符筆數") or 0
        if candidates == 0:
            text = "✗ 沒有人的行程同一天去過這些地方" if arranged else "✗ 資料庫沒有這種經驗"
        elif matched == 0:
            text = f"✗ 有 {candidates} 筆，但時間都對不上"
        else:
            text = f"✗ 有 {matched} 筆，但都跟問題無關"
    out.append(f"  {no} {text}")
    points = ((res.get("整理重點") or {}).get("points")) or []
    out += [f"     · {p['text']}" for p in points[:MAX_POINTS]]
    if len(points) > MAX_POINTS:
        out.append(f"     …還有 {len(points) - MAX_POINTS} 點（見答案）")


def _used(out: list[str], q: dict) -> None:
    """合成時每一項用到哪個節點：最後一輪的查詢狀態表。"""
    rounds = [r for r in q.get("每一輪") or [] if r.get("查詢狀態表")]
    if not rounds:
        return
    found, missing = [], []
    for row in rounds[-1]["查詢狀態表"]:
        if "節點" not in row:
            continue
        state = row.get("狀態", "")
        if state.startswith(ST_RELAXED):
            found.append(f"{row.get('用的節點')}（代替 {row['節點']}）")
        elif state.startswith(ST_HAS):
            found.append(row["節點"])
        else:
            missing.append(row["節點"])
    out.append("回答用到：" + ("、".join(found) or "沒有") + (f"；沒找到：{'、'.join(missing)}" if missing else ""))
    if (q.get("合成") or {}).get("補上開頭"):
        out.append("（整題沒有證據，答案開頭由程式補上固定的那句）")


def _cost(u: dict) -> str:
    llm = u.get("LLM") or {}
    jev = u.get("JEV") or {}
    sent = (u.get("embedding") or {}).get("送了幾段文字", 0)
    return (f"花費：LLM {llm.get('次數', 0)} 次、約 ${llm.get('估計金額（美元）', 0):.2f}｜相關判斷 {jev.get('問了幾次', 0)} 次"
            + (f"｜embedding {sent} 段" if sent else "") + f"｜共 {u.get('時間（秒）')} 秒")


# ---------- 細節檔 ----------

def render_detail(record: dict, details: str = "") -> str:
    out: list[str] = []
    if "參數" in record:
        out.append("━━ 參數 ━━")
        out += [f"{k}：{v}" for k, v in record["參數"].items()]
    usage = record.get("用量") or {}
    steps: dict[str, list] = {}
    for c in (usage.get("LLM") or {}).get("每一次") or []:
        s = steps.setdefault(c.get("步驟") or "（沒標步驟）", [0, 0, 0, 0.0, 0, c.get("模型")])
        s[0] += 1
        s[1] += c.get("輸入 token") or 0
        s[2] += c.get("輸出 token") or 0
        s[3] += c.get("估計金額（美元）") or 0
        s[4] += c.get("時間（毫秒）") or 0
    if steps:
        out += ["", "━━ 每一步的 LLM 用量（時間是每次相加，同時跑的會重疊）━━"]
        for label, (n, tin, tout, cost, ms, model) in steps.items():
            out.append(f"{label}：{n} 次｜{model}｜輸入 {tin:,}、輸出 {tout:,} token｜${cost:.3f}｜{ms / 1000:.0f} 秒")
    if details:
        out += ["", "━━ 每一步的細節（每次 llm 的完整輸入和輸出、重問、地點解析每一輪怎麼查）━━", details]
    return "\n".join(out).strip("\n") + "\n"
