"""國土地理院：取第一筆的座標和地址，空陣列是查不到。"""
import json

from service.gsi import GsiClient, GsiHit, parse_first


def feature(title, lon, lat) -> dict:
    return {"geometry": {"coordinates": [lon, lat], "type": "Point"}, "type": "Feature",
            "properties": {"addressCode": "", "title": title}}


def test_first_feature_is_used(monkeypatch):
    body = json.dumps([feature("東京都台東区浅草二丁目", 139.7967, 35.7148), feature("別的", 1, 2)])
    client = GsiClient()
    urls = []
    monkeypatch.setattr(client, "_get", lambda url, what: urls.append(url) or body)

    assert client.search("東京都台東区浅草2-3-1") == GsiHit("東京都台東区浅草二丁目", 35.7148, 139.7967)
    assert urls[0].startswith("https://msearch.gsi.go.jp/address-search/AddressSearch?q=%E6%9D%B1")


def test_empty_result_is_not_found():
    assert parse_first([]) is None
