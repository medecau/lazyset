import pytest

import lazyset

from .sample_data import TEST_DATA


@pytest.fixture(scope="function")
def db():
    db = lazyset.connect()
    yield db
    db._executable.rollback()
    for table in db.tables:
        db[table].drop()
    db.close()


@pytest.fixture(scope="function")
def table(db):
    tbl = db["weather"]
    tbl.insert(TEST_DATA)
    yield tbl
