"""主流程：一篇文章從 JSON 到資料庫，以及略過、停下、填答案後重跑。"""
import asyncio
import json
import sqlite3

import pytest

from service.gsi import GsiError
from service.llm import LLMError
from tests.v4.extract import fakes
from tests.v4.extract.fakes import FakeGsi, build_map
from v4.extract import __main__ as cli
from v4.extract import config, pending, pipeline, store, usage
from v4.extract.local_map import LocalMap

ID = "M.1767013758.A.A9D"


def article_file(folder, n: int = 0, **fields):
    """寫一篇文章 JSON，回傳 (路徑, 文章 id)。n 不同就是不同篇。"""
    article_id = f"M.{1767013758 + n}.A.A9D"
    doc = {
        "title": f"[問題] 東京行程 {n}", "author": "poster", "time": "Mon Dec 29 21:09:16 2025",
        "url": f"https://www.ptt.cc/bbs/Japan_Travel/{article_id}.html",
        "text": "D1 淺草寺 → 晴空塔\n", "comments": [{"tag": "推", "user": "aaa", "content": "淺草寺早上人很少"}],
        **fields,
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"ptt_{article_id}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path, article_id


LINE = fakes.line(
    places=[fakes.line_place("p1", "淺草寺", "浅草寺"), fakes.line_place("p2", "晴空塔", "スカイツリー")],
    days=[fakes.day("D1", ["p1", "p2"], month="2")])
NOTE = fakes.note(
    place_notes=[fakes.place_note("p1", "早上人很少", times=[fakes.time(time="5-10")])],
    moves=[fakes.move("p1", "p2", "走過去 20 分鐘")],
    day_notes=[fakes.day_note([1], "這天很順")])

# 行程裡有解析不到的地點（築地場外市場）和猜不出來的地點（飯店）
HARD_LINE = fakes.line(
    places=[fakes.line_place("p1", "淺草寺", "浅草寺"), fakes.line_place("p2", "築地場外市場", "築地場外市場"),
            fakes.line_place("p3", "飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間")],
    days=[fakes.day("D1", ["p1", "p2", "p3"])])

# 築地場外市場本機地圖沒有，要上網查地址
TSUKIJI_LINE = fakes.line(
    places=[fakes.line_place("p1", "淺草寺", "浅草寺"), fakes.line_place("p2", "築地場外市場", "築地場外市場")],
    days=[fakes.day("D1", ["p1", "p2"])])


@pytest.fixture
def env(tmp_path):
    """回傳 run(sources, **kw)：資料庫、pending、log、本機地圖都放在 tmp_path。"""
    class Env:
        db = tmp_path / "v4.db"
        pending = tmp_path / "v4_pending"
        articles = tmp_path / "articles"
        logs = tmp_path / "logs"
        map = build_map(tmp_path / "japan.duckdb")
        gsi = FakeGsi()

        def run(self, *sources, jobs=1, limit=None):
            local_map = LocalMap(self.map)
            try:
                return asyncio.run(pipeline.run(
                    list(sources or [self.articles]), db_path=self.db, jobs=jobs, limit=limit,
                    local_map=local_map, gsi=self.gsi, pending_dir=self.pending,
                    log_dir=self.logs))
            finally:
                local_map.close()

        def rows(self, sql):
            conn = sqlite3.connect(self.db)
            try:
                return conn.execute(sql).fetchall()
            finally:
                conn.close()

    return Env()


# ---------- 一篇從 JSON 到資料庫 ----------

def test_article_goes_from_json_to_database(env, use_llm):
    article_file(env.articles)
    llm = use_llm(line=LINE, note=NOTE)

    r = env.run()

    assert (r.written, r.skipped, r.dropped, r.error) == ([ID], [], {}, None)
    # 兩個地點一起問：搜尋計畫一次、挑哪一筆一次
    assert [kind for kind, _ in llm.calls] == ["line", "note", "plan", "pick"]
    assert all('<item ref="p1">' in u and '<item ref="p2">' in u for u in (*llm.users("plan"), *llm.users("pick")))
    assert {name: p.tally.calls for name, p in r.parts.items() if p.tally.calls} == {
        usage.LINE: 1, usage.NOTE: 1, usage.PLAN: 1, usage.PICK: 1}
    assert r.parts[usage.LINE].tally.input_tokens == 1000
    assert all(r.parts[name].seconds > 0 for name in (usage.LINE, usage.NOTE, usage.PLAN, usage.PICK, usage.MAP,
                                                      usage.WRITE))
    assert r.parts[usage.WEB].seconds == r.parts[usage.GSI].seconds == 0
    assert env.rows("SELECT id, posted_at FROM article") == [(ID, "2025-12-29T21:09:16")]
    assert env.rows("SELECT s.source, s.source_id, p.name_ja, p.name_zh FROM place p"
                    " JOIN place_source s ON s.place_id = p.id ORDER BY p.id") == [
        ("osm", "way/1", "浅草寺", "淺草寺"), ("osm", "way/2", "東京スカイツリー", "晴空塔")]
    assert env.rows("SELECT month FROM day") == [("2",)]
    assert env.rows("SELECT seq, poi_id FROM day_place ORDER BY seq") == [(1, 1), (2, 2)]
    assert env.rows("SELECT poi_id, poi_name, content FROM place_note") == [(1, "淺草寺", "早上人很少")]
    assert env.rows("SELECT time FROM place_note_time") == [("5-10",)]
    assert env.rows("SELECT from_id, to_id, content FROM move") == [(1, 2, "走過去 20 分鐘")]
    assert env.rows("SELECT content FROM day_note") == [("這天很順",)]
    assert env.rows("SELECT note_id, day_id FROM day_note_day") == [("1767013758-1", "1767013758-1")]
    assert not pending.path_of(env.pending, ID).exists()


def test_folder_takes_only_json_files_and_limit_keeps_the_first_ones(env, use_llm):
    for n in range(3):
        article_file(env.articles, n)
    (env.articles / "days.csv").write_text("id,days\n")
    use_llm(line=LINE, note=NOTE)

    r = env.run(limit=2)

    assert r.written == ["M.1767013758.A.A9D", "M.1767013759.A.A9D"] and r.skipped == []


def test_records_dropped_by_the_check_are_counted(env, use_llm):
    article_file(env.articles)
    bad = fakes.note(place_notes=[fakes.place_note("p1"), fakes.place_note("p9", "指到不存在的地點")])
    use_llm(line=LINE, note=bad)

    r = env.run()

    assert r.written == [ID] and r.dropped == {pipeline.PARSE_DROPPED: 1}
    assert env.rows("SELECT COUNT(*) FROM place_note") == [(1,)]


# ---------- 略過 ----------

def test_articles_without_author_already_in_database_or_repeated_are_skipped(env, use_llm):
    article_file(env.articles)
    use_llm(line=LINE, note=NOTE)
    env.run()
    path, _ = article_file(env.articles)
    deleted, _ = article_file(env.articles, 1, author="")
    fresh, fresh_id = article_file(env.articles, 2)
    llm = use_llm(line=LINE, note=NOTE)

    r = env.run(path, deleted, fresh, fresh)

    assert r.written == [fresh_id]
    assert r.skipped == [(path.name, "資料庫已有"), (deleted.name, "沒有 author"), (fresh.name, "重複")]
    assert len(llm.users("line")) == 1
    assert env.rows("SELECT COUNT(*) FROM article") == [(2,)]


# ---------- 解析不到的地點 ----------

def test_unresolved_places_are_written_without_a_place(env, use_llm):
    article_file(env.articles)
    line = fakes.line(places=[*HARD_LINE["places"], fakes.line_place("p4", "晴空塔", "スカイツリー")],
                      days=[fakes.day("D1", ["p1", "p2", "h3"]), fakes.day("D2", ["p1", "p4"])])
    note = fakes.note(place_notes=[fakes.place_note("p2", "海鮮丼很讚"), fakes.place_note("p1", "早上人很少")],
                      moves=[fakes.move("p1", "p2", "搭地鐵 30 分鐘"), fakes.move("p1", "p4", "走過去 20 分鐘")],
                      day_notes=[fakes.day_note([1], "這天很趕"), fakes.day_note([2], "這天很順")])
    use_llm(line=line, note=note)

    r = env.run()

    assert (r.written, r.error, r.dropped) == ([ID], None, {})
    assert env.rows("SELECT day_id, poi_id IS NULL, poi_name, type FROM day_place ORDER BY day_id, seq") == [
        ("1767013758-1", 0, "淺草寺", "景點"), ("1767013758-1", 1, "築地場外市場", "景點"), ("1767013758-1", 1, "飯店", "住宿"),
        ("1767013758-2", 0, "淺草寺", "景點"), ("1767013758-2", 0, "晴空塔", "景點")]
    assert env.rows("SELECT content FROM day_note ORDER BY id") == [("這天很趕",), ("這天很順",)]
    assert env.rows("SELECT poi_id IS NULL, poi_name, content FROM place_note ORDER BY id") == [
        (1, "築地場外市場", "海鮮丼很讚"), (0, "淺草寺", "早上人很少")]
    assert env.rows("SELECT to_id IS NULL, to_name, content FROM move ORDER BY id") == [
        (1, "築地場外市場", "搭地鐵 30 分鐘"), (0, "晴空塔", "走過去 20 分鐘")]
    assert not pending.path_of(env.pending, ID).exists()
    assert [(t.place.name_zh, t.result, t.fail_reason) for t in r.geocode_traces if t.result == "解析不到"] == [
        ("築地場外市場", "解析不到", "上網查不到地址"), ("飯店", "解析不到", "猜不出來")]


# ---------- 整批停下 ----------

def test_llm_failure_stops_the_batch_and_keeps_what_was_written(env, use_llm):
    ids = [article_file(env.articles, n)[1] for n in range(3)]

    def line(user):
        if "東京行程 1" in user:
            raise LLMError("額度用完")
        return LINE
    use_llm(line=line, note=NOTE)

    r = env.run()

    assert r.written == [ids[0]]
    assert ids[1] in r.error and "額度用完" in r.error
    assert r.unfinished == ids[1:]
    assert env.rows("SELECT id FROM article") == [(ids[0],)]


def test_itinerary_that_still_fails_the_check_after_retries_stops_the_batch(env, use_llm):
    article_file(env.articles)
    _, second = article_file(env.articles, 1)
    bad = fakes.line(places=[fakes.line_place("p1", "淺草寺", "浅草寺")], days=[fakes.day("D1", ["p1", "p7"])])
    llm = use_llm(line=bad, note=NOTE)

    r = env.run()

    assert r.written == [] and ID in r.error and "p7" in r.error
    assert r.unfinished == [ID, second]
    assert len([u for u in llm.users("line") if "東京行程 0" in u]) == 1 + config.RETRIES
    assert not env.db.exists() or env.rows("SELECT COUNT(*) FROM article") == [(0,)]


def test_gsi_failure_stops_the_batch_and_the_parse_result_is_kept_for_the_rerun(env, use_llm):
    article_file(env.articles)
    use_llm(line=TSUKIJI_LINE, note=fakes.note(), web=fakes.web("東京都中央区築地4-16-2"))
    env.gsi = FakeGsi(fail=GsiError("國土地理院連線失敗"))

    r = env.run()

    assert r.written == [] and "國土地理院連線失敗" in r.error and r.unfinished == [ID]
    assert pending.path_of(env.pending, ID).exists()
    # 已經寫的還在，最後一個是 中斷
    traces = r.geocode_traces
    assert [(t.place.name_zh, t.result) for t in traces] == [("淺草寺", "地圖-第1輪"), ("築地場外市場", "中斷")]
    assert "國土地理院連線失敗" in traces[-1].fail_reason
    assert "築地場外市場｜築地場外市場｜東京｜景點｜是　→　中斷（GsiError: 國土地理院連線失敗）" in \
        (r.log_dir / f"{ID}.log").read_text(encoding="utf-8")

    env.gsi = FakeGsi()
    llm = use_llm(web=fakes.web("東京都中央区築地4-16-2"))
    r = env.run()
    assert r.written == [ID] and llm.users("line") == []
    assert [t.result for t in r.geocode_traces] == ["資料庫", "上網"]


def test_llm_failure_while_resolving_a_place_is_logged_as_stopped(env, use_llm):
    article_file(env.articles)

    def pick(user):
        raise LLMError("額度用完")
    use_llm(line=LINE, note=NOTE, pick=pick)

    r = env.run()

    assert "額度用完" in r.error
    # 兩個地點同一批，一起中斷；各參與了搜尋計畫 1 次、挑 1 次
    assert [(t.place.name_zh, t.result, t.llm_calls) for t in r.geocode_traces] == [
        ("淺草寺", "中斷", 2), ("晴空塔", "中斷", 2)]
    assert all("額度用完" in t.fail_reason for t in r.geocode_traces)


# ---------- 並行 ----------

@pytest.mark.parametrize("jobs", [1, 3])
def test_jobs_limits_how_many_articles_are_parsed_at_once(env, use_llm, monkeypatch, jobs):
    ids = [article_file(env.articles, n)[1] for n in range(5)]
    running, peak = 0, 0

    geocode = fakes.FakeLLM()

    async def slow(system, user, schema, model, effort=None, tools=None):
        nonlocal running, peak
        if schema is fakes.LINE_SCHEMA:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            return json.loads(json.dumps(LINE))
        if schema is fakes.NOTE_SCHEMA:
            await asyncio.sleep(0.01)
            running -= 1
            return json.loads(json.dumps(NOTE))
        return await geocode(system, user, schema, model, effort, tools)
    monkeypatch.setattr("service.llm.call", slow)

    r = env.run(jobs=jobs)

    assert peak == jobs
    assert r.written == ids   # 寫入照輸入的順序


# ---------- 命令列 ----------

@pytest.fixture
def cli_paths(tmp_path, monkeypatch):
    """命令列用的是 `config` 的預設路徑（conftest 已經換到 tmp_path）；本機地圖用小型的，國土地理院換成假的。"""
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "v4_pending")
    monkeypatch.setattr(config, "LOCAL_MAP_PATH", build_map(tmp_path / "japan.duckdb"))
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pipeline, "GsiClient", FakeGsi)
    return tmp_path


