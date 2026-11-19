"""測試用的假實作：小型本機地圖（手寫的十幾筆）、假的國土地理院、假的 llm。都不打網路。"""
import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from service import llm
from service.gsi import GsiHit
from v4.extract import ask_place
from v4.extract.data_schema import LINE_SCHEMA, NOTE_SCHEMA
from v4.extract.local_map import SCHEMA, TABLE, normalize


@dataclass(frozen=True)
class Spot:
    """小型本機地圖裡的一筆。names 是名稱以外、也查得到它的寫法（OSM 的 alt_name 這類）。"""
    source: str
    source_id: str
    name: str
    pref: str
    municipality: str
    lat: float
    lon: float
    category: str = "amenity=place_of_worship"
    kind: str = "景點"
    address: str = ""
    names: tuple[str, ...] = ()
    area: bool = False      # 區域而且是多邊形：存範圍

    def boundary(self) -> str | None:
        if not self.area:
            return None
        w, s, e, n = self.lon - 0.01, self.lat - 0.01, self.lon + 0.01, self.lat + 0.01
        return json.dumps({"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]})


SENSOJI = Spot("osm", "way/1", "浅草寺", "東京都", "台東区", 35.7148, 139.7967, address="浅草2-3-1")
SENSOJI_OVERTURE = Spot("overture", "ovt-sensoji", "浅草寺", "東京都", "台東区", 35.71478, 139.79665,
                        category="buddhist_temple", address="東京都台東区浅草2-3-1")
SKYTREE = Spot("osm", "way/2", "東京スカイツリー", "東京都", "墨田区", 35.7101, 139.8107,
               category="tourism=attraction", names=("スカイツリー",))
UENO = Spot("osm", "relation/100", "上野", "東京都", "台東区", 35.7120, 139.7770, category="place=quarter",
            kind="區域", area=True)
AMEYOKO = Spot("osm", "way/3", "アメ横商店街", "東京都", "台東区", 35.7104, 139.7745, category="highway=pedestrian")
KING_ASAKUSA = Spot("overture", "ovt-king-asakusa", "焼肉きんぐ 浅草店", "東京都", "台東区", 35.7120, 139.7940,
                    category="korean_restaurant")
KING_SHINJUKU = Spot("overture", "ovt-king-shinjuku", "焼肉きんぐ 新宿店", "東京都", "新宿区", 35.6900, 139.7000,
                     category="korean_restaurant")
KIYOMIZU_KYOTO = Spot("osm", "way/21", "清水寺", "京都府", "京都市", 34.9949, 135.7850)
KIYOMIZU_TOKYO = Spot("osm", "way/22", "清水寺", "東京都", "台東区", 35.7000, 139.7800)
SOLAMACHI = Spot("osm", "way/4", "東京ソラマチ", "東京都", "墨田区", 35.7100, 139.8110, category="shop=mall")
KYOTO = Spot("osm", "node/200", "京都", "京都府", "京都市", 35.0116, 135.7681, category="place=city", kind="區域")

WORLD = (SENSOJI, SENSOJI_OVERTURE, SKYTREE, UENO, AMEYOKO, KING_ASAKUSA, KING_SHINJUKU, KIYOMIZU_KYOTO,
         KIYOMIZU_TOKYO, SOLAMACHI, KYOTO)


def build_map(path: Path, spots=WORLD) -> Path:
    """建一個表結構跟真的一樣（`local_map.SCHEMA`）的小型本機地圖。"""
    conn = duckdb.connect(str(path))
    try:
        conn.execute(SCHEMA)
        conn.executemany(
            f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i, s.source, s.source_id, s.name, list(dict.fromkeys(normalize(n) for n in (s.name, *s.names))),
              s.category, s.kind, s.pref, s.municipality, s.address, s.lat, s.lon, s.boundary())
             for i, s in enumerate(spots, 1)])
    finally:
        conn.close()
    return path


# 假的國土地理院認得的地址
GSI_HITS = {
    "東京都中央区築地4-16-2": GsiHit("東京都中央区築地四丁目", 35.6654, 139.7707),
    "東京都中央区築地4-10-5": GsiHit("東京都中央区築地四丁目", 35.6654, 139.7707),
}


class FakeGsi:
    """跟 `service.gsi.GsiClient` 一樣的介面。hits：地址 → GsiHit；fail 給了就每次丟它。"""

    def __init__(self, hits=None, fail: Exception | None = None):
        self.hits = GSI_HITS if hits is None else hits
        self.fail = fail
        self.queries: list[str] = []

    def search(self, address: str) -> GsiHit | None:
        self.queries.append(address)
        if self.fail:
            raise self.fail
        return self.hits.get(address)


