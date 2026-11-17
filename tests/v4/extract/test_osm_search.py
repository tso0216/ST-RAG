"""OSM 查詢：把 Nominatim 的回應整理成候選。"""
import json

from service.nominatim import OSM_PREFIX
from tests.v4.extract.fakes import FakeNominatim, SENSOJI, UENO
from v4.extract.osm_search import OsmSearch


def test_search_returns_candidates_with_name_type_address_and_coordinates():
    client = FakeNominatim()

    [c] = OsmSearch(client).search("浅草寺", "東京")

    assert client.searches == [("浅草寺", "東京")]
    assert (c.osm_type, c.osm_id, c.name, c.kind) == ("way", 1, "浅草寺", "景點")
    assert c.type == "amenity:yes" and c.address == "浅草寺, 台東区, 東京都, 日本"
    assert (c.lat, c.lon) == (SENSOJI.lat, SENSOJI.lon)
    assert c.boundary is None


def test_administrative_result_is_an_area_with_its_boundary():
    [c] = OsmSearch(FakeNominatim()).search("上野")

    assert c.kind == "區域"
    assert c.boundary["type"] == "Polygon"


def test_search_returns_at_most_five_and_nothing_when_not_found():
    many = tuple(type(SENSOJI)("node", i, ("同名",), "amenity", 35.0, 139.0) for i in range(1, 9))
    osm = OsmSearch(FakeNominatim(world=many))

    assert len(osm.search("同名")) == 5
    assert osm.search("沒有這個地方") == []


def test_lookup_by_osm_object_id():
    client = FakeNominatim()
    osm = OsmSearch(client)

    assert osm.lookup("relation", UENO.osm_id).name == "上野"
    assert osm.lookup("way", 999) is None
    assert client.lookups == [("relation", 100), ("way", 999)]


def test_result_without_osm_object_is_ignored():
    class NoObject:
        def search(self, *_):
            return json.dumps([{"name": "郵遞區號", "lat": "35", "lon": "139", "display_name": "111-0032"}])

    assert OsmSearch(NoObject()).search("111-0032") == []


def test_lookup_prefix_covers_the_three_osm_types():
    assert OSM_PREFIX == {"node": "N", "way": "W", "relation": "R"}
