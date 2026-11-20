"""程式檢查欄位：每條檢查各一個會被抓到的例子。"""
import pytest

from tests.v4.extract.fakes import day, day_note, line, line_place, move, note, place, place_note, time
from v4.extract import check, parse_line

TEXT = ["D1 淺草寺 → 上野", "", "住哪裡還沒決定"]
LINE = line(
    places=[line_place("p1", "淺草寺", "浅草寺"), line_place("p2", "上野", "上野", kind="區域"),
            line_place("p3", "飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間", line=3)],
    days=[day("D1", ["p1", "p2", "h3"], month="2")],
)
EXPANDED = parse_line.expand(LINE, "\n".join(TEXT))   # 第二次解析拿到的是轉回來的格式


def messages(problems) -> list[str]:
    return [str(p) for p in problems]


# ---------- 第一次解析（線） ----------

def test_valid_line_output_has_no_problems():
    assert check.check_line(LINE, TEXT) == []


def test_stop_must_refer_to_a_listed_place():
    out = line(places=[line_place("p1", "淺草寺", "浅草寺")], days=[day("D1", ["p1", "p9", "h8"])])

    assert messages(check.check_line(out, TEXT)) == [
        "days[0]：stops 的 p9 不在 places 裡", "days[0]：stops 的 h8 不在 places 裡（沒有 p8）"]


@pytest.mark.parametrize("stop", ["1", "P1", "x1", "h", "p1 ", "住宿"])
def test_stop_must_be_p_or_h_with_a_number(stop):
    out = line(places=[line_place("p1", "淺草寺", "浅草寺")], days=[day("D1", [stop])])

    assert messages(check.check_line(out, TEXT)) == [
        f"days[0]：stops 的 {stop} 要是 p 或 h 加數字（景點 p3、住宿 h3）"]


def test_place_ref_must_be_p_with_a_number():
    out = line(places=[line_place("a1", "淺草寺", "浅草寺")])

    assert messages(check.check_line(out, TEXT)) == ["places[0]：ref a1 要是 p 加數字（p1、p2）"]


@pytest.mark.parametrize("n, message", [(0, "line 0 不在主文的 1～3 行裡"), (4, "line 4 不在主文的 1～3 行裡"),
                                        (2, "line 2 是空行，要給提到這個地點的那一行")])
def test_place_line_must_be_a_non_blank_line_of_the_text(n, message):
    out = line(places=[line_place("p1", "淺草寺", "浅草寺", line=n)])

    assert messages(check.check_line(out, TEXT)) == [f"places[0]：{message}"]


def test_place_ref_must_be_unique():
    out = line(places=[line_place("p1", "淺草寺", "浅草寺"), line_place("p1", "上野", "上野")])

    assert messages(check.check_line(out, TEXT)) == ["places[1]：ref p1 重複"]


@pytest.mark.parametrize("field", ["name_zh", "name_ja"])
def test_place_names_must_not_be_empty(field):
    out = line(places=[{**line_place("p1", "淺草寺", "浅草寺"), field: " "}])

    assert messages(check.check_line(out, TEXT)) == [f"places[0]：{field} 是空的"]


@pytest.mark.parametrize("month", ["13", "0", "冬", "2-3", "02"])
def test_day_month_must_be_one_month_or_empty(month):
    out = line(places=[line_place("p1", "淺草寺", "浅草寺")], days=[day("D1", ["p1"], month=month)])

    assert [p.kind for p in check.check_line(out, TEXT)] == ["days"]


# ---------- 第二次解析（點、段、day_note） ----------

def test_valid_note_output_has_no_problems():
    out = note(
        places=[place("p4", "東京晴空塔", "東京スカイツリー")],
        place_notes=[place_note("p4", times=[time("2024", "12", "假日", "18-4"), time(time="7")])],
        moves=[move("p1", "p4", times=[time(month="1", time="7-9")])],
        day_notes=[day_note([1]), day_note([])],
    )

    assert check.check_note(out, EXPANDED) == []


