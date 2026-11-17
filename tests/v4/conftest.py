import pytest

from v4 import db


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()
