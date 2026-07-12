import os
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from dataset import connect
from dataset.database import Database
from dataset.util import ResultIter

from .sample_data import TEST_CITY_1, TEST_DATA

# Backend detected at collection time so it can gate skipif marks, which are
# evaluated before the ``db`` fixture exists.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite://")
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_MYSQL = "mysql" in DATABASE_URL
IS_POSTGRES = DATABASE_URL.startswith("postgres")


def test_valid_database_url(db):
    assert db.url


def test_database_repr(db):
    assert repr(db) == f"<Database({db.url})>"


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


def test_flush_tables_concurrent_with_get_table(tmp_path):
    # _flush_tables iterated the shared self._tables without the lock; a
    # concurrent get_table inserting a new wrapper (under the lock) resized
    # the dict mid-iteration, raising "dictionary changed size during
    # iteration" on the rolling-back thread. A tiny thread-switch interval
    # makes the overlap land reliably. A file-backed DB (not :memory:) keeps
    # the flusher thread's connections usable at close() teardown.
    db = connect(f"sqlite:///{tmp_path / 'flush.db'}")
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    try:
        for i in range(300):
            db[f"seed_{i}"]  # populate _tables so each flush iterates real work

        errors: list[Exception] = []
        stop = threading.Event()

        def flusher():
            try:
                while not stop.is_set():
                    db.begin()
                    db.rollback()  # calls _flush_tables, iterating _tables
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def adder():
            try:
                for i in range(4000):
                    db[f"new_name_{i}"]
            finally:
                stop.set()

        t1 = threading.Thread(target=flusher)
        t2 = threading.Thread(target=adder)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not errors, errors
    finally:
        sys.setswitchinterval(old_interval)
        db.close()


def test_rollback_invalidates_column_cache(db):
    tbl = db["rollback_cache"]
    tbl.insert({"a": 1})
    assert tbl.has_column("a")  # populates the _columns cache

    # Simulate the state after a rolled-back in-transaction ADD COLUMN: the
    # real schema no longer has "a", but the wrapper still has it cached. On
    # PostgreSQL rollback() would undo the ADD COLUMN; here we drop it
    # out-of-band and commit so the DB genuinely lacks the column.
    db.query("ALTER TABLE rollback_cache DROP COLUMN a")
    db.executable.commit()

    # _flush_tables (via rollback) nulled only _table, leaving _columns
    # populated; _column_keys short-circuits on a non-None cache, so
    # has_column kept returning the stale True. It must re-reflect now.
    db.begin()
    db.rollback()
    assert tbl.has_column("a") is False


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


def test_result_iter_attributes():
    db = connect()
    conn = db.executable
    rp = conn.execute(text("SELECT 1 AS a, 2 AS b"))
    it = ResultIter(rp, connection=conn)
    assert it.result_proxy is rp
    assert it.keys == ["a", "b"]
    assert dict(next(it)) == {"a": 1, "b": 2}
    assert it._conn is conn
    it.close()
    it.close()  # closing twice must not raise
    db.close()


def test_result_iter_closed_result():
    db = connect()
    # DDL statements return a result proxy that raises ResourceClosedError
    # on .keys(); ResultIter must fall back to an empty iterator.
    it = db.query("CREATE TABLE result_iter_ddl (id INTEGER)")
    assert it.keys == []
    assert list(it) == []
    db.close()


@pytest.mark.parametrize("key", ["schema", "searchpath"])
def test_schema_extracted_from_url(key):
    db = connect(f"sqlite:///:memory:/?{key}=myschema")
    assert db.schema == "myschema"
    db.close()


def test_dialect_flags(db):
    # Derived from DATABASE_URL so the suite passes on every backend.
    assert db.is_sqlite is IS_SQLITE
    assert db.is_postgres is IS_POSTGRES
    assert db.is_mysql is IS_MYSQL


def test_sqlite_wal_mode_for_file_db():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = connect(f"sqlite:///{f.name}")
        mode = list(db.query("PRAGMA journal_mode").next().values())[0]
        assert mode.lower() == "wal"
        db.close()

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = connect(f"sqlite:///{f.name}", sqlite_wal_mode=False)
        mode = list(db.query("PRAGMA journal_mode").next().values())[0]
        assert mode.lower() != "wal"
        db.close()


