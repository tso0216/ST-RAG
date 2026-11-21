import asyncio

import pytest

from tests.v4.extract.fakes import build_map
from tests.v4.rag.fakes import FakeEmbedder, FakeJEV, FakeLLM, World
from v4.extract import config as extract_config
from v4.extract.local_map import LocalMap
from v4.rag import config
from v4.rag.embedding import CachedEmbedder
from v4.rag.gate import Gate
from v4.rag.nodes import Env

QUESTION = "淺草寺人多嗎"


@pytest.fixture(autouse=True)
def no_real_data(tmp_path, monkeypatch):
    """測試一律不碰真的 `data/`：資料庫、pending、本機地圖、log 的預設路徑都換到 tmp_path。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "default" / "test.db")
    monkeypatch.setattr(extract_config, "DB_PATH", tmp_path / "default" / "v4.db")
    monkeypatch.setattr(extract_config, "PENDING_DIR", tmp_path / "default" / "v4_pending")
    monkeypatch.setattr(extract_config, "LOCAL_MAP_PATH", tmp_path / "default" / "japan.duckdb")
    monkeypatch.setattr(extract_config, "LOG_DIR", tmp_path / "default" / "logs")


@pytest.fixture
def world(conn):
    return World(conn)


@pytest.fixture
def jev():
    return FakeJEV()


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def use_llm(monkeypatch):
    """把 `service.llm.call` 換成假的；回傳的函式收 `FakeLLM` 的參數，回傳那個假 llm。"""
    def install(**sources) -> FakeLLM:
        fake = FakeLLM(**sources)
        monkeypatch.setattr("service.llm.call", fake)
        return fake
    return install


@pytest.fixture
def fake_llm(use_llm):
    return use_llm()


@pytest.fixture
def env(conn, jev, embedder, fake_llm):
    """執行節點用的 Env：假的 JEV、假的 embedding、假的 LLM（整理重點）。"""
    return Env(conn, Gate(conn, jev, config.JEV_MODEL, 4), CachedEmbedder(embedder, conn),
               asyncio.Semaphore(4), QUESTION)


@pytest.fixture
def map_path(tmp_path):
    return build_map(tmp_path / "japan.duckdb")


@pytest.fixture
def local_map(map_path):
    m = LocalMap(map_path)
    yield m
    m.close()
