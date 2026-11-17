"""測試用的假實作：假的 Nominatim client（固定的小世界）和假的 llm。都不打網路。"""
import json
from dataclasses import dataclass

from v4.extract import ask_place
from v4.extract.data_schema import LINE_SCHEMA, NOTE_SCHEMA


@dataclass(frozen=True)
class Osm:
    """假世界裡的一個 OSM 物件。names 是查得到它的寫法，第一個是 OSM 上的名稱。"""
    osm_type: str
    osm_id: int
    names: tuple[str, ...]
    addresstype: str
    lat: float
    lon: float
    address: str = "東京都, 日本"
    area: bool = False

    def result(self) -> dict:
        r = {
            "osm_type": self.osm_type, "osm_id": self.osm_id, "name": self.names[0],
            "category": "boundary" if self.area else self.addresstype,
            "type": "administrative" if self.area else "yes",
            "addresstype": self.addresstype, "lat": str(self.lat), "lon": str(self.lon),
            "display_name": f"{self.names[0]}, {self.address}",
        }
        if self.area:
            w, s, e, n = self.lon - 0.01, self.lat - 0.01, self.lon + 0.01, self.lat + 0.01
            r["geojson"] = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
        else:
            r["geojson"] = {"type": "Point", "coordinates": [self.lon, self.lat]}
        return r


SENSOJI = Osm("way", 1, ("浅草寺",), "amenity", 35.7148, 139.7967, "台東区, 東京都, 日本")
SKYTREE = Osm("way", 2, ("東京スカイツリー", "スカイツリー"), "tourism", 35.7101, 139.8107, "墨田区, 東京都, 日本")
UENO = Osm("relation", 100, ("上野",), "quarter", 35.7120, 139.7770, "台東区, 東京都, 日本", area=True)
AMEYOKO = Osm("way", 3, ("アメ横商店街",), "highway", 35.7104, 139.7745, "台東区, 東京都, 日本")
KING_ASAKUSA = Osm("node", 11, ("焼肉きんぐ 浅草店", "焼肉きんぐ"), "amenity", 35.7120, 139.7940, "台東区, 東京都, 日本")
KING_SHINJUKU = Osm("node", 12, ("焼肉きんぐ 新宿店", "焼肉きんぐ"), "amenity", 35.6900, 139.7000, "新宿区, 東京都, 日本")
KIYOMIZU_KYOTO = Osm("way", 21, ("清水寺",), "amenity", 34.9949, 135.7850, "東山区, 京都市, 日本")
KIYOMIZU_TOKYO = Osm("way", 22, ("清水寺",), "amenity", 35.7000, 139.7800, "台東区, 東京都, 日本")
SOLAMACHI = Osm("way", 4, ("東京ソラマチ",), "shop", 35.7100, 139.8110, "墨田区, 東京都, 日本")

WORLD = (SENSOJI, SKYTREE, UENO, AMEYOKO, KING_ASAKUSA, KING_SHINJUKU, KIYOMIZU_KYOTO, KIYOMIZU_TOKYO, SOLAMACHI)
LIMIT = 5


class FakeNominatim:
    """跟 `service.nominatim.NominatimClient` 一樣的介面，回傳跟真的一樣格式的原始回應。"""

    def __init__(self, world=WORLD, fail: Exception | None = None):
        self.world = world
        self.fail = fail
        self.searches: list[tuple[str, str]] = []
        self.lookups: list[tuple[str, int]] = []

    def search(self, name_ja, city, kind, settlement_only=False) -> str:
        self.searches.append((name_ja, city))
        if self.fail:
            raise self.fail
        hits = [o for o in self.world if name_ja in o.names]
        return json.dumps([o.result() for o in hits[:LIMIT]], ensure_ascii=False)

    def lookup(self, osm_type, osm_id) -> str:
        self.lookups.append((osm_type, osm_id))
        hits = [o for o in self.world if (o.osm_type, o.osm_id) == (osm_type, osm_id)]
        return json.dumps([o.result() for o in hits], ensure_ascii=False)


class FakeLLM:
    """假的 `service.llm.call`：看輸出格式分辨是哪一種呼叫，回固定內容。

    每一種可以給：一個值（每次都回它）、一個 list（照順序一次回一個，用完報錯）、
    或一個函式（拿到 user 訊息，回傳值；丟例外就是這次呼叫失敗）。
    pick 沒給時挑第一筆；pick 的值是 `choice`（0 是都不是）。new_name 的值是 (name_ja, city)。
    """

    def __init__(self, line=None, note=None, pick=1, new_name=("", "")):
        self.sources = {"line": line, "note": note, "pick": pick, "new_name": new_name}
        self.calls: list[tuple[str, str]] = []   # (哪一種, user 訊息)

    def users(self, kind: str) -> list[str]:
        return [user for k, user in self.calls if k == kind]

    async def __call__(self, system, user, schema, model, effort=None):
        kind = {id(LINE_SCHEMA): "line", id(NOTE_SCHEMA): "note", id(ask_place.PICK_SCHEMA): "pick",
                id(ask_place.NEW_NAME_SCHEMA): "new_name"}[id(schema)]
        self.calls.append((kind, user))
        source = self.sources[kind]
        if callable(source):
            value = source(user)
        elif isinstance(source, list):
            value = source.pop(0)
        else:
            value = source
        if kind == "pick":
            return {"reason": "", "choice": value}
        if kind == "new_name":
            return {"name_ja": value[0], "city": value[1]}
        return json.loads(json.dumps(value))   # 每次給一份新的，呼叫的人改了也不影響下一次


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
