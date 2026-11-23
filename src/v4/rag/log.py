"""紀錄（spec 第 7 章）：終端機的輸出和 `--log` 的純文字檔。

- 終端機：預設每題跑完印「### 第 N 題：問句」和答案；`--verbose` 另外邊跑邊印步驟（`⏺` 判斷或工具呼叫、`⎿` 回傳的第一行）。
- log 檔：給了路徑才寫，每次寫入就開檔、寫完關掉，中途出錯時已經跑完的部分會留著。只寫資料，不寫敘述句。
"""
import json
import sys
from pathlib import Path
from typing import TextIO

from v4.extract.data_schema import Article

LINE_WIDTH = 160    # --verbose 時工具呼叫只寫一行，太長就截斷


def usage_line(input_tokens: int, output_tokens: int, cost: float, seconds: float) -> str:
    return f"用量：輸入 {input_tokens:,}｜輸出 {output_tokens:,}｜${cost:.2f}｜{seconds:.1f} 秒"


def one_line(text: str, width: int = LINE_WIDTH) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def args_json(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)


class Log:
    def __init__(self, verbose: bool = False, path: Path | None = None, out: TextIO | None = None):
        self.verbose = verbose
        self.path = Path(path) if path else None
        self.out = out or sys.stdout
        self._turn = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")

    # ---------- 底層 ----------

    def _file(self, *lines: str) -> None:
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text, file=self.out, flush=True)

    # ---------- 各段落 ----------

    def attach(self, env) -> None:
        """行程解析完、開始答題前呼叫。這裡用不到；網頁版（`v4.web`）靠它拿到 `Env` 查座標。"""

    def input(self, article: Article, question_only: bool) -> None:
        if question_only:
            self._file("# 輸入", f"問題：{article.text}", "")
        else:
            self._file("# 輸入", f"{article.id}｜{article.posted_date}｜{article.title}", f"主文：{article.text}", "")

    def split(self, questions: list[str], retries: list[str], usage: str) -> None:
        self._file("# 拆題", *(f"{i}. {q}" for i, q in enumerate(questions, 1)),
                   *(f"重問 {i} 次：{r}" for i, r in enumerate(retries, 1)), usage, "")
        self._say(f"拆題：{len(questions)} 題")

    def no_question(self, text: str) -> None:
        """一句話裡沒有在問事情：不解析行程、不跑迴圈，直接回這段文字。"""
        self._file("# 回覆", text, "")
        print(f"{text}\n", file=self.out, flush=True)

    def itinerary(self, place_lines: list[str], day_lines: list[str], usage: str, summary: str) -> None:
        self._file("# 行程", "｜".join(place_lines) or "（沒有地點）",
                   *(day_lines or ["沒有按天排的行程"]), usage, "")
        self._say(summary)

    def question(self, n: int, text: str) -> None:
        self._turn = 0
        self._file(f"# 第 {n} 題", text)
        self._say(f"\n### 第 {n} 題：{text}")

    def _turn_header(self, turn: int) -> None:
        if turn != self._turn:
            self._turn = turn
            self._file(f"## 回合 {turn}")

    def judgment(self, turn: int, text: str) -> None:
        self._turn_header(turn)
        self._file(text.strip())
        self._say(f"⏺ {one_line(text)}")

    def tool(self, turn: int, no: int, name: str, args: dict, result: str, is_error: bool) -> None:
        self._turn_header(turn)
        self._file(f"#{no} {name} {args_json(args)}", ("工具錯誤：" if is_error else "") + result)
        self._say(f"⏺ {one_line(f'{name} {args_json(args)}')}")
        first = result.strip().splitlines()[0] if result.strip() else ""
        self._say(f"  ⎿ {one_line(('工具錯誤：' if is_error else '') + first)}")

    def answer(self, n: int, question: str, answer: str, stop: str, calls: int, turns: int, usage: str) -> None:
        self._file("## 答案", answer.strip(), f"停：{stop}｜工具 {calls} 次｜回合 {turns}｜{usage}", "")
        if self.verbose:
            print(f"\n{answer.strip()}\n", file=self.out, flush=True)
        else:
            print(f"### 第 {n} 題：{question}\n\n{answer.strip()}\n", file=self.out, flush=True)

    def error(self, text: str) -> None:
        self._file("# 錯誤", text, "")
