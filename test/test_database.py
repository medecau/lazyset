import os
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from dataset import connect

from .sample_data import TEST_CITY_1, TEST_DATA

# Backend detected at collection time so it can gate skipif marks, which are
# evaluated before the ``db`` fixture exists.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite://")
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_MYSQL = "mysql" in DATABASE_URL


def test_valid_database_url(db):
    assert db.url


def test_database_url_query_string(db):
    db = connect("sqlite:///:memory:/?cached_statements=1")
    assert "cached_statements" in db.url, db.url


def test_tables(db, table):
    assert "weather" in db.tables, db.tables


def test_contains(db, table):
    assert "weather" in db, db.tables


def test_create_table(db):
    table = db["foo"]
    assert db.has_table(table.table.name)
    assert len(table.table.columns) == 1, table.table.columns
    assert "id" in table.table.c, table.table.c


def test_create_table_no_ids(db):
    table = db.create_table("foo_no_id", primary_id=False)
    assert table.table.name == "foo_no_id"
    assert len(table.table.columns) == 0, table.table.columns


def test_create_table_custom_id1(db):
    pid = "string_id"
    table = db.create_table("foo2", pid, db.types.string(255))
    assert db.has_table(table.table.name)
    assert len(table.table.columns) == 1, table.table.columns
    assert pid in table.table.c, table.table.c
    table.insert({pid: "foobar"})
    assert table.find_one(string_id="foobar")[pid] == "foobar"


def test_create_table_custom_id2(db):
    pid = "string_id"
    table = db.create_table("foo3", pid, db.types.string(50))
    assert db.has_table(table.table.name)
    assert len(table.table.columns) == 1, table.table.columns
    assert pid in table.table.c, table.table.c

    table.insert({pid: "foobar"})
    assert table.find_one(string_id="foobar")[pid] == "foobar"


def test_create_table_custom_id3(db):
    pid = "int_id"
    table = db.create_table("foo4", primary_id=pid)
    assert db.has_table(table.table.name)
    assert len(table.table.columns) == 1, table.table.columns
    assert pid in table.table.c, table.table.c

    table.insert({pid: 123})
    table.insert({pid: 124})
    assert table.find_one(int_id=123)[pid] == 123
    assert table.find_one(int_id=124)[pid] == 124


def test_create_table_shorthand1(db):
    pid = "int_id"
    table = db.get_table("foo5", pid)
    assert len(table.table.columns) == 1, table.table.columns
    assert pid in table.table.c, table.table.c

    table.insert({"int_id": 123})
    table.insert({"int_id": 124})
    assert table.find_one(int_id=123)["int_id"] == 123
    assert table.find_one(int_id=124)["int_id"] == 124


def test_duplicate_primary_key_raises(db):
    pid = "int_id"
    table = db.create_table("dup_pk", primary_id=pid)
    table.insert({pid: 123})
    with pytest.raises(IntegrityError):
        table.insert({pid: 123})
    db.executable.rollback()


def test_create_table_shorthand2(db):
    pid = "string_id"
    table = db.get_table("foo6", primary_id=pid, primary_type=db.types.string(255))
    assert len(table.table.columns) == 1, table.table.columns
    assert pid in table.table.c, table.table.c

    table.insert({"string_id": "foobar"})
    assert table.find_one(string_id="foobar")["string_id"] == "foobar"


def test_with(db, table):
    init_length = len(table)
    with pytest.raises(ValueError), db:
        table.insert(
            {
                "date": datetime(2011, 1, 1),
                "temperature": 1,
                "place": "tmp_place",
            }
        )
        raise ValueError()
    assert len(table) == init_length


@pytest.mark.skipif(
    IS_MYSQL,
    reason="MySQL casts implicitly, so it does not raise",
)
def test_invalid_values(db, table):
    with pytest.raises(SQLAlchemyError):
        table.insert({"date": True, "temperature": "wrong_value", "place": "tmp_place"})


def test_load_table(db, table):
    tbl = db.load_table("weather")
    assert tbl.table.name == table.table.name


def test_query(db, table):
    r = db.query("SELECT COUNT(*) AS num FROM weather").next()
    assert r["num"] == len(TEST_DATA), r


def test_table_cache_updates(db):
    tbl1 = db.get_table("people")
    data = OrderedDict([("first_name", "John"), ("last_name", "Smith")])
    tbl1.insert(data)
    data["id"] = 1
    tbl2 = db.get_table("people")
    assert dict(tbl2.all().next()) == dict(data), (tbl2.all().next(), data)


def test_thread_connections_released():
    """Connections should be released after a transaction ends (issue #425)."""
    # Use a file-based SQLite database — in-memory SQLite uses
    # SingletonThreadPool which doesn't support real multi-threading.
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = connect(f"sqlite:///{f.name}")
        # Create the table on the main thread to avoid schema races.
        db["thread_test"].insert({"value": 0})

        def insert_in_thread() -> None:
            with db:
                db["thread_test"].insert({"value": 1})

        threads = []
        for _ in range(5):
            t = threading.Thread(target=insert_in_thread)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # After all threads finish and their transactions commit,
        # their connections should have been released. Only the main
        # thread's connection (from the setup insert) may remain.
        assert len(db.connections) <= 1, (
            f"Expected at most 1 connection, got {len(db.connections)}"
        )
        db.close()


def test_query_with_params(db, table):
    rows = list(db.query("SELECT * FROM weather WHERE place = :p", p=TEST_CITY_1))
    assert len(rows) == 3, rows


def test_query_step(db, table):
    # _step of 0 disables chunked fetching (treated as None internally).
    rows = list(db.query("SELECT * FROM weather", _step=0))
    assert len(rows) == len(TEST_DATA), rows


def test_explicit_rollback(db):
    tbl = db["explicit_rollback"]
    tbl.insert({"a": 1})
    db.begin()
    tbl.insert({"a": 2})
    db.rollback()
    assert tbl.count() == 1, tbl.count()


def test_autobegin_commit(db):
    tbl = db["autobegin_commit"]
    tbl.insert({"a": 1})
    # A read autobegins a transaction on the shared connection; the following
    # begin() then nests on top of it (tracked as True rather than a new tx).
    list(tbl.find())
    db.begin()
    tbl.insert({"a": 2})
    db.commit()
    assert tbl.count() == 2, tbl.count()


def test_closed_database_raises():
    db = connect()
    db["closed_test"].insert({"a": 1})
    db.close()
    with pytest.raises(RuntimeError):
        _ = db.executable


def test_contains_invalid_name(db):
    # Names that fail normalization are simply "not contained", not errors.
    assert "" not in db
    assert "   " not in db


def test_load_table_caches(db, table):
    # Evict from the wrapper cache so load_table must reflect it afresh.
    db._tables.pop("weather", None)
    tbl = db.load_table("weather")
    assert tbl.exists
    assert "weather" in db._tables


def test_connect_no_ensure_schema():
    db = connect(ensure_schema=False)
    # With schema generation off, get_table routes through load_table.
    tbl = db.get_table("any_table")
    assert tbl.name == "any_table"
    db.close()
