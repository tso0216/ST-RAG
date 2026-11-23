"""網頁伺服器：靜態頁、問答事件串。同一時間只跑一題（會寫資料庫、用訂閱額度）。"""
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import stream

STATIC = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]     # 擋 DNS rebinding：別的網站不能借瀏覽器打這個服務


def create_app(db_path: Path) -> Starlette:
    async def index(_: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def ask(request: Request):
        # 只收 JSON：跨站的表單送不出這個 Content-Type，瀏覽器會先擋下來
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return JSONResponse({"error": "要用 application/json"}, status_code=415)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "JSON 格式不對"}, status_code=400)
        text = body.get("question") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "問題是空的"}, status_code=400)
        if app.state.busy:
            return JSONResponse({"error": "上一題還在跑"}, status_code=409)
        app.state.busy = True
        run = stream.Run(text, db_path, lambda: setattr(app.state, "busy", False))
        return StreamingResponse(stream.lines(run), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store"})

    app = Starlette(
        routes=[Route("/", index), Route("/api/ask", ask, methods=["POST"]),
                Mount("/static", StaticFiles(directory=STATIC))],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)],
    )
    app.state.busy = False
    return app
