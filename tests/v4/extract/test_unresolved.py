"""`place_unresolved.tsv`：分辨答案、找答案、寫入要使用者決定的地點。"""
import csv
import io

import pytest

from v4.extract import unresolved
from v4.extract.data_schema import NO_GUESS, NOT_FOUND, Unresolved


def read(path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def fill(path, name_zh: str, answer: str) -> None:
    """模擬使用者在某一列填答案。"""
    rows = read(path)
    for r in rows:
        if r["name_zh"] == name_zh:
            r["answer"] = answer
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, unresolved.COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def not_found(article_id, name_zh="築地場外市場", name_ja="築地場外市場", city="東京", **kw) -> Unresolved:
    return Unresolved(NOT_FOUND, article_id, name_zh, name_ja, city, **kw)


def no_guess(article_id, name_zh="飯店", **kw) -> Unresolved:
    return Unresolved(NO_GUESS, article_id, name_zh, "ホテル", "東京", **kw)


# ---------- 四種答案 ----------

@pytest.mark.parametrize("text, osm_type, osm_id", [
    ("https://www.openstreetmap.org/way/123", "way", 123),
    ("openstreetmap.org/node/45#map=19/35.7/139.7", "node", 45),
    ("  https://www.openstreetmap.org/relation/1758858  ", "relation", 1758858),
])
def test_osm_url_answer(text, osm_type, osm_id):
    a = unresolved.parse_answer(text)

    assert (a.kind, a.osm_type, a.osm_id) == (unresolved.OSM, osm_type, osm_id)


@pytest.mark.parametrize("text", ["35.7148,139.7967", "35.7148, 139.7967", "35.7148，139.7967"])
def test_coordinate_answer(text):
    a = unresolved.parse_answer(text)

    assert (a.kind, a.lat, a.lon) == (unresolved.COORD, 35.7148, 139.7967)


def test_skip_answer():
    assert unresolved.parse_answer(" 不收 ").kind == unresolved.EXCLUDE


@pytest.mark.parametrize("text", ["築地場外市場", "ホテルグレイスリー新宿", "135.5,200.1"])
def test_anything_else_is_a_japanese_name(text):
    a = unresolved.parse_answer(text)

    assert (a.kind, a.name) == (unresolved.NAME, text)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_answer_is_no_answer(text):
    assert unresolved.parse_answer(text) is None


# ---------- 寫入 ----------

def test_places_to_decide_are_written_with_an_empty_answer(tmp_path):
    path = tmp_path / "place_unresolved.tsv"

    unresolved.save(path, [
        not_found("M.1.A.AAA", sentence="早上去\t築地場外市場\n吃早餐", tried=["築地場外市場", "築地市場"], reason=""),
        no_guess("M.1.A.AAA", sentence="回飯店", reason="沒寫是哪一間飯店", where="D1 第 3 站"),
    ], handled={"M.1.A.AAA"})

    assert path.read_text(encoding="utf-8").splitlines()[0].split("\t") == list(unresolved.COLUMNS)
    assert read(path) == [
        {"type": "解析不到", "name_zh": "築地場外市場", "name_ja": "築地場外市場", "city": "東京",
         "articles": "M.1.A.AAA", "sentence": "早上去 築地場外市場 吃早餐", "tried": "築地場外市場、築地市場",
         "reason": "", "answer": ""},
        {"type": "猜不出來", "name_zh": "飯店", "name_ja": "ホテル", "city": "東京", "articles": "M.1.A.AAA",
         "sentence": "回飯店", "tried": "", "reason": "沒寫是哪一間飯店", "answer": ""},
    ]


def test_unresolvable_place_is_listed_once_with_all_its_articles(tmp_path):
    path = tmp_path / "place_unresolved.tsv"

    unresolved.save(path, [not_found("M.1.A.AAA"), not_found("M.2.A.BBB", name_ja="築地場外"),
                           not_found("M.3.A.CCC", city="大阪")], handled={"M.1.A.AAA", "M.2.A.BBB", "M.3.A.CCC"})

    assert [(r["name_zh"], r["city"], r["articles"]) for r in read(path)] == [
        ("築地場外市場", "東京", "M.1.A.AAA,M.2.A.BBB"), ("築地場外市場", "大阪", "M.3.A.CCC")]


def test_place_that_cannot_be_guessed_is_one_row_per_article(tmp_path):
    path = tmp_path / "place_unresolved.tsv"

    unresolved.save(path, [no_guess("M.1.A.AAA"), no_guess("M.2.A.BBB")], handled={"M.1.A.AAA", "M.2.A.BBB"})

    assert [(r["name_zh"], r["articles"]) for r in read(path)] == [("飯店", "M.1.A.AAA"), ("飯店", "M.2.A.BBB")]


def test_guessed_branches_of_the_same_chain_are_separate_rows(tmp_path):
    path = tmp_path / "place_unresolved.tsv"

    unresolved.save(path, [
        not_found("M.1.A.AAA", "燒肉 KING", "焼肉きんぐ 浅草", guessed=True),
        not_found("M.2.A.BBB", "燒肉 KING", "焼肉きんぐ 新宿", guessed=True),
        not_found("M.3.A.CCC", "燒肉 KING", "焼肉きんぐ 浅草", guessed=True),
    ], handled={"M.1.A.AAA", "M.2.A.BBB", "M.3.A.CCC"})

    assert [(r["name_ja"], r["articles"]) for r in read(path)] == [
        ("焼肉きんぐ 浅草", "M.1.A.AAA,M.3.A.CCC"), ("焼肉きんぐ 新宿", "M.2.A.BBB")]


def test_answered_rows_are_not_overwritten(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [not_found("M.1.A.AAA", sentence="原本的句子"), no_guess("M.1.A.AAA")], {"M.1.A.AAA"})
    fill(path, "築地場外市場", "https://www.openstreetmap.org/way/123")
    before = read(path)[0]

    # 重跑：同一個地點又被報一次（例如答案查不到），另一篇也出現它
    unresolved.save(path, [not_found("M.1.A.AAA", sentence="新的句子", tried=["別的"]), not_found("M.2.A.BBB"),
                           no_guess("M.1.A.AAA")], {"M.1.A.AAA", "M.2.A.BBB"})

    rows = read(path)
    assert rows[0] == before
    assert [(r["name_zh"], r["articles"], r["answer"]) for r in rows[1:]] == [("飯店", "M.1.A.AAA", "")]


def test_open_row_is_removed_once_its_articles_no_longer_need_it(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [not_found("M.1.A.AAA"), not_found("M.2.A.BBB"), no_guess("M.1.A.AAA")],
                    {"M.1.A.AAA", "M.2.A.BBB"})

    # 重跑只做了第一篇，它的地點這次都有結果了
    unresolved.save(path, [], handled={"M.1.A.AAA"})

    assert [(r["name_zh"], r["articles"]) for r in read(path)] == [("築地場外市場", "M.2.A.BBB")]


def test_nothing_to_decide_does_not_create_the_file(tmp_path):
    path = tmp_path / "place_unresolved.tsv"

    unresolved.save(path, [], handled={"M.1.A.AAA"})

    assert not path.exists()


# ---------- 找答案 ----------

def test_answer_for_unresolvable_place_applies_to_any_article_with_the_same_name_and_city(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [not_found("M.1.A.AAA")], {"M.1.A.AAA"})
    assert unresolved.Answers.load(path).for_place("築地場外市場", "築地場外市場", "東京") is None
    fill(path, "築地場外市場", "35.6655,139.7707")

    answers = unresolved.Answers.load(path)

    assert answers.for_place("築地場外市場", "別的日文寫法", "東京").kind == unresolved.COORD
    assert answers.for_place("築地場外市場", "築地場外市場", "大阪") is None
    assert answers.for_place("築地市場", "築地場外市場", "東京") is None
    # 這是解析不到的答案，不是替某一篇的「猜不出來」填的
    assert answers.for_guess("M.1.A.AAA", "築地場外市場") is None


def test_answer_for_place_that_cannot_be_guessed_only_applies_to_that_article(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [no_guess("M.1.A.AAA"), no_guess("M.2.A.BBB")], {"M.1.A.AAA", "M.2.A.BBB"})
    rows = read(path)
    rows[0]["answer"] = "ホテルグレイスリー新宿"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, unresolved.COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    answers = unresolved.Answers.load(path)

    assert answers.for_guess("M.1.A.AAA", "飯店").name == "ホテルグレイスリー新宿"
    assert answers.for_guess("M.2.A.BBB", "飯店") is None
    assert answers.for_guess("M.3.A.CCC", "飯店") is None
    assert answers.for_place("飯店", "ホテル", "東京") is None


def test_answer_for_a_guessed_branch_needs_the_same_japanese_name(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [not_found("M.1.A.AAA", "燒肉 KING", "焼肉きんぐ 浅草", guessed=True)], {"M.1.A.AAA"})
    fill(path, "燒肉 KING", "不收")

    answers = unresolved.Answers.load(path)

    assert answers.for_place("燒肉 KING", "焼肉きんぐ 浅草", "東京", guessed=True).kind == unresolved.EXCLUDE
    assert answers.for_place("燒肉 KING", "焼肉きんぐ 新宿", "東京", guessed=True) is None


def test_file_saved_with_bom_is_still_read(tmp_path):
    path = tmp_path / "place_unresolved.tsv"
    unresolved.save(path, [not_found("M.1.A.AAA")], {"M.1.A.AAA"})
    fill(path, "築地場外市場", "不收")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    assert unresolved.Answers.load(path).for_place("築地場外市場", "", "東京").kind == unresolved.EXCLUDE


def test_missing_file_has_no_answers(tmp_path):
    assert unresolved.Answers.load(tmp_path / "none.tsv").for_guess("M.1.A.AAA", "飯店") is None


# ---------- print ----------

def test_print_list_shows_what_the_user_needs_to_decide():
    out = io.StringIO()

    unresolved.print_list([
        not_found("M.1.A.AAA", sentence="早上去築地場外市場", tried=["築地場外市場", "築地市場"]),
        not_found("M.2.A.BBB", tried=["築地場外"]),
        no_guess("M.1.A.AAA", sentence="回飯店", reason="沒寫是哪一間飯店", where="D1 第 3 站",
                 same_day=["淺草寺", "上野"]),
    ], file=out)

    text = out.getvalue()
    assert text.count("[解析不到] 築地場外市場（築地場外市場，東京）") == 1
    assert "文章：M.1.A.AAA、M.2.A.BBB" in text
    assert "原文：早上去築地場外市場" in text
    assert "試過的日文名稱：築地場外市場、築地市場、築地場外" in text
    assert "https://www.openstreetmap.org/search?query=%E7%AF%89%E5%9C%B0" in text
    assert "[猜不出來] 飯店（ホテル，東京）" in text
    assert "行程：D1 第 3 站" in text and "同一天的其他站：淺草寺、上野" in text and "原因：沒寫是哪一間飯店" in text