def cli_run(cli_paths, *extra) -> int:
    return cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db"), *extra])


def log_dir(cli_paths):
    [folder] = (cli_paths / "logs").glob("extract_*")
    return folder


def run_log(cli_paths) -> str:
    """整批的 `_run.log`：進度和總結。"""
    return (log_dir(cli_paths) / "_run.log").read_text(encoding="utf-8")


def article_log(cli_paths, article_id=ID) -> str:
    """一篇的 log 檔：這篇的進度、每個地點、--verbose 的過程。"""
    return (log_dir(cli_paths) / f"{article_id}.log").read_text(encoding="utf-8")


def test_command_line_prints_only_a_brief_summary(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=LINE, note=NOTE)

    code = cli_run(cli_paths)

    assert code == 0
    folder = log_dir(cli_paths)
    assert capsys.readouterr().out.splitlines() == [
        "共 1 篇",
        f"log：{folder}/",
        f"[1/1] {ID} [問題] 東京行程 0：地點 2、天 1",
        "",
        "寫入 1 篇、略過 0 篇",
        "地點 2 個，解析不到 0 個",
        "llm 4 次，約 US$0.04",
        "花費時間：0 秒",
        f"log：{folder}/",
    ]


def test_log_folder_has_the_progress_the_full_summary_and_one_file_per_article(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=LINE, note=NOTE)

    cli_run(cli_paths)

    text = run_log(cli_paths)
    tag = f"[1/1] {ID}"
    steps = [f"{tag} llm 解析：[問題] 東京行程 0，推文 1 則",
             f"{tag} llm 解析完成：地點 2、天 1，llm 2 次，0 秒",
             f"{tag} 地點解析：2 個"]
    progress = [line for line in text.splitlines() if line.startswith((tag, "[p", "共 "))]
    assert progress == ["共 1 篇", *steps, f"{tag} 寫入：地點解析 llm 2 次，0 秒"]
    in_article = [line for line in article_log(cli_paths).splitlines() if line.startswith((tag, "[p", "="))]
    assert in_article == [
        f"========== {ID} ==========", *steps,
        "[p1] 淺草寺｜浅草寺｜東京｜景點｜是　→　地圖-第1輪：浅草寺",
        "[p2] 晴空塔｜スカイツリー｜東京｜景點｜是　→　地圖-第1輪：東京スカイツリー",
        f"{tag} 寫入：地點解析 llm 2 次，0 秒",
    ]
    summary = text.split("========== 總結 ==========\n\n")[1]
    assert summary.startswith("寫入：1 篇\n略過：0 篇\n拿掉沒寫入：0 筆\n\n地點解析：共 2 個\n"
                              "  資料庫 0、地圖-第1輪 2、地圖-第2輪 0、上網 0、解析不到 0\n")
    assert "解析不到的地點" not in summary
    assert "llm 用量：共 4 次，輸入 4,000 token（其中快取讀取 2,400）、輸出 200 token" in summary
    assert "  約 US$0.04" in summary
    assert f"  {usage.LINE}：llm 1 次，token 1,050（25%），US$0.01，時間 0 秒（" in summary
    assert f"  {usage.WEB}：llm 0 次，token 0（0%），US$0.00，時間 0 秒（0%）" in summary
    assert f"  {usage.MAP}：時間 0 秒（" in summary
    assert summary.endswith("花費時間：0 秒\n")


