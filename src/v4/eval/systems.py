"""四個系統。輸入都一樣：發文日期（執行當天）＋題目和選項；模型、effort 都照 `v4.rag.config`，只差在怎麼找資料。

| 代號 | 怎麼找資料 |
|---|---|
| S0 | 不檢索，只用模型自己的知識（`prompts/closed_book.md`） |
| S1 | 資料庫 100 篇的原文（主文＋推文）切段，向量檢索取 k 段，一次回答（`prompts/rag.md`） |
| S2 | 結構化經驗（格式同工具回傳的），向量檢索取 k 筆，一次回答（`prompts/rag.md`） |
| S3 | ST-RAG（`v4.rag.pipeline` 照原樣） |
| S3-sonnet | 同 S3，只把拆題和工具迴圈的模型換成 Sonnet |
| S0-sonnet、S1-sonnet | 同 S0、S1，只把回答的模型換成 Sonnet |

S1、S2 的「回答」規則照抄 `v4/rag/prompts/agent.md`，只把「寫法同工具回傳的」換成「寫法同段落開頭方括號裡的」；
S2 的資料庫說明也照抄 agent.md。k 跟工具預設回傳的筆數一樣。
"""
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from service import llm
from v4.rag import config as rag_config
from v4.rag import pipeline, tools
from v4.rag.log import Log

EVAL_DIR = Path(__file__).resolve().parent
PROMPT_DIR = EVAL_DIR / "prompts"
RAW_ARTICLE_DIR = rag_config.ROOT / "data" / "ptt_ask" / "itinerary"   # 資料庫 100 篇的原文
TOP_K = rag_config.LIST_LIMIT
CHUNK_CHARS = 500
SONNET = "claude-sonnet-5-5"

S1_CORPUS = "別的旅客在 ptt 寫的發問文和底下的回覆，原文切成段：主文是發問的人寫的，推文是別的旅客的回覆。"
S2_CORPUS = """別的旅客在 ptt 寫的遊記和回覆，整理成三種經驗：
- 地點的經驗：某個地點怎麼樣（人潮、排隊、停留多久、設施、值不值得），附經驗的時間。
- 移動的經驗：從一地到另一地怎麼移動、花多久、路況，附經驗的時間。
- 別人的行程：每一天照順序去了哪些站（含住宿），有些附了對那天或整趟的評語（太趕、太累、順不順）。"""

_TAIPEI = timezone(timedelta(hours=8))


def today() -> str:
    return datetime.now(_TAIPEI).date().isoformat()


