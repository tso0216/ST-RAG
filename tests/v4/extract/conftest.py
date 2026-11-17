import pytest

from tests.v4.extract.fakes import FakeLLM
from v4.extract import config


@pytest.fixture(autouse=True)
def no_real_data(tmp_path, monkeypatch):
    """測試一律不碰真的 `data/`：資料庫、tsv、pending 的預設路徑都換到 tmp_path。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "default" / "v4.db")
    monkeypatch.setattr(config, "UNRESOLVED_PATH", tmp_path / "default" / "place_unresolved.tsv")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "default" / "v4_pending")


@pytest.fixture
def use_llm(monkeypatch):
    """把 `service.llm.call` 換成假的；回傳的函式收 `FakeLLM` 的參數，回傳那個假 llm。"""
    def install(**sources) -> FakeLLM:
        fake = FakeLLM(**sources)
        monkeypatch.setattr("service.llm.call", fake)
        return fake
    return install
