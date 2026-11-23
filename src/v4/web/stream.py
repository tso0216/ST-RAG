"""把 `pipeline.run` 的進度變成一行一個 JSON 的事件串，每個工具呼叫附上算好的地圖場景。

回答在獨立的執行緒和事件迴圈裡跑：工具是同步的（embedding、地點解析可能要好幾秒），放在伺服器的迴圈會卡住串流和停止鍵。
資料庫連線由 `pipeline.run` 在那個執行緒裡開，`StreamLog` 的方法也都在那裡執行。
"""
import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from v4.rag import pipeline
from v4.rag.log import Log

from . import visual


class StreamLog(Log):
    def __init__(self, push: Callable[[dict], None]) -> None:
        super().__init__()
        self.push = push
        self.env = None
        self.n = 0

    def emit(self, kind: str, **data) -> None:
        self.push({"type": kind, **data})

    def _scene(self, build, *args) -> dict | None:
        """畫圖出錯不能中斷回答：這個方法在 SDK 的訊息迴圈裡執行，例外會讓整題停掉。"""
        try:
            return build(self.env, *args)
        except Exception as e:
            self.emit("scene_error", text=f"{type(e).__name__}: {e}")
            return None

    def attach(self, env) -> None:
        self.env = env

    def split(self, questions: list[str], retries: list[str], usage: str) -> None:
        self.emit("split", questions=questions)

    def no_question(self, text: str) -> None:
        self.emit("answer", n=0, text=text, scene=None)

    def itinerary(self, place_lines: list[str], day_lines: list[str], usage: str, summary: str) -> None:
        self.emit("itinerary", summary=summary, days=day_lines, scene=self._scene(visual.itinerary_scene))

    def question(self, n: int, text: str) -> None:
        self.n = n
        self.emit("question", n=n, text=text)

    def judgment(self, turn: int, text: str) -> None:
        self.emit("judgment", n=self.n, text=text.strip())

    def tool(self, turn: int, no: int, name: str, args: dict, result: str, is_error: bool) -> None:
        self.emit("tool", n=self.n, no=no, name=name, args=args, result=result, is_error=is_error,
                  scene=self._scene(visual.tool_scene, name, args, result, is_error))

    def answer(self, n: int, question: str, answer: str, stop: str, calls: int, turns: int, usage: str) -> None:
        self.emit("answer", n=n, text=answer.strip(), scene=self._scene(visual.answer_scene, answer))

    def error(self, text: str) -> None:
        self.emit("error", text=text)


class Run:
    """一次回答。要在伺服器的事件迴圈裡建立；`on_finish` 也會回到那個迴圈執行。"""

    def __init__(self, text: str, db_path: Path, on_finish: Callable[[], None]) -> None:
        self.text, self.db_path, self.on_finish = text, db_path, on_finish
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.server = asyncio.get_running_loop()
        self.log = StreamLog(lambda event: self.server.call_soon_threadsafe(self.events.put_nowait, event))
        self.worker: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.cancelled = False
        threading.Thread(target=self._main, name="v4-web-run", daemon=True).start()

    def _main(self) -> None:
        try:
            asyncio.run(self._work())
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self.server.call_soon_threadsafe(self.on_finish)
            except RuntimeError:        # 伺服器已經關了
                pass

    async def _work(self) -> None:
        self.worker, self.task = asyncio.get_running_loop(), asyncio.current_task()
        if self.cancelled:
            return
        try:
            await pipeline.run(pipeline.from_question(self.text), self.db_path, self.log, question_only=True)
        except Exception as e:
            self.log.error(f"{type(e).__name__}: {e}")
        self.log.emit("done")

    def cancel(self) -> None:
        self.cancelled = True
        if self.worker is not None and self.task is not None:
            try:
                self.worker.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:        # 已經跑完
                pass


async def lines(run: Run) -> AsyncIterator[bytes]:
    """連線斷掉（使用者按停止）時取消這次回答。"""
    try:
        while True:
            event = await run.events.get()
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode()
            if event["type"] == "done":
                return
    finally:
        run.cancel()
