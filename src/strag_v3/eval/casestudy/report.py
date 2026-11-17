"""把案例研究的結果檔寫成 markdown：每個案例一節（檢索計畫、檢索過程、證據、答案、檢查）。"""
from ...rag.timematch import SEASONS

FLAGS = {"time_backoff": "時間退讓", "space_backoff": "空間退讓", "time_unknown": "時間不明"}
# 一個案例最多列幾筆證據；超過的只寫筆數（完整的在結果檔）
MAX_EVIDENCE = 8


def _time(t: dict | None) -> str:
    if not t:
        return "不限"
    months = "、".join(m if m in SEASONS else f"{m} 月" for m in t["month"])  # 季節（春夏秋冬）不加「月」
    parts = [months, t["day_type"] or "", "、".join(t["time"])]
    return " ".join(p for p in parts if p) or "不限"


def _cell(text: str) -> str:
    return str(text).replace("|", "｜").replace("\n", " ")


def _counts(pairs: list) -> str:
    return "、".join(f"{label} {n} 筆" for label, n in pairs) or "無"


def _plan(plan: dict) -> list[str]:
    out = ["| 子查詢 | 地點 | 時間 | 面向 |", "|---|---|---|---|"]
    for i, sq in enumerate(plan["sub_queries"], 1):
        sp = sq["space"]
        place = f"{sp['name']}（{sp['kind']}）" if sp else "不限"
        out.append(f"| {i} | {place} | {_time(sq['time'])} | {_cell(sq['aspect'] or '（同問題）')} |")
    out.append(f"\ncompose：{plan['compose']}；group_by：{'、'.join(plan['group_by']) or '無'}")
    return out


def _trace(trace: list[dict]) -> list[str]:
    out = ["| 子查詢 | 層 | 時間條件 | 地點範圍內 | 時間相符 | 相關判斷通過 |", "|---|---|---|---|---|---|"]
    for i, t in enumerate(trace, 1):
        for layer in t["layers"]:
            name = layer["layer"] + (f"（{layer['area']}）" if layer["area"] else "")
            out.append(f"| {i} | {name} | {_time(layer['time'])} | {layer['pool']} | {layer['time_matched']} "
                       f"| {layer['passed']} |")
        out.append(f"| {i} | **停在：{t['stage']}** | | | | |")
    return out


def _evidence(evidence: list[dict]) -> list[str]:
    if not evidence:
        return ["沒有證據。"]
    out = ["| 片段 | 地點 | 月份 | 平日／假日 | 時段 | noul | 標記 | 內容 |", "|---|---|---|---|---|---|---|---|"]
    for e in evidence[:MAX_EVIDENCE]:
        flags = "、".join(name for key, name in FLAGS.items() if e[key]) or "—"
        t = e["time"]
        out.append(f"| {e['content_id']} | {_cell(e['place'])} | {t['month']} | {t['day_type']} | {t['time']} "
                   f"| {e['relevance']:.2f} | {flags} | {_cell(e['content'])} |")
    if len(evidence) > MAX_EVIDENCE:
        out.append(f"\n另有 {len(evidence) - MAX_EVIDENCE} 筆，見結果檔。")
    return out


def case_section(c: dict) -> str:
    lines = [f"## {c['id']}　{c['mechanism']}", "", f"問題：{c['question']}（{c['source']}）", ""]
    if c.get("error"):
        return "\n".join(lines + [f"錯誤：{c['error']}", ""])
    lines += ["### 檢索計畫", "", *_plan(c["plan"]), "", "### 檢索過程", "", *_trace(c["trace"]), ""]
    for t in c["trace"]:
        if t["stage"] == "沒有證據":
            lines += [f"有資料的時段：{_counts(t['available']['time'])}；月份：{_counts(t['available']['month'])}", ""]
    lines += [f"### 證據（{len(c['evidence'])} 筆）", "", *_evidence(c["evidence"]), "",
              "### 答案", "", *(f"> {line}" for line in c["answer"].splitlines()), "",
              "### 檢查", "",
              f"- 預期停在「{c['expect_stage']}」，實際：{'、'.join(t['stage'] for t in c['trace'])}"
              f"　→　{'符合' if c['stage_ok'] else '不符合'}",
              f"- 該看什麼：{c['look_for']}", ""]
    return "\n".join(lines)


def report(run: dict) -> str:
    cfg = run["config"]
    head = ["# 案例研究（strag full）", "",
            f"結果檔：`{cfg['run']}`；生成模型 {cfg['model']}；相關判斷 {cfg['jev_model']}（noul ≥ {cfg['jev_pass']}）；"
            f"DB {cfg['db']}。", "",
            "| 案例 | 機制 | 問題 | 子查詢 | 停在 | 證據 | 符合預期 |", "|---|---|---|---|---|---|---|"]
    for c in run["cases"]:
        if c.get("error"):
            head.append(f"| {c['id']} | {c['mechanism']} | {_cell(c['question'])} | — | 錯誤 | — | — |")
            continue
        head.append(f"| {c['id']} | {c['mechanism']} | {_cell(c['question'])} | {len(c['trace'])} "
                    f"| {'、'.join(t['stage'] for t in c['trace'])} | {len(c['evidence'])} "
                    f"| {'是' if c['stage_ok'] else '否'} |")
    return "\n".join(head + [""] + [case_section(c) for c in run["cases"]])
