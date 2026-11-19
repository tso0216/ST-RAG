"""地點解析的 log 檔：每個地點一行 JSON，記下它在哪一步找到、錯在哪裡；跑完算統計。

- 檔案：`data/logs/geocode_<開始時間 YYYYmmdd-HHMMSS>.jsonl`，每次執行一個檔，資料夾不存在就建。
- 一行一個地點（UTF-8，中文不跳脫），每寫完一行就 flush，中途整批停下已經寫的也會留著。
- `Trace` 是一個地點正在記的內容，`geocoding` 一邊解析一邊填，最後交給 `GeocodeLog.write`。
- `summarize`、`summary_lines` 從 log 檔的內容算統計，給 `__main__.py` 印。
"""
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .data_schema import Context, PlaceRef

# `result` 的值
DATABASE = "資料庫"
ANSWER = "答案"
WEB = "上網"
WAITING = "等你決定"
EXCLUDED = "不收"
STOPPED = "中斷"


def map_result(round_: int) -> str:
    """本機地圖在第幾輪找到：`地圖-第1輪`……"""
    return f"地圖-第{round_}輪"


# `rounds[].outcome` 的值
PICKED = "挑中"
NO_CANDIDATES = "沒有候選"
NONE_PICKED = "AI 都不選"
REPEATED = "名稱重複"

# `fail_reason`：上網查地址失敗的兩種
WEB_NOT_FOUND = "上網查不到地址"
GSI_NOT_FOUND = "國土地理院查不到這個地址"


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


@dataclass
class Trace:
    """一個地點的 log，解析時一邊填。"""
    place: PlaceRef
    ctx: Context
    result: str | None = None
    fail_reason: str | None = None
    rounds: list[dict] = field(default_factory=list)
    web: dict | None = None
    used: dict | None = None        # 最後用的地點（place id、來源、名稱、座標……）
    llm_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def record(self) -> dict:
        p, ctx = self.place, self.ctx
        return {
            "article_id": ctx.article_id,
            "ref": p.ref,
            "name_zh": p.name_zh,
            "name_ja": p.name_ja,
            "city": p.city,
            "kind": p.kind,
            "located": p.located,
            "sentence": p.sentence,
            "same_day": [{"name": n.name, "lat": n.lat, "lon": n.lon} for n in ctx.same_day],
            "result": self.result,
            "fail_reason": self.fail_reason if self.result in (WAITING, STOPPED) else None,
            "rounds": self.rounds,
            "web": self.web,
            "place": self.used,
            "llm_calls": self.llm_calls,
            "seconds": round(time.monotonic() - self.started, 3),
        }


class GeocodeLog:
    """一次執行的 log 檔。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    @classmethod
    def start(cls, folder: Path, started: datetime | None = None) -> "GeocodeLog":
        """開這次執行的 log 檔。同一秒已經有一個（例如連跑兩次）就在檔名後面加 `-2`、`-3`，不混在一起。"""
        stem = f"geocode_{(started or datetime.now()):%Y%m%d-%H%M%S}"
        path, n = Path(folder) / f"{stem}.jsonl", 1
        while path.exists():
            n += 1
            path = Path(folder) / f"{stem}-{n}.jsonl"
        return cls(path)

    def write(self, record: dict) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def read(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass(frozen=True)
class Summary:
    total: int
    results: Counter          # result → 個數
    fail_reasons: Counter     # 等你決定的原因 → 個數
    round_failures: Counter   # 本機地圖每一輪失敗的 outcome → 輪數


def summarize(path: Path) -> Summary:
    """從 log 檔的內容算統計。"""
    records = read(path)
    return Summary(
        total=len(records),
        results=Counter(r["result"] for r in records),
        fail_reasons=Counter(r["fail_reason"] for r in records if r["result"] == WAITING and r["fail_reason"]),
        round_failures=Counter(x["outcome"] for r in records for x in r["rounds"] if x["outcome"] != PICKED),
    )


_RESULT_ORDER = (DATABASE, ANSWER, *(map_result(i) for i in (1, 2, 3)), WEB, WAITING, EXCLUDED)


def summary_lines(path: Path, shown_path: str | None = None) -> list[str]:
    """`__main__.py` 結束時印的那一段。固定的項目沒有也印 0，其他有出現的接在後面。"""
    s = summarize(path)

    def join(counter: Counter, fixed: tuple[str, ...]) -> str:
        keys = [*fixed, *(k for k in counter if k not in fixed)]
        return "、".join(f"{k} {counter[k]}" for k in keys)

    return [
        f"地點解析：共 {s.total} 個（log：{shown_path or path}）",
        "  " + join(s.results, _RESULT_ORDER),
        "  等你決定的原因：" + join(s.fail_reasons, (WEB_NOT_FOUND, GSI_NOT_FOUND)),
        "  本機地圖每一輪的失敗：" + join(s.round_failures, (NO_CANDIDATES, NONE_PICKED)),
    ]