def test_each_article_reports_its_own_llm_calls(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    article_file(cli_paths / "articles", 1)
    use_llm(line=LINE, note=NOTE)

    cli_run(cli_paths)

    done = [line for line in run_log(cli_paths).splitlines() if "llm 解析完成" in line]
    assert len(done) == 2 and all("，llm 2 次，" in line for line in done)


def test_command_line_prints_the_tokens_spent_when_interrupted(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")

    def interrupt(user):
        raise KeyboardInterrupt
    use_llm(line=LINE, note=NOTE, plan=interrupt)

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    out = capsys.readouterr()
    assert code == 130
    assert "中斷" in out.err
    assert "llm 用量：共 3 次" in out.out   # line、note，加上被中斷的那次搜尋計畫


def test_command_line_lists_the_unresolved_places(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=HARD_LINE, note=fakes.note())

    code = cli_run(cli_paths)

    out = capsys.readouterr().out
    assert code == 0
    assert f"[1/1] {ID} [問題] 東京行程 0：地點 3、天 1，解析不到 2" in out
    assert "地點 3 個，解析不到 2 個" in out
    assert "築地場外市場" not in out   # 明細只在 log 檔
    text = run_log(cli_paths)
    assert f"[1/1] {ID} 寫入：地點解析 llm 4 次，0 秒，解析不到 2" in text
    assert "築地場外市場｜築地場外市場｜東京｜景點｜是　→　解析不到（上網查不到地址）" in article_log(cli_paths)
    assert "  資料庫 0、地圖-第1輪 1、地圖-第2輪 0、上網 0、解析不到 2" in text
    assert "  解析不到的原因：上網查不到地址 1、國土地理院查不到這個地址 0、猜不出來 1" in text
    assert f"  {ID} 築地場外市場（築地場外市場）：上網查不到地址" in text
    assert f"  {ID} 飯店（ホテル）：猜不出來" in text
    # 築地場外市場：第 1 輪沒有候選，llm 沒給新名稱（假的回空字串），第 2 輪名稱重複
    assert "  本機地圖每輪失敗：沒有候選 1、AI 都不選 0、名稱重複 1" in text


def test_command_line_stops_when_the_local_map_is_missing(cli_paths, monkeypatch, use_llm, capsys):
    article_file(cli_paths / "articles")
    monkeypatch.setattr(config, "LOCAL_MAP_PATH", cli_paths / "沒有.duckdb")
    llm = use_llm(line=LINE, note=NOTE)

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    assert code == 1
    assert "請先跑 scripts/geo/build_japan.py" in capsys.readouterr().err
    assert llm.calls == [] and not (cli_paths / "v4.db").exists()


def test_command_line_exits_non_zero_when_the_batch_stops(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")

    def fail(user):
        raise LLMError("額度用完")
    use_llm(line=fail)

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    assert code == 1
    assert "額度用完" in capsys.readouterr().err


def test_verbose_writes_the_steps_and_llm_calls_into_the_log_file(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    bad = fakes.line(places=[fakes.line_place("p1", "淺草寺", "浅草寺")], days=[fakes.day("D1", ["p1", "p7"])])
    use_llm(line=[bad, LINE], note=NOTE)

    cli_run(cli_paths, "--verbose")

    text = article_log(cli_paths)
    assert f"{ID} 第一次解析（線）：1 個問題，重試 1/2" in text
    assert "days[0]：stops 的 p7 不在 places 裡" in text
    assert "淺草寺 第 1 輪查本機地圖「浅草寺」東京都：2 筆" in text and "淺草寺 挑第 1 筆" in text
    assert "淺草寺 的搜尋計畫：浅草寺｜{'pref': '東京'}" in text
    assert "━━━━━ llm #" in text
    out = capsys.readouterr()
    assert "個問題" not in out.out + out.err and "━━━━━" not in out.out + out.err   # 終端機不印


def test_without_verbose_the_log_file_has_no_llm_calls(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=LINE, note=NOTE)

    cli_run(cli_paths)

    text = run_log(cli_paths) + article_log(cli_paths)
    assert "━━━━━" not in text and "的搜尋計畫" not in text
    assert "[p1] 淺草寺｜浅草寺｜東京｜景點｜是　→　地圖-第1輪：浅草寺" in text