def _prompt(name: str, **values: str) -> str:
    text = (PROMPT_DIR / name).read_text()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _write_log(path: Path, user: str, answer: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 輸入\n{user}\n\n# 答案\n{answer}\n")


# ---------- S1、S2 的段落 ----------

@dataclass(frozen=True)
class Passage:
    id: str
    text: str        # 給 LLM 看、也拿去算 embedding 的整段（開頭是 [編號]）


def _pack(pieces: list[str], limit: int) -> list[str]:
    """把一段段文字依序接起來，每塊不超過 limit 字；單段太長的硬切。"""
    out, cur = [], ""
    for p in pieces:
        while len(p) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:limit])
            p = p[limit:]
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def _paragraphs(text: str) -> list[str]:
    paras = [re.sub(r"[ \t　]+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return [re.sub(r"\s*\n\s*", " ", p) for p in paras if p]


def _comments(comments: list[dict]) -> list[str]:
    """同一人連續的推文接成一句（PTT 一則推文常被切成好幾行）。"""
    merged: list[list[str]] = []
    for c in comments:
        user, text = c.get("user", ""), (c.get("content") or "").strip()
        if merged and merged[-1][0] == user:
            merged[-1][1] += text
        else:
            merged.append([user, text])
    return [f"{u}：{t}" for u, t in merged if t]


def raw_chunks(db: Path) -> list[Passage]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        articles = conn.execute("SELECT id, title, posted_at FROM article ORDER BY id").fetchall()
    finally:
        conn.close()
    out = []
    for aid, title, posted in articles:
        d = json.loads((RAW_ARTICLE_DIR / f"ptt_{aid}.json").read_text())
        parts = [("主文", c) for c in _pack(_paragraphs(d.get("text") or ""), CHUNK_CHARS)]
        parts += [("推文", c) for c in _pack(_comments(d.get("comments") or []), CHUNK_CHARS)]
        for k, (kind, body) in enumerate(parts, 1):
            pid = f"chunk {aid}#{k}"
            out.append(Passage(pid, f"[{pid}] 〈{title}〉{posted[:10]} 發文｜{kind}｜{body}"))
    return out


def experience_passages(db: Path) -> list[Passage]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        idx = tools.Index(conn)
    finally:
        conn.close()
    out = [Passage(f"place_note {n.id}", tools.note_line(idx, n, [])) for n in idx.notes]
    out += [Passage(f"move {m.id}", tools.move_line(idx, m, [], False)) for m in idx.moves]
    for d in sorted(idx.days.values(), key=lambda d: tools.id_key(d.id)):
        month = f"{d.month} 月" if d.month else "沒寫月份"
        out.append(Passage(f"day {d.id}",
                           f"[day {d.id}] 〈{idx.title(d.article_id)}〉{idx.posted_year(d.article_id)} 發文｜"
                           f"第 {tools.day_no(d.id)} 天｜{month}｜{tools.day_stop_line(idx, d)}"))
    out += [Passage(f"day_note {x.id}", tools.day_note_line(idx, x, [])) for x in idx.day_notes]
    return out


class _EmbedEnv:
    """`tools.embed` 只用到 `embed`；None 表示用 `service.embedding`（模型照 `v4.rag.config.EMBEDDING_MODEL`）。"""
    embed = None


class Retriever:
    def __init__(self, passages: list[Passage]):
        self.env = _EmbedEnv()
        self.passages = passages
        self.matrix = tools.embed(self.env, [p.text for p in passages])

    def retrieve(self, query: str, k: int = TOP_K) -> list[Passage]:
        scores = self.matrix @ tools.embed(self.env, [tools.query_text(query)])[0]
        order = sorted(range(len(self.passages)), key=lambda i: -float(scores[i]))
        return [self.passages[i] for i in order[:k]]


# ---------- 系統 ----------

class ClosedBook:
    def __init__(self, db: Path, model: str | None = None):
        self.system = _prompt("closed_book.md")
        self.model = model or rag_config.MODEL

    async def answer(self, question: str, log_path: Path) -> str:
        user = f"發文日期：{today()}\n\n<question>{question}</question>"
        text = await llm.call(self.system, user, None, self.model, rag_config.EFFORT)
        _write_log(log_path, user, text)
        return text


class DenseRAG:
    def __init__(self, passages: list[Passage], corpus: str, model: str | None = None):
        self.retriever = Retriever(passages)
        self.model = model or rag_config.MODEL
        self.system = _prompt("rag.md", CORPUS=corpus, K=str(TOP_K),
                              NO_EVIDENCE_OPENING=rag_config.NO_EVIDENCE_OPENING)

    async def answer(self, question: str, log_path: Path) -> str:
        body = "\n".join(p.text for p in self.retriever.retrieve(question))
        user = f"發文日期：{today()}\n\n<question>{question}</question>\n\n<passages>\n{body}\n</passages>"
        text = await llm.call(self.system, user, None, self.model, rag_config.EFFORT)
        _write_log(log_path, user, text)
        return text


def raw_rag(db: Path, model: str | None = None) -> DenseRAG:
    return DenseRAG(raw_chunks(db), S1_CORPUS, model)


def experience_rag(db: Path) -> DenseRAG:
    return DenseRAG(experience_passages(db), S2_CORPUS)


class StRag:
    def __init__(self, db: Path, model: str | None = None):
        self.db = db
        self.model = model

    async def answer(self, question: str, log_path: Path) -> str:
        log = Log(path=log_path, out=io.StringIO())   # 答案只進 log 檔，不印
        text = await pipeline.run(pipeline.from_question(question), self.db, log, question_only=True,
                                  model=self.model)
        # 只拆出一題時，拿掉 pipeline 加的「### 第 1 題：……」標題，跟其他系統一樣只留答案
        if text.startswith("### 第 1 題：") and "\n### 第 2 題：" not in text:
            text = text.split("\n\n", 1)[1] if "\n\n" in text else text
        return text


SYSTEMS = {"S0": ClosedBook, "S1": raw_rag, "S2": experience_rag, "S3": StRag,
           "S0-sonnet": partial(ClosedBook, model=SONNET), "S1-sonnet": partial(raw_rag, model=SONNET),
           "S3-sonnet": partial(StRag, model=SONNET)}
NAMES = {"S0": "不檢索", "S1": "原文 RAG", "S2": "經驗 RAG", "S3": "ST-RAG",
         "S0-sonnet": "不檢索（Sonnet）", "S1-sonnet": "原文 RAG（Sonnet）", "S3-sonnet": "ST-RAG（Sonnet）"}