def test_place_note_must_refer_to_an_existing_place():
    out = note(place_notes=[place_note("p9")])

    assert messages(check.check_note(out, EXPANDED)) == ["place_notes[0]：place 的 p9 不在 places 裡"]


def test_move_must_refer_to_existing_places():
    out = note(moves=[move("p1", "p9")])

    assert messages(check.check_note(out, EXPANDED)) == ["moves[0]：to 的 p9 不在 places 裡"]


def test_day_note_days_must_be_days_of_the_itinerary():
    out = note(day_notes=[day_note([0, 1, 7]), day_note([1, 1])])

    assert messages(check.check_note(out, EXPANDED)) == [
        "day_notes[0]：days 的 [0, 7] 不在 1～1 裡（`<line>` 每一天的 no）", "day_notes[1]：days 有重複的天"]


def test_move_must_have_different_ends():
    out = note(moves=[move("p1", "p1")])

    assert messages(check.check_note(out, EXPANDED)) == ["moves[0]：from 和 to 不能是同一個地點"]


@pytest.mark.parametrize("out, kind", [
    (note(place_notes=[place_note("p1", content="")]), "place_notes"),
    (note(moves=[move("p1", "p2", content="  ")]), "moves"),
    (note(day_notes=[day_note([1], content="")]), "day_notes"),
])
def test_content_must_not_be_empty(out, kind):
    assert messages(check.check_note(out, EXPANDED)) == [f"{kind}[0]：content 是空的"]


def test_place_that_cannot_be_guessed_must_not_be_referenced():
    out = note(place_notes=[place_note("p3")], moves=[move("p1", "p3")])

    assert messages(check.check_note(out, EXPANDED)) == [
        "place_notes[0]：place 的 p3 是猜不出來的地點，不能引用",
        "moves[0]：to 的 p3 是猜不出來的地點，不能引用",
    ]


@pytest.mark.parametrize("month", ["13", "0", "冬", "12-2"])
def test_time_month_must_be_one_month(month):
    out = note(place_notes=[place_note("p1", times=[time(month=month)])])

    assert [p.kind for p in check.check_note(out, EXPANDED)] == ["place_notes"]


@pytest.mark.parametrize("value", ["24", "7點", "7-", "5-10-12", "7:30", "25-3"])
def test_time_must_be_an_hour_or_a_range(value):
    out = note(moves=[move("p1", "p2", times=[time(time=value)])])

    assert [p.kind for p in check.check_note(out, EXPANDED)] == ["moves"]


def test_time_written_as_a_slot_name_is_told_the_range_to_store():
    out = note(place_notes=[place_note("p1", times=[time(time="晚上")])])

    [problem] = check.check_note(out, EXPANDED)

    assert "「晚上」照時段表存 18-4" in problem.message


def test_time_slots_come_from_the_shared_table():
    assert check.time_slots() == {"早上": "5-10", "中午": "11-13", "下午": "14-17", "晚上": "18-4"}


@pytest.mark.parametrize("field, value", [("year", "25"), ("day_type", "週末")])
def test_other_time_fields_are_checked_too(field, value):
    out = note(place_notes=[place_note("p1", times=[time(**{field: value})])])

    assert [p.kind for p in check.check_note(out, EXPANDED)] == ["place_notes"]


def test_new_place_must_not_reuse_a_ref_from_the_itinerary():
    out = note(places=[place("p1", "東京晴空塔", "東京スカイツリー"), place("p5", "", "名前")])

    assert messages(check.check_note(out, EXPANDED)) == ["places[0]：ref p1 重複", "places[1]：name_zh 是空的"]


def test_drop_removes_the_items_with_problems():
    out = note(place_notes=[place_note("p1"), place_note("p9"), place_note("p2", content="")],
               moves=[move("p1", "p1")])

    removed = check.drop(out, check.check_note(out, EXPANDED))

    assert removed == 3
    assert out["place_notes"] == [place_note("p1")] and out["moves"] == []
