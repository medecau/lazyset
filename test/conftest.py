import pytest

import dataset

from .sample_data import TEST_DATA


@pytest.fixture(scope="function")
def db():
    db = dataset.connect()
    yield db
    db.executable.rollback()
    for table in db.tables:
        db[table].drop()
    db.close()


@pytest.fixture(scope="function")
def table(db):
    tbl = db["weather"]
    tbl.insert_many(TEST_DATA)
    yield tbl
