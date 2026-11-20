"""兩次 llm 解析：提示詞裡有什麼、有問題時回報 llm、第二次解析的分批和合併。"""
import asyncio

import pytest

from tests.v4.extract.fakes import day, day_note, line, line_place, move, note, place, place_note, time
from v4.extract import config, parse_line, parse_note
from v4.extract.data_schema import Article, Comment, Parsed, Stop

TEXT = "2/23 淺草寺 → 上野"
RAW_LINE = line(places=[line_place("p1", "淺草寺", "浅草寺"), line_place("p2", "上野", "上野", kind="區域")],
                days=[day("2/23", ["p1", "p2"], month="2")])
# 第一次解析轉回來的格式，第二次解析拿到的是這個
LINE = line(places=[place("p1", "淺草寺", "浅草寺", sentence=TEXT), place("p2", "上野", "上野", kind="區域", sentence=TEXT)],
            days=[{"label": "2/23", "month": "2", "stops": [{"ref": "p1", "type": "景點"}, {"ref": "p2", "type": "景點"}]}])


def make_article(comments=()) -> Article:
    return Article(id="M.1.A.AAA", title="[問題] 東京行程", author="poster", posted_at="2025-12-29T21:09:16",
                   text=TEXT, comments=list(comments))


def comments(n: int) -> list[Comment]:
    return [Comment(i, f"user{i}", f"第{i}則推文", by_author=False) for i in range(1, n + 1)]


# ---------- 第一次解析 ----------

def test_line_prompt_has_title_posted_date_and_numbered_text_only(use_llm):
    llm = use_llm(line=RAW_LINE)

    out = asyncio.run(parse_line.parse(make_article(comments(2))))

    assert out == LINE
    [user] = llm.users("line")
    assert "[問題] 東京行程" in user and "2025-12-29" in user and "1｜2/23 淺草寺 → 上野" in user
    assert "第1則推文" not in user


def test_line_output_is_expanded_to_sentences_and_typed_stops():
    text = "D1 淺草寺\n\n  住 Super Hotel 上野  \nD2 回飯店"
    raw = line(places=[line_place("p1", "淺草寺", "浅草寺"), line_place("p2", "Super Hotel 上野", "スーパーホテル上野", line=3)],
               days=[day("D1", ["p1", "h2"]), day("D2", ["h2"])])

    out = parse_line.expand(raw, text)

    assert [(p["ref"], p["sentence"]) for p in out["places"]] == [("p1", "D1 淺草寺"), ("p2", "住 Super Hotel 上野")]
    assert all("line" not in p for p in out["places"])
    assert [d["stops"] for d in out["days"]] == [
        [{"ref": "p1", "type": "景點"}, {"ref": "p2", "type": "住宿"}], [{"ref": "p2", "type": "住宿"}]]
    assert Parsed.from_llm(out, note()).days[0].stops == [Stop("p1", "景點"), Stop("p2", "住宿")]


def test_line_problems_are_reported_back_with_the_previous_output(use_llm):
    bad = line(places=[line_place("p1", "淺草寺", "浅草寺")], days=[day("2/23", ["p1", "p9"])])
    llm = use_llm(line=[bad, RAW_LINE])

    out = asyncio.run(parse_line.parse(make_article()))

    assert out == LINE
    first, second = llm.users("line")
    assert "<problems>" not in first
    assert "days[0]：stops 的 p9 不在 places 裡" in second and '"p9"' in second


def test_line_still_wrong_after_two_retries_is_an_error(use_llm):
    bad = line(places=[line_place("p1", "淺草寺", "浅草寺")], days=[day("2/23", ["p9"])])
    llm = use_llm(line=bad)

    with pytest.raises(parse_line.ParseError, match="M.1.A.AAA"):
        asyncio.run(parse_line.parse(make_article()))

    assert len(llm.users("line")) == 1 + config.RETRIES


# ---------- 第二次解析 ----------

def test_note_prompt_has_text_line_result_and_numbered_comments(use_llm):
    llm = use_llm(note=note())
    article = make_article([Comment(1, "aaa", "淺草寺早上人少", False), Comment(2, "poster", "謝謝", True)])

    asyncio.run(parse_note.parse(article, LINE))

    [user] = llm.users("note")
    assert "2/23 淺草寺 → 上野" in user and '"no": 1,\n   "label": "2/23"' in user   # 每一天前面標第幾天
    assert "1 aaa：淺草寺早上人少" in user and "2 [原po] poster：謝謝" in user
    assert parse_note.SCOPE_ALL in user


