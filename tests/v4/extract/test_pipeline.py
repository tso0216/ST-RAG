"""主流程：一篇文章從 JSON 到資料庫，以及略過、停下、填答案後重跑。"""
import asyncio
import csv
import json
import sqlite3

import pytest

from service.gsi import GsiError
from service.llm import LLMError
from tests.v4.extract import fakes
from tests.v4.extract.fakes import FakeGsi, build_map
from v4.extract import __main__ as cli
from v4.extract import config, geocode_log, pending, pipeline, unresolved
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
    """回傳 run(sources, **kw)：資料庫、tsv、pending、log、本機地圖都放在 tmp_path。"""
    class Env:
        db = tmp_path / "v4.db"
        tsv = tmp_path / "place_unresolved.tsv"
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
                    local_map=local_map, gsi=self.gsi, unresolved_path=self.tsv, pending_dir=self.pending,
                    log_dir=self.logs))
            finally:
                local_map.close()

        def rows(self, sql):
            conn = sqlite3.connect(self.db)
            try:
                return conn.execute(sql).fetchall()
            finally:
                conn.close()

        def tsv_rows(self):
            with self.tsv.open(newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f, delimiter="\t"))

        def answer(self, name_zh, text):
            rows = self.tsv_rows()
            for r in rows:
                if r["name_zh"] == name_zh:
                    r["answer"] = text
            with self.tsv.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, unresolved.COLUMNS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
    return Env()


# ---------- 一篇從 JSON 到資料庫 ----------

def test_article_goes_from_json_to_database(env, use_llm):
    article_file(env.articles)
    llm = use_llm(line=LINE, note=NOTE)

    r = env.run()

    assert (r.written, r.skipped, r.waiting, r.dropped, r.unresolved, r.error) == ([ID], [], [], 0, [], None)
    assert [kind for kind, _ in llm.calls] == ["line", "note", "pick", "pick"]
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
    assert not env.tsv.exists()
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

    assert r.written == [ID] and r.dropped == 1
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


# ---------- 地點要使用者決定 ----------

def test_article_with_a_place_to_decide_is_not_written_and_is_reported(env, use_llm):
    article_file(env.articles)
    _, other = article_file(env.articles, 1)
    lines = [HARD_LINE, LINE]
    use_llm(line=lambda user: lines.pop(0), note=fakes.note())

    r = env.run()

    assert r.written == [other] and r.waiting == [ID] and r.error is None
    assert [(u.type, u.name_zh, u.where) for u in r.unresolved] == [
        ("解析不到", "築地場外市場", "D1 第 2 站"), ("猜不出來", "飯店", "D1 第 3 站")]
    assert env.rows("SELECT id FROM article") == [(other,)]
    assert [(row["type"], row["name_zh"], row["articles"], row["answer"]) for row in env.tsv_rows()] == [
        ("解析不到", "築地場外市場", ID, ""), ("猜不出來", "飯店", ID, "")]
    assert pending.path_of(env.pending, ID).exists() and not pending.path_of(env.pending, other).exists()


def test_rerun_after_answering_writes_the_article_without_parsing_it_again(env, use_llm):
    article_file(env.articles)
    use_llm(line=HARD_LINE, note=fakes.note(place_notes=[fakes.place_note("p2", "海鮮丼很讚")]))
    env.run()
    env.answer("築地場外市場", "35.6655,139.7707")
    env.answer("飯店", "不收")
    llm = use_llm()

    r = env.run()

    assert r.written == [ID] and r.waiting == [] and r.unresolved == []
    assert llm.users("line") == [] and llm.users("note") == []
    assert env.rows("SELECT s.source, p.name_zh, p.lat, p.lon FROM place p"
                    " JOIN place_source s ON s.place_id = p.id ORDER BY p.id") == [
        ("osm", "淺草寺", 35.7148, 139.7967), ("manual", "築地場外市場", 35.6655, 139.7707)]
    assert env.rows("SELECT seq, poi_id FROM day_place ORDER BY seq") == [(1, 1), (2, 2)]
    assert env.rows("SELECT poi_name, content FROM place_note") == [("築地場外市場", "海鮮丼很讚")]
    assert r.dropped == 1   # 飯店那一站
    assert not pending.path_of(env.pending, ID).exists()
    # 填過答案的列留著，之後別篇出現同一個地點可以直接用
    assert [(row["name_zh"], row["answer"]) for row in env.tsv_rows()] == [
        ("築地場外市場", "35.6655,139.7707"), ("飯店", "不收")]


def test_rerun_with_only_some_answers_keeps_waiting(env, use_llm):
    article_file(env.articles)
    use_llm(line=HARD_LINE, note=fakes.note())
    env.run()
    env.answer("飯店", "不收")
    use_llm()

    r = env.run()

    assert r.written == [] and r.waiting == [ID]
    assert [u.name_zh for u in r.unresolved] == ["築地場外市場"]
    assert [(row["name_zh"], row["answer"]) for row in env.tsv_rows()] == [("築地場外市場", ""), ("飯店", "不收")]
    assert env.rows("SELECT COUNT(*) FROM article") == [(0,)]


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
    assert not env.tsv.exists()
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
    assert (line["name_zh"], line["result"], line["llm_calls"]) == ("淺草寺", "中斷", 1)
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
        return {"reason": "", "choice": 1}
    monkeypatch.setattr("service.llm.call", slow)

    r = env.run(jobs=jobs)

    assert peak == jobs
    assert r.written == ids   # 寫入照輸入的順序


# ---------- 命令列 ----------

@pytest.fixture
def cli_paths(tmp_path, monkeypatch):
    """命令列用的是 `config` 的預設路徑（conftest 已經換到 tmp_path）；本機地圖用小型的，國土地理院換成假的。"""
    monkeypatch.setattr(config, "UNRESOLVED_PATH", tmp_path / "place_unresolved.tsv")
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
    assert "  資料庫 0、答案 0、地圖-第1輪 2、地圖-第2輪 0、地圖-第3輪 0、上網 0、等你決定 0、不收 0" in out


def test_command_line_exits_non_zero_and_lists_the_places_to_decide(cli_paths, use_llm, capsys):
    article_file(cli_paths / "articles")
    use_llm(line=HARD_LINE, note=fakes.note())

    code = cli.main([str(cli_paths / "articles"), "--db", str(cli_paths / "v4.db")])

    out = capsys.readouterr()
    assert code == 1
    assert "等地點答案：1 篇" in out.out
    assert "[解析不到] 築地場外市場" in out.err and "[猜不出來] 飯店" in out.err
    assert str(cli_paths / "place_unresolved.tsv") in out.err
    assert (cli_paths / "place_unresolved.tsv").exists()
    assert "地點解析：共 3 個" in out.out
    assert "  資料庫 0、答案 0、地圖-第1輪 1、地圖-第2輪 0、地圖-第3輪 0、上網 0、等你決定 2、不收 0" in out.out
    assert "  等你決定的原因：上網查不到地址 1、國土地理院查不到這個地址 0、猜不出來，是行程的一站 1" in out.out
    # 築地場外市場：第 1 輪沒有候選，llm 沒給新名稱（假的回空字串），第 2、3 輪名稱重複
    assert "  本機地圖每一輪的失敗：沒有候選 1、AI 都不選 0、名稱重複 2" in out.out


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
    assert "第 1 輪查本機地圖「浅草寺 東京」：2 筆" in err and "llm 挑第 1 筆" in err
