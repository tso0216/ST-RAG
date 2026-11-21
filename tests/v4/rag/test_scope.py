"""地點與範圍：區域範圍裡的地點、有洞的多邊形、周圍、名稱比對、直線距離。"""
import pytest

from v4 import place as place_table
from v4.rag import scope
from v4.rag.data_schema import ANYWHERE, AROUND, AT, Scope, Where


def test_area_contains_spots_and_smaller_areas_inside(world, conn):
    big = world.area("台東區", 35.71, 139.78, 0.05)
    small = world.area("上野", 35.712, 139.777, 0.01)
    bigger = world.area("東京都", 35.71, 139.78, 0.5)
    inside = world.place("上野公園", lat=35.715, lon=139.773)
    outside = world.place("新宿御苑", lat=35.685, lon=139.71)
    found = scope.places_in_area(conn, place_table.get(conn, big))
    assert found == {big, small, inside}
    assert outside not in found and bigger not in found


def test_area_without_boundary_is_only_itself(world, conn):
    kyoto = world.place("京都", kind="區域", lat=35.0, lon=135.7)
    world.place("清水寺", lat=35.0, lon=135.7)
    assert scope.places_in_area(conn, place_table.get(conn, kyoto)) == {kyoto}


def test_point_in_hole_is_not_inside(world, conn):
    ring = world.area("環狀區", 35.70, 139.70, 0.05, hole=0.01)
    in_hole = world.place("洞裡", lat=35.70, lon=139.70)
    in_ring = world.place("環上", lat=35.73, lon=139.70)
    assert scope.places_in_area(conn, place_table.get(conn, ring)) == {ring, in_ring}
    assert in_hole not in scope.places_in_area(conn, place_table.get(conn, ring))


def test_around_takes_spots_within_radius_and_center(world, conn):
    center = world.place("淺草寺", lat=35.7148, lon=139.7967)
    near = world.place("雷門", lat=35.7111, lon=139.7964)          # 約 0.4 公里
    far = world.place("晴空塔", lat=35.7101, lon=139.8107)          # 約 1.4 公里
    area = world.area("淺草", 35.712, 139.795, 0.01)
    found = scope.around(conn, place_table.get(conn, center), 1.0)
    assert found == {center, near}
    assert far not in found and area not in found


def test_scope_places(world, conn):
    spot = world.place("淺草寺", lat=35.7148, lon=139.7967)
    area = world.area("台東區", 35.71, 139.78, 0.05)
    assert scope.scope_places(conn, Scope(ANYWHERE)) is None
    assert scope.scope_places(conn, Scope(AT, Where(name_zh="淺草寺", place_id=spot))) == {spot}
    assert scope.scope_places(conn, Scope(AT, Where(name_zh="台東區", place_id=area))) == {area, spot}
    assert scope.scope_places(conn, Scope(AROUND, Where(name_zh="淺草寺", place_id=spot), 0.5)) == {spot}


def test_name_matching_uses_exact_zh_name_and_zh_alias(world, conn):
    pid = world.place("淺草寺", "浅草寺", aliases=("淺草觀音寺",))
    place_table.add_alias(conn, pid, name_ja="あさくさでら")
    names = scope.zh_names(conn, {pid})
    assert names == {"淺草寺": pid, "淺草觀音寺": pid}
    assert "浅草寺" not in names and "淺草" not in names


def test_distance():
    # 淺草寺 → 晴空塔
    assert scope.km(35.7148, 139.7967, 35.7101, 139.8107) == pytest.approx(1.4, abs=0.05)
    assert scope.km(35.0, 135.0, 35.0, 135.0) == 0
    # 緯度差 1 度約 111.2 公里
    assert scope.distance(35.0, 135.0, 36.0, 135.0) == pytest.approx(111.2, abs=0.1)
