"""這次執行的 log（給人看的純文字）和地點解析的統計。終端機只印少量資訊，詳細的都寫在這裡。

- 每次執行一個資料夾：`data/logs/extract_<開始時間 YYYYmmdd-HHMMSS>/`，不存在就建。裡面有
  - `_run.log`（`RUN_LOG`）：整批的。要做幾篇、每篇的進度（`progress`：llm 解析、地點解析、寫入各一行）、
    結束時的總結（`__main__.py` 接在最後面）。
  - `<文章 id>.log`：一篇一個。這篇的進度、每個地點一段（`write`：查了什麼、在哪一步找到、錯在哪裡）；
    一起問 llm 的一批地點先寫一行「一批 N 個：…」（`batch`）。
- `--verbose` 時另外寫 logging 的訊息（`handler`）：每次 llm 的輸入輸出、檢查出的問題和重試、每一輪的搜尋計畫和挑選。
  寫進哪個檔看現在在做哪一篇（`article()` 包住的範圍，asyncio 的每個 task 各自記），不在任何一篇裡的寫進 `_run.log`。
  好幾篇同時進行（下一篇的 llm 解析在背景跑）也不會混在一起。
- 每寫一次就開檔、寫完關掉，中途整批停下已經寫的也會留著。
- `Trace` 是一個地點正在記的內容，`geocoding` 一邊解析一邊填，做完交給 `GeocodeLog.write`。
- `GeocodeLog.traces` 留著寫過的每個地點，`summary_lines`、`dropped_lines` 用它算統計給 `__main__.py` 印。
"""
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .data_schema import Context, PlaceRef

# `result` 的值
DATABASE = "資料庫"
WEB = "上網"
DROPPED = "解析不到"   # 沒有 place，照樣寫入（place id 留空）
STOPPED = "中斷"


def map_result(round_: int) -> str:
    """本機地圖在第幾輪找到：`地圖-第1輪`……"""
    return f"地圖-第{round_}輪"


# `rounds[].outcome` 的值
PICKED = "挑中"
NO_CANDIDATES = "沒有候選"
NONE_PICKED = "AI 都不選"
REPEATED = "名稱重複"
AREA_INVALID = "範圍不能用"

# `fail_reason`：解析不到的原因
WEB_NOT_FOUND = "上網查不到地址"
GSI_NOT_FOUND = "國土地理院查不到這個地址"
NO_GUESS = "猜不出來"


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


@dataclass
class Trace:
    """一個地點的 log，解析時一邊填。

    rounds 一輪一個 dict：`round`、`names`、`area`（llm 給的範圍）、`plan_reason`、`area_error`、
    `area_used`（實際查的範圍）、`widened`、`candidates`（一筆一行的文字）、`choice`、`pick_reason`、`outcome`。
    llm_calls 是這個地點參與的 llm 呼叫次數（同一批的地點一起問，各算一次）。
    """
    place: PlaceRef
    ctx: Context
    result: str | None = None
    fail_reason: str | None = None
    rounds: list[dict] = field(default_factory=list)
    web: dict | None = None
    used: dict | None = None        # 最後用的地點（place id、來源、名稱、座標……）
    llm_calls: int = 0


RUN_LOG = "_run.log"

_article: ContextVar[str | None] = ContextVar("log_article", default=None)   # 現在在做哪一篇


class GeocodeLog:
    """一次執行的 log 資料夾。`path` 是整批的 `_run.log`。"""

    PREFIX = "extract"

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / RUN_LOG
        self.path.touch()
        self.traces: list[Trace] = []

    @classmethod
    def start(cls, folder: Path, started: datetime | None = None) -> "GeocodeLog":
        """開這次執行的資料夾。同一秒已經有一個（例如連跑兩次）就在名稱後面加 `-2`、`-3`，不混在一起。"""
        stem = f"{cls.PREFIX}_{(started or datetime.now()):%Y%m%d-%H%M%S}"
        path, n = Path(folder) / stem, 1
        while path.exists():
            n += 1
            path = Path(folder) / f"{stem}-{n}"
        return cls(path)

    def article_path(self, article_id: str) -> Path:
        return self.folder / f"{article_id}.log"

    @contextmanager
    def article(self, article_id: str) -> Iterator[None]:
        """這段裡 logging 的訊息和 `line` 寫進這一篇的檔。"""
        token = _article.set(article_id)
        try:
            yield
        finally:
            _article.reset(token)

    def line(self, text: str) -> None:
        """寫一行：在 `article()` 裡寫進那一篇的檔，不然寫進 `_run.log`。"""
        self._put(_article.get(), text + "\n")

    def run_line(self, text: str) -> None:
        """寫一行進 `_run.log`。"""
        self._put(None, text + "\n")

    def progress(self, article_id: str, text: str) -> None:
        """一篇的進度：`_run.log` 和那一篇的檔都寫。"""
        self._put(None, text + "\n")
        self._put(article_id, text + "\n")

    def handler(self) -> logging.Handler:
        """把 logging 的訊息寫進 log（`--verbose` 時 logger `v4` 才會放 INFO 進來）。"""
        return _FileHandler(self)

    def batch(self, article_id: str, places: list[PlaceRef]) -> None:
        """一批一起問 llm 的地點開始：`一批 3 個：淺草寺、晴空塔、燒肉 KING（是）`。"""
        located = "、".join(dict.fromkeys(p.located for p in places))
        self._put(article_id, f"\n一批 {len(places)} 個：{'、'.join(p.name_zh for p in places)}（{located}）\n")

    def write(self, trace: Trace) -> None:
        self.traces.append(trace)
        self._put(trace.ctx.article_id, render(trace) + "\n")

    def _put(self, article_id: str | None, text: str) -> None:
        path = self.path if article_id is None else self.article_path(article_id)
        if article_id is not None and not path.exists():
            text = f"========== {article_id} ==========\n" + text
        with path.open("a", encoding="utf-8") as f:
            f.write(text)