def test_constructor_defaults_direct():
    # Bypass connect()'s explicit kwargs to exercise Database.__init__'s
    # own defaults directly.
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(f"sqlite:///{f.name}")
        assert db.ensure_schema is True
        mode = list(db.query("PRAGMA journal_mode").next().values())[0]
        assert mode.lower() == "wal"
        db.close()


def test_constructor_kwargs_forwarded(db):
    d = Database("sqlite://", engine_kwargs={"echo": True})
    assert d.engine.echo is True
    d.close()

    tbl = db.get_table("no_increment", primary_increment=False)
    assert tbl.table.c["id"].autoincrement is False


def test_text_primary_type_rejected(db):
    with pytest.raises(
        AssertionError,
        match=r"^Text-based primary_type support is dropped, use db\.types\.$",
    ):
        db.create_table("bad_primary_type", primary_type="str")


def test_connect_forwards_kwargs():
    db = connect("sqlite:///:memory:", schema="myschema", ensure_schema=False)
    assert db.schema == "myschema"
    assert db.ensure_schema is False
    db.close()

    db = connect(engine_kwargs={"echo": True})
    assert db.engine.echo is True
    db.close()

    db = connect(row_type=dict)
    row = db.query("SELECT 1 AS a").next()
    assert type(row) is dict
    db.close()

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = connect(f"sqlite:///{f.name}")
        mode = list(db.query("PRAGMA journal_mode").next().values())[0]
        assert mode.lower() == "wal"
        db.close()

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        # -2000 is SQLite's own default cache_size, so it wouldn't
        # distinguish "the statement ran" from "it didn't"; pick a value
        # that differs from the default.
        db = connect(
            f"sqlite:///{f.name}", on_connect_statements=["PRAGMA cache_size=-4000"]
        )
        cache_size = list(db.query("PRAGMA cache_size").next().values())[0]
        assert cache_size == -4000
        db.close()


def test_connect_database_url_env_fallback(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{f.name}")
        db = connect()
        assert db.url == f"sqlite:///{f.name}"
        db.close()


def test_nested_transaction_rollback(db):
    tbl = db["nested_tx"]
    db.begin()
    db.begin()
    tbl.insert({"a": 1})
    db.commit()
    db.rollback()
    assert tbl.count() == 0


def test_commit_rollback_without_transaction_noop(db):
    # Neither call has ever begun a transaction; both must be no-ops.
    db.commit()
    db.rollback()


def test_close_resets_tables_and_is_idempotent():
    db = connect()
    db["close_idempotent_test"].insert({"a": 1})
    assert db._tables != {}
    db.close()
    assert db._tables == {}
    db.close()  # closing twice must not raise


def test_close_atomic_with_concurrent_use(tmp_path):
    # close() cleared connections under the lock but disposed the engine and
    # nulled it OUTSIDE the lock. A concurrent executable() acquiring the lock
    # in that gap saw engine != None and built a connection on a to-be-disposed
    # engine (orphaned). Pausing inside dispose() forces the window.
    db = connect(f"sqlite:///{tmp_path / 'close.db'}")
    db["t"].insert({"a": 1})

    original_dispose = db.engine.dispose
    in_dispose = threading.Event()
    resume = threading.Event()

    def slow_dispose(*args, **kwargs):
        in_dispose.set()
        resume.wait(timeout=5)
        return original_dispose(*args, **kwargs)

    db.engine.dispose = slow_dispose

    result = {}

    def use():
        try:
            result["conn"] = db.executable
        except RuntimeError as e:
            result["error"] = str(e)

    closer = threading.Thread(target=db.close)
    closer.start()
    in_dispose.wait(timeout=5)  # close() is now mid-teardown

    user = threading.Thread(target=use)
    user.start()
    time.sleep(0.2)  # let use() acquire (buggy) or block on (fixed) the lock
    resume.set()

    closer.join()
    user.join()

    # With teardown atomic under the lock, executable() runs either fully
    # before close (valid conn) or fully after (clean RuntimeError) — never on
    # a half-torn-down engine.
    assert "conn" not in result, "executable() got a connection from a closing DB"
    assert "error" in result
    assert db.connections == {}


def test_connection_closed_after_commit(db):
    tbl = db["conn_closed_test"]
    db.begin()
    tbl.insert({"a": 1})
    conn = db.executable
    db.commit()
    assert conn.closed