def test_note_system_prompt_includes_the_time_slot_table():
    system, _ = parse_note.build_messages(make_article(), LINE, [], first=True)

    assert config.TIME_SLOTS_MARK not in system
    assert "| 晚上 | 18–4 點（含深夜、凌晨） |" in system


def test_article_without_comments_is_still_parsed_once(use_llm):
    llm = use_llm(note=note(place_notes=[place_note("p1")]))

    out, dropped = asyncio.run(parse_note.parse(make_article(), LINE))

    assert len(llm.users("note")) == 1
    assert out["place_notes"] == [place_note("p1")] and dropped == 0


def test_comments_are_parsed_in_batches_with_the_same_line_result(use_llm, monkeypatch):
    monkeypatch.setattr(config, "COMMENTS_PER_BATCH", 2)
    llm = use_llm(note=note())

    asyncio.run(parse_note.parse(make_article(comments(5)), LINE))

    users = llm.users("note")
    assert len(users) == 3
    assert all('"label": "2/23"' in u for u in users)
    assert ["第1則推文" in u for u in users] == [True, False, False]
    assert ["第5則推文" in u for u in users] == [False, False, True]
    # 主文只在第一批抽
    assert [parse_note.SCOPE_ALL in u for u in users] == [True, False, False]
    assert [parse_note.SCOPE_COMMENTS in u for u in users] == [False, True, True]


def test_batches_are_merged_and_new_places_renumbered(use_llm, monkeypatch):
    monkeypatch.setattr(config, "COMMENTS_PER_BATCH", 1)
    skytree = place("p3", "東京晴空塔", "東京スカイツリー")
    ameyoko = place("p3", "阿美橫町", "アメ横商店街")
    use_llm(note=[
        note(places=[skytree], place_notes=[place_note("p3", "晴空塔晚上很美")], day_notes=[day_note([1])]),
        note(places=[ameyoko], place_notes=[place_note("p3", "阿美橫町年底很擠")], moves=[move("p1", "p3")]),
        note(places=[skytree], place_notes=[place_note("p3", "晴空塔要預約")]),
    ])

    out, dropped = asyncio.run(parse_note.parse(make_article(comments(3)), LINE))

    assert dropped == 0
    assert [(p["ref"], p["name_zh"]) for p in out["places"]] == [("p3", "東京晴空塔"), ("p4", "阿美橫町")]
    assert [(n["place"], n["content"]) for n in out["place_notes"]] == [
        ("p3", "晴空塔晚上很美"), ("p4", "阿美橫町年底很擠"), ("p3", "晴空塔要預約")]
    assert [(m["from"], m["to"]) for m in out["moves"]] == [("p1", "p4")]
    assert out["day_notes"] == [day_note([1])]


def test_note_items_still_wrong_after_retries_are_dropped(use_llm):
    bad = note(place_notes=[place_note("p1"), place_note("p1", times=[time(month="冬")]), place_note("p9")],
               day_notes=[day_note([9]), day_note([])])
    llm = use_llm(note=bad)

    out, dropped = asyncio.run(parse_note.parse(make_article(), LINE))

    assert len(llm.users("note")) == 1 + config.RETRIES
    assert "place_notes[1]" in llm.users("note")[1] and "day_notes[0]" in llm.users("note")[1]
    assert dropped == 3
    assert out["place_notes"] == [place_note("p1")] and out["day_notes"] == [day_note([])]


def test_dropping_a_bad_place_also_drops_what_refers_to_it(use_llm):
    bad = note(places=[place("p3", "東京晴空塔", "")],
               place_notes=[place_note("p3"), place_note("p1")], moves=[move("p1", "p3")])
    use_llm(note=bad)

    out, dropped = asyncio.run(parse_note.parse(make_article(), LINE))

    assert dropped == 3
    assert out["places"] == [] and out["moves"] == []
    assert out["place_notes"] == [place_note("p1")]


def test_note_fixed_on_retry_is_kept_whole(use_llm):
    good = note(place_notes=[place_note("p1")])
    llm = use_llm(note=[note(place_notes=[place_note("p9")]), good])

    out, dropped = asyncio.run(parse_note.parse(make_article(), LINE))

    assert len(llm.users("note")) == 2 and dropped == 0
    assert out["place_notes"] == [place_note("p1")]


def test_pending_round_trips_the_stop_type():
    p = Parsed.from_llm(parse_line.expand(line(places=[], days=[day("D1", ["p1", "h2"])]), ""), note())

    assert Parsed.from_dict(p.to_dict()) == p
