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
from v4.extract import ask_place, config, geocode_log, pending, pipeline, store
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
    places=[fakes.place("p1", "淺草寺", "浅草寺"), fakes.place("p2", "晴空塔", "スカイツリー")],
    days=[fakes.day("D1", ["p1", "p2"], month="2")])
NOTE = fakes.note(
    place_notes=[fakes.place_note("p1", "早上人很少", times=[fakes.time(time="5-10")])],
    moves=[fakes.move("p1", "p2", "走過去 20 分鐘")],
    day_notes=[fakes.day_note("D1", "這天很順")])

# 行程裡有解析不到的地點（築地場外市場）和猜不出來的地點（飯店）
HARD_LINE = fakes.line(
    places=[fakes.place("p1", "淺草寺", "浅草寺"), fakes.place("p2", "築地場外市場", "築地場外市場"),
            fakes.place("p3", "飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間")],
    days=[fakes.day("D1", ["p1", "p2", "p3"])])

# 築地場外市場本機地圖沒有，要上網查地址
TSUKIJI_LINE = fakes.line(
    places=[fakes.place("p1", "淺草寺", "浅草寺"), fakes.place("p2", "築地場外市場", "築地場外市場")],
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
    assert [kind for kind, _ in llm.calls] == ["line", "note", "plan", "pick", "plan", "pick"]
    assert {step: (t.calls, t.input_tokens) for step, t in r.llm_usage.items()} == {
        pipeline.PARSE: (2, 2000), pipeline.GEOCODE: (4, 4000)}
    assert env.rows("SELECT id, posted_at FROM article") == [(ID, "2025-12-29T21:09:16")]
    assert env.rows("SELECT s.source, s.source_id, p.name_ja, p.name_zh FROM place p"
                    " JOIN place_source s ON s.place_id = p.id ORDER BY p.id") == [
        ("osm", "way/1", "浅草寺", "淺草寺"), ("osm", "way/2", "東京スカイツリー", "晴空塔")]
    assert env.rows("SELECT month FROM day") == [("2",)]
    assert env.rows("SELECT seq, poi_id FROM day_place ORDER BY seq") == [(1, 1), (2, 2)]
    assert env.rows("SELECT poi_id, poi_name, content FROM place_note") == [(1, "淺草寺", "早上人很少")]
    assert env.rows("SELECT time FROM place_note_time") == [("5-10",)]
    assert env.rows("SELECT from_id, to_id, content FROM move") == [(1, 2, "走過去 20 分鐘")]
    assert env.rows("SELECT day_id, content FROM day_note") == [(1, "這天很順")]
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

def test_dropped_places_take_their_day_notes_and_moves_with_them(env, use_llm):
    article_file(env.articles)
    line = fakes.line(places=[*HARD_LINE["places"], fakes.place("p4", "晴空塔", "スカイツリー")],
                      days=[fakes.day("D1", ["p1", "p2", "p3"]), fakes.day("D2", ["p1", "p4"])])
    note = fakes.note(place_notes=[fakes.place_note("p2", "海鮮丼很讚"), fakes.place_note("p1", "早上人很少")],
                      moves=[fakes.move("p1", "p2", "搭地鐵 30 分鐘"), fakes.move("p1", "p4", "走過去 20 分鐘")],
                      day_notes=[fakes.day_note("D1", "這天很趕"), fakes.day_note("D2", "這天很順")])
    use_llm(line=line, note=note)

    r = env.run()

    assert (r.written, r.error) == ([ID], None)
    assert r.dropped == {store.DAY: 1, store.DAY_NOTE: 1, store.PLACE_NOTE: 1, store.MOVE: 1}
    assert env.rows("SELECT d.id, p.name_zh FROM day d JOIN day_place dp ON dp.day_id = d.id"
                    " JOIN place p ON p.id = dp.poi_id ORDER BY dp.seq") == [(1, "淺草寺"), (1, "晴空塔")]
    assert env.rows("SELECT content FROM day_note") == [("這天很順",)]
    assert env.rows("SELECT poi_name, content FROM place_note") == [("淺草寺", "早上人很少")]
    assert env.rows("SELECT content FROM move") == [("走過去 20 分鐘",)]
    assert not pending.path_of(env.pending, ID).exists()
    lines = geocode_log.read(r.geocode_log)
    assert [(x["name_zh"], x["result"], x["fail_reason"]) for x in lines if x["result"] == "捨棄"] == [
        ("築地場外市場", "捨棄", "上網查不到地址"), ("飯店", "捨棄", "猜不出來")]


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
    bad = fakes.line(places=[fakes.place("p1", "淺草寺", "浅草寺")], days=[fakes.day("D1", ["p1", "p7"])])
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
    # 已經寫的行還在，最後一行是 中斷
    lines = geocode_log.read(r.geocode_log)
    assert [(x["name_zh"], x["result"]) for x in lines] == [("淺草寺", "地圖-第1輪"), ("築地場外市場", "中斷")]
    assert "國土地理院連線失敗" in lines[-1]["fail_reason"]

    env.gsi = FakeGsi()
    llm = use_llm(web=fakes.web("東京都中央区築地4-16-2"))
    r = env.run()
    assert r.written == [ID] and llm.users("line") == []
    assert [x["result"] for x in geocode_log.read(r.geocode_log)] == ["資料庫", "上網"]


def test_llm_failure_while_resolving_a_place_is_logged_as_stopped(env, use_llm):
    article_file(env.articles)

    def pick(user):
        raise LLMError("額度用完")
    use_llm(line=LINE, note=NOTE, pick=pick)

    r = env.run()

    assert "額度用完" in r.error
    [line] = geocode_log.read(r.geocode_log)
    assert (line["name_zh"], line["result"], line["llm_calls"]) == ("淺草寺", "中斷", 2)   # 搜尋計畫 1 次、挑 1 次
    assert "額度用完" in line["fail_reason"]


# ---------- 並行 ----------

@pytest.mark.parametrize("jobs", [1, 3])
def test_jobs_limits_how_many_articles_are_parsed_at_once(env, use_llm, monkeypatch, jobs):
    ids = [article_file(env.articles, n)[1] for n in range(5)]
    running, peak = 0, 0

    async def slow(system, user, schema, model, effort=None):
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
        if schema is ask_place.PLAN_SCHEMA:
            return fakes.default_plan(user)
        return {"reason": "", "choice": 1}
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


def test_command_line_exits_zero_when_everything_is_written(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=LINE, note=NOTE)

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    assert code == 0
    out = capsys.readouterr().out
    assert "寫入：1 篇" in out
    [log] = (cli_paths / "logs").glob("geocode_*.jsonl")
    assert f"地點解析：共 2 個（log：{log}）" in out
    assert "  資料庫 0、地圖-第1輪 2、地圖-第2輪 0、地圖-第3輪 0、上網 0、捨棄 0" in out
    assert "拿掉沒寫入：0 筆" in out and "捨棄的地點" not in out


def test_command_line_prints_progress_and_the_tokens_spent(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=LINE, note=NOTE)

    cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    out = capsys.readouterr().out
    tag = f"[1/1] {ID}"
    progress = [line for line in out.splitlines() if line.startswith((tag, "  1/2", "  2/2", "這次要做"))]
    assert [line.split("（")[0] for line in progress] == [
        "這次要做 1 篇：每篇 llm 解析 → 地點解析 → 寫入",
        f"{tag} llm 解析中：[問題] 東京行程 0",
        f"{tag} llm 解析完成：2 個地點、1 天",
        f"{tag} 地點解析開始：2 個地點",
        "  1/2 淺草寺：地圖-第1輪 → 浅草寺",
        "  2/2 晴空塔：地圖-第1輪 → 東京スカイツリー",
        f"{tag} 寫入",
    ]
    assert "  1/2 淺草寺：地圖-第1輪 → 浅草寺（llm 2 次，0 秒）" in out
    assert "llm 用量：共 6 次，輸入 6,000 token（其中快取讀取 3,600）、輸出 300 token" in out
    assert "  照 API 定價約 US$0.06（走訂閱額度不會真的收這筆）" in out
    assert f"  {pipeline.PARSE}：2 次，輸入 2,000 token（其中快取讀取 1,200）、輸出 100 token" in out
    assert f"  {pipeline.GEOCODE}：4 次，輸入 4,000 token（其中快取讀取 2,400）、輸出 200 token" in out
    assert "花費時間：0 秒" in out


def test_each_article_reports_its_own_llm_calls(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    article_file(cli_paths / "articles", 1)
    use_llm(line=LINE, note=NOTE)

    cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    done = [line for line in capsys.readouterr().out.splitlines() if "llm 解析完成" in line]
    assert len(done) == 2 and all("（llm 2 次，" in line for line in done)


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


def test_command_line_lists_the_dropped_places_and_what_was_left_out(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=HARD_LINE, note=fakes.note())

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    out = capsys.readouterr().out
    assert code == 0
    assert "寫入：1 篇" in out
    assert "拿掉沒寫入：1 筆（天 1）" in out
    assert f"[1/1] {ID} 寫入（地點解析 0 秒，捨棄 2 個地點，拿掉 天 1）" in out
    assert "  2/3 築地場外市場：捨棄（上網查不到地址）" in out
    assert "地點解析：共 3 個" in out
    assert "  資料庫 0、地圖-第1輪 1、地圖-第2輪 0、地圖-第3輪 0、上網 0、捨棄 2" in out
    assert "  捨棄的原因：上網查不到地址 1、國土地理院查不到這個地址 0、猜不出來 1" in out
    assert f"  {ID} 築地場外市場（築地場外市場）：上網查不到地址" in out
    assert f"  {ID} 飯店（ホテル）：猜不出來" in out
    # 築地場外市場：第 1 輪沒有候選，llm 沒給新名稱（假的回空字串），第 2、3 輪名稱重複
    assert "  本機地圖每一輪的失敗：沒有候選 1、AI 都不選 0、名稱重複 2" in out


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


def test_verbose_prints_the_problems_and_which_retry_it_is(cli_paths, use_llm, caplog):
    article_file(cli_paths / "articles")
    bad = fakes.line(places=[fakes.place("p1", "淺草寺", "浅草寺")], days=[fakes.day("D1", ["p1", "p7"])])
    use_llm(line=[bad, LINE], note=NOTE)

    cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db"), "--verbose"])

    err = caplog.text
    assert f"{ID} 第一次解析（線）：檢查出 1 個問題，回報 llm 重試第 1 次（最多 2 次）" in err
    assert "days[0]：stops 的 p7 不在 places 裡" in err
    assert "第 1 輪查本機地圖「浅草寺」東京都：2 筆" in err and "llm 挑第 1 筆" in err
    assert "llm 的搜尋計畫：浅草寺｜{'pref': '東京'}" in err