class _FileHandler(logging.Handler):
    def __init__(self, log_file: GeocodeLog):
        super().__init__()
        self.log_file = log_file
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_file.line(self.format(record))
        except Exception:
            self.handleError(record)


def render(t: Trace) -> str:
    """一個地點在 log 檔裡的那一段。"""
    p = t.place
    lines = [f"[{p.ref}] {p.name_zh}｜{p.name_ja}｜{p.city or '城市不明'}｜{p.kind}｜{p.located}　→　{_outcome(t)}",
             f"  句子：{p.sentence}"]
    if p.reason:
        lines.append(f"  依據：{p.reason}")
    same_day = "、".join(f"{n.name}（{n.lat:.4f}, {n.lon:.4f}）" if n.lat is not None else f"{n.name}（還沒定位）"
                         for n in t.ctx.same_day)
    if same_day:
        lines.append(f"  同一天：{same_day}")
    for r in t.rounds:
        lines.append(f"  第 {r['round']} 輪：名稱 {'、'.join(r['names']) or '（沒給）'}｜範圍 {r['area']}"
                     f"｜{r['outcome'] or '（還沒做完）'}")
        if r["plan_reason"]:
            lines.append(f"    理由：{r['plan_reason']}")
        if r["area_error"]:
            lines.append(f"    範圍不能用：{r['area_error']}")
        if r["area_used"]:
            lines.append(f"    實際查：{r['area_used']}" + ("（放寬）" if r["widened"] else "")
                         + f"，{len(r['candidates'])} 筆候選")
        for no, text in enumerate(r["candidates"], 1):
            lines.append(f"    {'→' if no == r['choice'] else ' '} {no}. {text}")
        if r["choice"] is not None:
            lines.append(f"    AI {'挑第 ' + str(r['choice']) + ' 筆' if r['choice'] else '都不選'}：{r['pick_reason']}")
    if t.web is not None:
        w = t.web
        lines.append(f"  上網查地址：{w['address']}（{w['source_url']}）" if w["found"] else "  上網查地址：查不到")
        if w["gsi_title"]:
            lines.append(f"  國土地理院：{w['gsi_title']}（{w['lat']:.4f}, {w['lon']:.4f}）")
    if t.used:
        u = t.used
        lines.append(f"  用：place {u['id']} {u['name']}（{u['source']} {u['source_id']}）{u['google_maps']}")
    lines.append(f"  llm：{t.llm_calls} 次")
    return "\n".join(lines) + "\n"


def _outcome(t: Trace) -> str:
    if t.result in (DROPPED, STOPPED):
        return f"{t.result}（{t.fail_reason}）"
    if t.used:
        return f"{t.result}：{t.used['name']}"
    return t.result or "（還沒做完）"


@dataclass(frozen=True)
class Summary:
    total: int
    results: Counter          # result → 個數
    fail_reasons: Counter     # 解析不到的原因 → 個數
    round_failures: Counter   # 本機地圖每一輪失敗的 outcome → 輪數


def summarize(traces: list[Trace]) -> Summary:
    return Summary(
        total=len(traces),
        results=Counter(t.result for t in traces),
        fail_reasons=Counter(t.fail_reason for t in traces if t.result == DROPPED and t.fail_reason),
        round_failures=Counter(r["outcome"] for t in traces for r in t.rounds
                               if r["outcome"] and r["outcome"] != PICKED),
    )


def summary_lines(traces: list[Trace]) -> list[str]:
    """總結裡地點解析的那一段。固定的項目沒有也印 0，其他有出現的接在後面。"""
    s = summarize(traces)

    def join(counter: Counter, fixed: tuple[str, ...]) -> str:
        keys = [*fixed, *(k for k in counter if k not in fixed)]
        return "、".join(f"{k} {counter[k]}" for k in keys)

    return [
        f"地點解析：共 {s.total} 個",
        "  " + join(s.results, (DATABASE, *(map_result(i) for i in range(1, config.GEOCODE_ROUNDS + 1)), WEB, DROPPED)),
        "  解析不到的原因：" + join(s.fail_reasons, (WEB_NOT_FOUND, GSI_NOT_FOUND, NO_GUESS)),
        "  本機地圖每輪失敗：" + join(s.round_failures, (NO_CANDIDATES, NONE_PICKED)),
    ]


def dropped_lines(traces: list[Trace]) -> list[str]:
    """解析不到的地點，一個一行：`  M.xxx 築地場外市場（築地場外市場）：上網查不到地址`；沒有就是空的。"""
    gone = [t for t in traces if t.result == DROPPED]
    if not gone:
        return []
    return [f"解析不到的地點（{len(gone)} 個）：", *(
        f"  {t.ctx.article_id} {t.place.name_zh}（{t.place.name_ja}）：{t.fail_reason}" for t in gone)]
