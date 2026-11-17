"""llm 解析：點、段、線的 day_note（推文）：組提示詞、推文分批、呼叫 llm、合併各批結果。"""
import json
import logging

from . import check, config, feedback
from .data_schema import NOTE_SCHEMA, Article, Comment

log = logging.getLogger(__name__)

SCOPE_ALL = "主文和這批推文"
SCOPE_COMMENTS = "只有這批推文"


def batches(comments: list[Comment]) -> list[list[Comment]]:
    """推文照順序切成幾批，一批 `config.COMMENTS_PER_BATCH` 則；沒有推文也給一批空的，主文還是要抽。"""
    size = config.COMMENTS_PER_BATCH
    return [comments[i:i + size] for i in range(0, len(comments), size)] or [[]]


def build_messages(article: Article, line: dict, comments: list[Comment], first: bool) -> tuple[str, str]:
    system = (config.PROMPT_DIR / "note.md").read_text().replace(
        config.TIME_SLOTS_MARK, config.TIME_SLOTS_PATH.read_text().strip())
    listing = "\n".join(f"{c.no} {'[原po] ' if c.by_author else ''}{c.user}：{c.content}" for c in comments)
    user = (
        f"標題：{article.title}\n"
        f"發文日期：{article.posted_date}\n\n"
        f"主文：\n<text>\n{article.text}\n</text>\n\n"
        f"<line>\n{json.dumps(line, ensure_ascii=False, indent=1)}\n</line>\n\n"
        f"<comments>\n{listing}\n</comments>\n\n"
        f"<scope>{SCOPE_ALL if first else SCOPE_COMMENTS}</scope>"
    )
    return system, user


async def parse(article: Article, line: dict) -> tuple[dict, int]:
    """line 是第一次解析的輸出，每一批都附同一份。

    回傳（各批合併後、照 `NOTE_SCHEMA` 的輸出，不收入的筆數）。
    重試後還有問題的那幾筆不收入；拿掉之後跟著出問題的（引用到被拿掉的地點）也一起拿掉。
    """
    merged = {"places": [], "place_notes": [], "moves": [], "day_notes": []}
    dropped = 0
    groups = batches(article.comments)
    for i, comments in enumerate(groups):
        label = f"{article.id} 第二次解析（點、段、day_note）第 {i + 1}/{len(groups)} 批"
        log.info("%s：%d 則推文", label, len(comments))
        system, user = build_messages(article, line, comments, first=i == 0)
        out, problems = await feedback.ask(system, user, NOTE_SCHEMA, lambda o: check.check_note(o, line), label=label)
        while problems:
            for p in problems:
                log.info("%s：不收入 %s[%d] %s", label, p.kind, p.index,
                         json.dumps(out[p.kind][p.index], ensure_ascii=False))
            dropped += check.drop(out, problems)
            problems = check.check_note(out, line)
        log.info("%s：%d 個新地點、%d 筆 place_note、%d 筆 move、%d 筆 day_note", label, len(out["places"]),
                 len(out["place_notes"]), len(out["moves"]), len(out["day_notes"]))
        _merge(merged, out, line)
    return merged, dropped


def _merge(merged: dict, out: dict, line: dict) -> None:
    """把一批的輸出併進 merged。

    各批是分開問的，新地點的編號會撞在一起，所以重新編號；跟前面幾批完全相同的新地點共用同一個編號。
    """
    def key(p: dict) -> tuple:
        return p["name_zh"], p["name_ja"], p["city"], p["kind"], p["located"]

    known = {key(p): p["ref"] for p in merged["places"]}
    used = {p["ref"] for p in [*line["places"], *merged["places"]]}
    rename: dict[str, str] = {}
    for p in out["places"]:
        if key(p) in known:
            rename[p["ref"]] = known[key(p)]
            continue
        ref, n = p["ref"], len(used)
        while ref in used:
            n += 1
            ref = f"p{n}"
        used.add(ref)
        rename[p["ref"]] = known[key(p)] = ref
        merged["places"].append({**p, "ref": ref})
    merged["place_notes"] += [{**n, "place": rename.get(n["place"], n["place"])} for n in out["place_notes"]]
    merged["moves"] += [{**m, "from": rename.get(m["from"], m["from"]), "to": rename.get(m["to"], m["to"])}
                        for m in out["moves"]]
    merged["day_notes"] += out["day_notes"]