WEB_NOT_FOUND = {"found": False, "address": "", "source_url": ""}


def web(address: str, url: str = "https://example.com/shop") -> dict:
    """llm 上網查到地址的回應。"""
    return {"found": True, "address": address, "source_url": url}


USAGE = llm.Usage(input_tokens=1000, output_tokens=50, cost_usd=0.01, cache_read_tokens=600)   # 假的 llm 每次呼叫的用量


class FakeLLM:
    """假的 `service.llm.call`：看輸出格式分辨是哪一種呼叫，回固定內容。

    每一種可以給：一個值（每次都回它）、一個 list（照順序一次回一個，用完報錯）、
    或一個函式（拿到 user 訊息，回傳值；丟例外就是這次呼叫失敗）。
    pick 沒給時挑第一筆；pick 的值是 `choice`（0 是都不是）。
    plan（搜尋計畫）的值用 `plan(...)` 做；沒給時是 `default_plan`：日文名稱一個、範圍是 `<place>` 的城市當都道府縣。
    web（上網查地址）的值是 `{"found", "address", "source_url"}`，沒給時是查不到。
    每次呼叫用量都記 `USAGE`（跟真的一樣加進 `llm.track()` 開著的合計）。
    """

    def __init__(self, line=None, note=None, pick=1, plan=None, web=WEB_NOT_FOUND):
        self.sources = {"line": line, "note": note, "pick": pick, "plan": plan or default_plan, "web": web}
        self.calls: list[tuple[str, str]] = []   # (哪一種, user 訊息)
        self.tools: dict[str, list] = {}         # 哪一種 → 最後一次呼叫開的工具

    def users(self, kind: str) -> list[str]:
        return [user for k, user in self.calls if k == kind]

    async def __call__(self, system, user, schema, model, effort=None, tools=None):
        kind = {id(LINE_SCHEMA): "line", id(NOTE_SCHEMA): "note", id(ask_place.PICK_SCHEMA): "pick",
                id(ask_place.PLAN_SCHEMA): "plan", id(ask_place.WEB_ADDRESS_SCHEMA): "web"}[id(schema)]
        self.calls.append((kind, user))
        self.tools[kind] = list(tools or [])
        llm.record(USAGE)
        source = self.sources[kind]
        if callable(source):
            value = source(user)
        elif isinstance(source, list):
            value = source.pop(0)
        else:
            value = source
        if kind == "pick":
            return {"reason": "", "choice": value}
        return json.loads(json.dumps(value))   # 每次給一份新的，呼叫的人改了也不影響下一次


def plan(*names, pref="", municipality="", near=0, km=0, reason="") -> dict:
    """llm 的搜尋計畫。"""
    return {"reason": reason, "names": list(names), "pref": pref, "municipality": municipality, "near": near, "km": km}


def default_plan(user: str) -> dict:
    """沒指定時的搜尋計畫：`<place>` 的日文名稱，範圍是它的城市（當都道府縣；城市不明就是全日本）。"""
    _, name_ja, city, _ = user.split("<place>")[1].split("</place>")[0].split("｜")
    return plan(name_ja, pref="" if city == "城市不明" else city)


def place(ref, name_zh, name_ja, city="東京", kind="景點", located="是", reason="", sentence=None) -> dict:
    """llm 輸出裡 `places[]` 的一筆。"""
    return {"ref": ref, "name_zh": name_zh, "name_ja": name_ja, "city": city, "kind": kind,
            "located": located, "reason": reason, "sentence": sentence or f"去{name_zh}"}


def day(label, stops, month="") -> dict:
    return {"label": label, "month": month, "stops": list(stops)}


def time(year="", month="", day_type="", time="") -> dict:
    return {"year": year, "month": month, "day_type": day_type, "time": time}


def place_note(place_ref, content="早上人很少", times=()) -> dict:
    return {"place": place_ref, "content": content, "times": list(times)}


def move(origin, dest, content="搭電車 20 分鐘", times=()) -> dict:
    return {"from": origin, "to": dest, "content": content, "times": list(times)}


def day_note(day_label, content="這天排太趕") -> dict:
    return {"day": day_label, "content": content}


def line(places=(), days=()) -> dict:
    return {"places": list(places), "days": list(days)}


def note(places=(), place_notes=(), moves=(), day_notes=()) -> dict:
    return {"places": list(places), "place_notes": list(place_notes), "moves": list(moves),
            "day_notes": list(day_notes)}
