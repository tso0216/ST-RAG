"""時間比對：鐘點範圍（含跨夜）、時間不明、好幾列、年份條件、天和 day_note 的月份。"""
import pytest

from v4.rag.data_schema import TimeCond
from v4.rag.timematch import HOURS, MONTH, YEAR, TimeRow, day_rows, match, month_rows

AFTERNOON = TimeCond(hours=((14, 17),))
NIGHT = TimeCond(hours=((18, 2),))


@pytest.mark.parametrize("time", ["15", "14-16", "16-19"])
def test_hours_overlapping_condition_match(time):
    assert match([TimeRow(time=time)], AFTERNOON, 2025).ok


def test_hour_outside_condition_does_not_match():
    assert not match([TimeRow(time="9")], AFTERNOON, 2025).ok


@pytest.mark.parametrize("time", ["22-2", "1"])
def test_overnight_condition(time):
    assert match([TimeRow(time=time)], NIGHT, 2025).ok


def test_overnight_condition_excludes_daytime():
    assert not match([TimeRow(time="10")], NIGHT, 2025).ok


def test_empty_hour_matches_as_unknown():
    m = match([TimeRow(month="3")], AFTERNOON, 2025)
    assert m.ok and m.unknown == (HOURS,)


def test_empty_hour_is_not_unknown_without_hour_condition():
    m = match([TimeRow(month="3")], TimeCond(months=(3,)), 2025)
    assert m.ok and m.unknown == ()


def test_no_rows_is_unknown_on_restricted_dimensions_only():
    m = match([], TimeCond(months=(10,), hours=((9, 11),)), 2025)
    assert m.ok and m.unknown == (MONTH, HOURS)


def test_one_matching_row_is_enough():
    rows = [TimeRow(month="1", time="9"), TimeRow(month="10", time="15")]
    m = match(rows, TimeCond(months=(10,), hours=((14, 17),)), 2025)
    assert m.ok and m.row == rows[1]


def test_prefers_row_with_fewest_unknown():
    rows = [TimeRow(month="10"), TimeRow(month="10", time="15")]
    m = match(rows, TimeCond(months=(10,), hours=((14, 17),)), 2025)
    assert m.unknown == () and m.row == rows[1]


def test_wrong_month_does_not_match():
    assert not match([TimeRow(month="11")], TimeCond(months=(10,)), 2025).ok


class TestYear:
    COND = TimeCond(year=2024)

    def test_earlier_year_fails(self):
        assert not match([TimeRow(year=2023)], self.COND, 2025).ok

    def test_same_or_later_year_matches(self):
        assert match([TimeRow(year=2024)], self.COND, 2025).unknown == ()

    def test_empty_year_posted_earlier_fails(self):
        assert not match([TimeRow()], self.COND, 2023).ok

    def test_empty_year_posted_later_is_unknown(self):
        m = match([TimeRow()], self.COND, 2025)
        assert m.ok and m.unknown == (YEAR,)


def test_day_uses_its_month():
    assert match(day_rows("10"), TimeCond(months=(10,)), 2025).ok
    assert not match(day_rows("11"), TimeCond(months=(10,)), 2025).ok
    assert match(day_rows(None), TimeCond(months=(10,)), 2025).unknown == (MONTH,)


def test_day_note_uses_months_of_its_days():
    """講到好幾天（或講整趟時那篇所有天）的月份，任一個符合就算。"""
    assert match(month_rows(["9", "10"]), TimeCond(months=(10,)), 2025).ok
    assert not match(month_rows(["9", "11"]), TimeCond(months=(10,)), 2025).ok
    assert match(month_rows([]), TimeCond(months=(10,)), 2025).unknown == (MONTH,)
