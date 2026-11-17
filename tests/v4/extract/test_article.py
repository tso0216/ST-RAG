"""程式整理文章。"""
import json

import pytest

from v4.extract import article


def write(tmp_path, **fields):
    doc = {
        "title": "[問題] 東京五天行程",
        "author": "poster",
        "time": "Mon Dec 29 21:09:16 2025",
        "url": "https://www.ptt.cc/bbs/Japan_Travel/M.1767013758.A.A9D.html",
        "text": "主文\n",
        "comments": [],
        **fields,
    }
    path = tmp_path / "ptt_any_name.json"
    path.write_text(json.dumps(doc, ensure_ascii=False))
    return path


def test_article_id_comes_from_url_and_posted_date_from_time(tmp_path):
    a = article.load(write(tmp_path))

    assert a.id == "M.1767013758.A.A9D"
    assert a.title == "[問題] 東京五天行程"
    assert a.posted_at == "2025-12-29T21:09:16"
    assert a.posted_date == "2025-12-29"
    assert a.text == "主文"


def test_consecutive_comments_by_the_same_user_are_merged_and_numbered_from_one(tmp_path):
    comments = [
        {"tag": "推", "user": "aaa", "content": "d1入境抓1小時，", "time": "12/30 00:04"},
        {"tag": "→", "user": "aaa", "content": "半小時就到飯店有點樂觀", "time": "12/30 00:04"},
        {"tag": "→", "user": "bbb", "content": "", "time": "12/30 00:05"},
        {"tag": "→", "user": "bbb", "content": "那你如何去雪具店?", "time": "12/30 01:08"},
        {"tag": "→", "user": "aaa", "content": "用走的", "time": "12/30 01:10"},
    ]

    a = article.load(write(tmp_path, comments=comments))

    assert [(c.no, c.user, c.content) for c in a.comments] == [
        (1, "aaa", "d1入境抓1小時，半小時就到飯店有點樂觀"),
        (2, "bbb", "那你如何去雪具店?"),
        (3, "aaa", "用走的"),
    ]


def test_comments_by_the_author_are_marked(tmp_path):
    comments = [
        {"tag": "推", "user": "aaa", "content": "推", "time": ""},
        {"tag": "→", "user": "poster", "content": "謝謝，補充一下我住上野", "time": ""},
    ]

    a = article.load(write(tmp_path, comments=comments))

    assert [c.by_author for c in a.comments] == [False, True]


@pytest.mark.parametrize("fields", [{"author": ""}, {"author": None}])
def test_article_without_author_is_skipped(tmp_path, fields):
    with pytest.raises(article.Skipped, match="沒有 author"):
        article.load(write(tmp_path, **fields))


def test_article_file_without_author_key_is_skipped(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"title": "舊檔", "time": "", "text": "主文", "comments": [],
                                "url": "https://www.ptt.cc/bbs/Japan_Travel/M.1767013758.A.A9D.html"}))

    with pytest.raises(article.Skipped, match="沒有 author"):
        article.load(path)


def test_unreadable_file_and_missing_id_are_skipped(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON")

    with pytest.raises(article.Skipped, match="讀不了"):
        article.load(broken)
    with pytest.raises(article.Skipped, match="文章 id"):
        article.load(write(tmp_path, url="https://www.ptt.cc/bbs/Japan_Travel/index.html"))


def test_unreadable_time_falls_back_to_the_timestamp_in_the_id(tmp_path):
    a = article.load(write(tmp_path, time=""))

    assert a.posted_date == "2025-12-29"
