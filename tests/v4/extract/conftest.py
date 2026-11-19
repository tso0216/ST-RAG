import pytest

from tests.v4.extract.fakes import FakeLLM, build_map
from v4.extract import config
from v4.extract.local_map import LocalMap


@pytest.fixture(autouse=True)
def no_real_data(tmp_path, monkeypatch):
    """測試一律不碰真的 `data/`：資料庫、tsv、pending、本機地圖、log 的預設路徑都換到 tmp_path。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "default" / "v4.db")
    monkeypatch.setattr(config, "UNRESOLVED_PATH", tmp_path / "default" / "place_unresolved.tsv")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "default" / "v4_pending")
    monkeypatch.setattr(config, "LOCAL_MAP_PATH", tmp_path / "default" / "japan.duckdb")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "default" / "logs")


@pytest.fixture
def map_path(tmp_path):
    """小型本機地圖（`fakes.WORLD`）的路徑。"""
    return build_map(tmp_path / "japan.duckdb")


@pytest.fixture
def local_map(map_path):
    m = LocalMap(map_path)
    yield m
    m.close()


@pytest.fixture
def use_llm(monkeypatch):
    """把 `service.llm.call` 換成假的；回傳的函式收 `FakeLLM` 的參數，回傳那個假 llm。"""
    def install(**sources) -> FakeLLM:
        fake = FakeLLM(**sources)
        monkeypatch.setattr("service.llm.call", fake)
        return fake
    return install
