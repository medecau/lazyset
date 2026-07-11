import logging
import os
import threading
import time
import warnings
from datetime import datetime

import pytest
from sqlalchemy import Float
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import Column
from sqlalchemy.sql.dml import Delete
from sqlalchemy.types import BIGINT, TEXT, Unicode

from dataset import DatasetError, QueryError, chunked, connect
from dataset.util import index_name

from .sample_data import TEST_CITY_1, TEST_CITY_2, TEST_DATA

# Backend detected at collection time so it can gate skipif marks, which are
# evaluated before the ``db`` fixture exists.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite://")
IS_SQLITE = DATABASE_URL.startswith("sqlite")


def test_insert(table):
    assert len(table) == len(TEST_DATA), len(table)
    last_id = table.insert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"}
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    assert table.find_one(id=last_id)["place"] == "Berlin"


def test_insert_ignore(table):
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_ignore_single_sync_columns_pass(table):
    # insert_ignore already ran _sync_columns on the row; delegating to
    # insert(row, ensure=False) redundantly re-ran it. has_column() is
    # called once per row column inside _sync_columns, so a non-key column
    # (not touched by create_index/count's own has_column calls) must be
    # checked exactly once, not twice.
    original_has_column = table.has_column
    calls = []

    def spy(column):
        calls.append(column)
        return original_has_column(column)

    table.has_column = spy
    try:
        table.insert_ignore(
            {"date": datetime(2011, 1, 5), "temperature": 3, "place": "NewPlace"},
            ["place"],
        )
    finally:
        del table.has_column

    assert calls.count("date") == 1, calls
    assert calls.count("temperature") == 1, calls


def test_insert_ignore_missing_key_column_raises(table):
    # Previously, a `keys` column absent from both the row and the table
    # compiled the existence check to `false()` (always 0 matches), so this
    # silently inserted a duplicate row on every call. create_index() now
    # raises before that check ever runs.
    before = len(table)
    with pytest.raises(DatasetError, match=r"^No such column: nonexistent_col$"):
        table.insert_ignore({"place": "Berlin"}, ["nonexistent_col"])
    assert len(table) == before


def test_insert_ignore_missing_key_column_raises_ensure_false(table):
    # With ensure=False the create_index guard is skipped, so the absent key
    # column previously slipped through: _keys_to_args defaulted it to None,
    # _args_to_clause compiled it to false(), count was 0, and the row was
    # inserted as a silent duplicate on every call. _keys_to_args must raise.
    before = len(table)
    with pytest.raises(DatasetError, match=r"^No such column: nonexistent_col$"):
        table.insert_ignore({"place": "Berlin"}, ["nonexistent_col"], ensure=False)
    assert len(table) == before


def test_update_missing_table_column_raises(table):
    # A key column absent from the table (not merely from the row) used to
    # compile to false() and make update() silently return 0. It must raise.
    with pytest.raises(DatasetError, match=r"^No such column: nonexistent_col$"):
        table.update({"place": "Berlin"}, ["nonexistent_col"], ensure=False)


def test_insert_ignore_all_key(table):
    for _i in range(0, 4):
        table.insert_ignore(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_json(table):
    info = {"currency": "EUR", "language": "German", "population": 3292365}
    last_id = table.insert(
        {
            "date": datetime(2011, 1, 2),
            "temperature": -10,
            "place": "Berlin",
            "info": info,
        }
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    stored = table.find_one(id=last_id)
    assert stored["place"] == "Berlin"
    # The nested dict round-trips through the JSON column.
    assert stored["info"] == info, stored["info"]


def test_upsert(table):
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_single_column(db):
    table = db["banana_single_col"]
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)


def test_upsert_all_key(table):
    assert len(table) == len(TEST_DATA), len(table)
    for _i in range(0, 2):
        table.upsert(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_id(db):
    table = db["banana_with_id"]
    data = {"id": 10, "title": "I am a banana!"}
    table.upsert(data, ["id"])
    assert len(table) == 1, len(table)


def test_write_method_return_values(db, table):
    pk = table.insert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"}
    )
    assert pk == len(TEST_DATA) + 1

    # No primary key column: insert() returns True (empty-tuple branch).
    no_pk_table = db.create_table("no_pk_writes", primary_id=False)
    assert no_pk_table.insert({"a": 1}) is True

    # insert_ignore() on a matching existing row returns False.
    assert (
        table.insert_ignore(
            {"date": datetime(2011, 1, 1), "temperature": 6, "place": TEST_CITY_1},
            ["place"],
        )
        is False
    )

    # insert_ignore() on a genuinely new row returns the inserted primary key.
    new_pk = table.insert_ignore(
        {"date": datetime(2011, 1, 6), "temperature": 2, "place": "NewCity"},
        ["place"],
    )
    assert new_pk == len(TEST_DATA) + 2

    # No primary key column: insert_ignore() also returns True for a new row.
    assert no_pk_table.insert_ignore({"a": 2}, ["a"]) is True

    # upsert() that matches and updates existing row(s) returns True.
    assert (
        table.upsert(
            {"date": datetime(2011, 1, 1), "temperature": 99, "place": TEST_CITY_1},
            ["place"],
        )
        is True
    )

    # delete() on a table that was never created returns 0 without erroring.
    missing = db.load_table("truly_missing_delete_target")
    assert missing.delete() == 0


def test_upsert_many_keys_targeting(db):
    # An empty keys list would match *every* existing row instead of just
    # the intended one, turning every upsert after the first into an update.
    tbl = db["upsert_many_targeting"]
    tbl.upsert_many([{"id": 1}, {"id": 2}], "id")
    assert len(tbl) == 2


def test_update_while_iter(table):
    for row in table:
        row["foo"] = "bar"
        table.update(row, ["place", "date"])
    assert len(table) == len(TEST_DATA), len(table)
    # Every row's new "foo" column was persisted.
    assert table.count(foo="bar") == len(TEST_DATA), table.count(foo="bar")


def test_cased_column_collapse(db):
    tbl = db["cased_column_names"]
    tbl.insert({"place": "Berlin"})
    tbl.insert({"Place": "Berlin"})
    tbl.insert({"PLACE ": "Berlin"})
    # id + place: the three cased/spaced variants collapse to one column.
    assert len(tbl.columns) == 2, tbl.columns


def test_case_insensitive_lookup(db):
    tbl = db["cased_column_names"]
    tbl.insert({"place": "Berlin"})
    tbl.insert({"Place": "Berlin"})
    tbl.insert({"PLACE ": "Berlin"})
    assert len(list(tbl.find(Place="Berlin"))) == 3
    assert len(list(tbl.find(place="Berlin"))) == 3
    assert len(list(tbl.find(PLACE="Berlin"))) == 3


def test_columns_differing_by_space_not_conflated(db):
    # "full name" and "fullname" are distinct columns; the old space-collapse
    # in normalize_column_key merged them, hiding one behind the other.
    tbl = db["space_cols"]
    tbl.create_column("full name", db.types.text)
    tbl.create_column("fullname", db.types.text)
    assert tbl.has_column("full name")
    assert tbl.has_column("fullname")
    assert "full name" in tbl.columns
    assert "fullname" in tbl.columns
    assert len(tbl.columns) == 3, tbl.columns  # id + both


@pytest.mark.parametrize("bad_name", [None, "", "-", "foo.bar"])
def test_invalid_column_names(db, bad_name):
    tbl = db["weather"]
    with pytest.raises(ValueError):
        tbl.insert({bad_name: "banana"})


def test_delete_positional_raises(table):
    # Passing a dict positionally is a misuse of the API.
    with pytest.raises(ArgumentError):
        table.delete({"place": "Berlin"})
    assert len(table) == len(TEST_DATA), len(table)


def test_delete_filtered(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    assert table.delete(place="Berlin") == 1, "should return 1"
    assert len(table) == len(TEST_DATA), len(table)


def test_delete_all(table):
    assert table.delete() == len(TEST_DATA), "should return non zero"
    assert len(table) == 0, len(table)


def test_repr(table):
    assert repr(table) == "<Table(weather)>", (
        "the representation should be <Table(weather)>"
    )


def test_delete_nonexist_entry(table):
    assert table.delete(place="Berlin") == 0, "entry not exist, should fail to delete"


def test_delete_returns_count_without_sane_rowcount(table, monkeypatch):
    # Dead on SQLite/PG/MySQL (all report sane rowcount); parity with
    # update(). Stub the dialect flag off and hand the DELETE a result whose
    # rowcount is the unreliable -1: delete() must fall back to the pre-delete
    # count rather than return the bogus rowcount.
    monkeypatch.setattr(table.db.executable.dialect, "supports_sane_rowcount", False)

    conn = table.db.executable
    original_execute = conn.execute

    class _NoSaneResult:
        rowcount = -1

        def supports_sane_rowcount(self):
            return False

    def spy(statement, *args, **kwargs):
        rp = original_execute(statement, *args, **kwargs)
        return _NoSaneResult() if isinstance(statement, Delete) else rp

    conn.execute = spy
    try:
        assert table.delete(place=TEST_CITY_1) == 3
    finally:
        del conn.execute

    # The delete still happened via the real execute; only the reported count
    # was faked.
    assert table.count(place=TEST_CITY_1) == 0


def test_find_one(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    d = table.find_one(place="Berlin")
    assert d["temperature"] == -10, d
    d = table.find_one(place="Atlantis")
    assert d is None, d


def test_find_one_positional_clause(table):
    row = table.find_one(table.table.columns.temperature > 4)
    assert row["temperature"] > 4


def test_count(table):
    assert len(table) == len(TEST_DATA), len(table)
    length = table.count(place=TEST_CITY_1)
    assert length == 3, length


def test_find_by_filter(table):
    ds = list(table.find(place=TEST_CITY_1))
    assert len(ds) == 3, ds


def test_find_pagination(table):
    ds = list(table.find(place=TEST_CITY_1, _limit=2))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _step=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=1, _step=2))
    assert len(ds) == 1, ds
    ds = list(table.find(_step=2))
    assert len(ds) == len(TEST_DATA), ds
    ds = list(table.find(place=TEST_CITY_1, _offset=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _offset=2))
    assert len(ds) == 1, ds


def test_find_step_zero(table):
    # _step=0 disables chunked fetching (treated as None internally).
    assert len(list(table.find(_step=0))) == len(TEST_DATA)


def test_find_order_by(table):
    ds = list(table.find(order_by=["temperature"]))
    assert ds[0]["temperature"] == -1, ds
    ds = list(table.find(order_by=["-temperature"]))
    assert ds[0]["temperature"] == 8, ds


def test_find_and_order_by_missing_or_none_column(db, table):
    # Filtering/ordering on a column that doesn't exist is silently ignored.
    assert list(table.find(nonexistent_col="x")) == []
    assert [dict(r) for r in table.find(order_by="nonexistent_col")] == [
        dict(r) for r in table.find()
    ]

    # A None entry in an order_by list is skipped, not an error.
    assert [dict(r) for r in table.find(order_by=[None, "temperature"])] == [
        dict(r) for r in table.find(order_by="temperature")
    ]

    # An invalid column *followed by* a valid one: the loop must "continue"
    # past it, not "break" out and drop the remaining orderings.
    assert [
        dict(r) for r in table.find(order_by=["nonexistent_col", "temperature"])
    ] == [dict(r) for r in table.find(order_by="temperature")]

    # A column name starting with "X" exercises lstrip("-") specifically
    # (stripping leading '-' characters), not a substring/set mutation.
    tbl2 = db["xtag_order"]
    tbl2.insert({"Xtag": 1})
    tbl2.insert({"Xtag": 2})
    ds = list(tbl2.find(order_by="-Xtag"))
    assert [r["Xtag"] for r in ds] == [2, 1]


def test_find_no_engine_raises():
    db = connect()
    tbl = db["find_no_engine"]
    tbl.insert({"a": 1})
    db.close()
    with pytest.raises(
        RuntimeError, match=r"^Cannot run queries when no engine is available\.$"
    ):
        list(tbl.find())


def test_find_clause_expression(table):
    ds = list(table.find(table.table.columns.temperature > 4))
    assert len(ds) == 3, ds


@pytest.mark.parametrize(
    "filt,expected",
    [
        ({"place": {"like": "%lw%"}}, 3),
        ({"place": {"notlike": "%lw%"}}, 3),
        ({"place": {"ilike": "%LwAy"}}, 3),
        ({"place": {"notilike": "%LwAy"}}, 3),
        ({"temperature": {">": 5}}, 2),
        ({"temperature": {">=": 5}}, 3),
        ({"temperature": {"<": 0}}, 1),
        ({"temperature": {"<=": 0}}, 2),
        ({"temperature": {"!=": -1}}, 5),
        ({"temperature": {"between": [5, 8]}}, 3),
        ({"temperature": {"in": [6, 8]}}, 2),
        ({"temperature": {"notin": [-1, 0, 1]}}, 3),
        ({"place": {"=": TEST_CITY_2}}, 3),
    ],
)
def test_find_operator(table, filt, expected):
    ds = list(table.find(**filt))
    assert len(ds) == expected, ds


@pytest.mark.parametrize(
    "filt,expected",
    [
        ({"temperature": {"gt": 5}}, 2),
        ({"temperature": {"gte": 5}}, 3),
        ({"temperature": {"lt": 0}}, 1),
        ({"temperature": {"lte": 0}}, 2),
        ({"place": {"is": TEST_CITY_2}}, 3),
        ({"place": {"==": TEST_CITY_2}}, 3),
        ({"temperature": {"not": -1}}, 5),
        ({"temperature": {"<>": -1}}, 5),
        ({"temperature": {"..": [5, 8]}}, 3),
    ],
)
def test_find_operator_aliases(table, filt, expected):
    # Every operator has a word/symbol alias; each must behave identically
    # to its canonical form from test_find_operator above.
    ds = list(table.find(**filt))
    assert len(ds) == expected, ds


@pytest.mark.parametrize(
    "filt,expected_message",
    [
        (
            {"temperature": {"in": 5}},
            r"^'in' filter requires a list, got <class 'int'>$",
        ),
        (
            {"temperature": {"notin": 5}},
            r"^'notin' filter requires a list, got <class 'int'>$",
        ),
        (
            {"temperature": {"between": [1]}},
            r"^'between' filter requires a list of two values$",
        ),
        (
            {"temperature": {"between": 5}},
            r"^'between' filter requires a list of two values$",
        ),
        ({"place": {"startswith": 5}}, r"^'startswith' filter requires a string$"),
        ({"place": {"endswith": 5}}, r"^'endswith' filter requires a string$"),
    ],
)
def test_find_operator_invalid_value(table, filt, expected_message):
    with pytest.raises(QueryError, match=expected_message):
        list(table.find(**filt))


def test_find_large_in_list(table):
    # A large IN list emits one bind param per element, blowing SQLite's
    # SQLITE_LIMIT_VARIABLE_NUMBER ("too many SQL variables"); the values
    # must be rendered inline instead.
    ids = list(range(40000))
    rows = list(table.find(id=ids))
    assert len(rows) == len(TEST_DATA), len(rows)
    # notin over a large list must inline too.
    assert list(table.find(id={"notin": ids})) == []


def test_find_unknown_operator_raises(table):
    # A typo'd/unrecognized operator must raise, not silently match zero rows.
    with pytest.raises(QueryError, match=r"^Unrecognized operator: contains$"):
        list(table.find(place={"contains": "x"}))


def _prefix_table(db):
    t = db["prefix_test"]
    t.insert({"org": "acme"})
    t.insert({"org": "acme_labs"})
    t.insert({"org": "other"})
    t.insert({"org": "admin"})
    return t


def test_startswith_endswith_basic(db):
    t = _prefix_table(db)
    rows = list(t.find(org={"startswith": "acme"}))
    assert len(rows) == 2, rows
    rows = list(t.find(org={"endswith": "labs"}))
    assert len(rows) == 1, rows


def test_startswith_endswith_metachars_escaped_in_find(db):
    t = _prefix_table(db)
    # LIKE metacharacters must be escaped (CVE-style regression test).
    assert len(list(t.find(org={"startswith": "%"}))) == 0
    assert len(list(t.find(org={"startswith": "_"}))) == 0
    assert len(list(t.find(org={"endswith": "%"}))) == 0


def test_startswith_endswith_metachars_escaped_in_delete(db):
    t = _prefix_table(db)
    t.delete(org={"startswith": "%"})
    assert t.count() == 4, "delete with % startswith should not remove rows"


def test_streamed_update(table):
    ds = list(table.find(place=TEST_CITY_1, _streamed=True, _step=1))
    assert len(ds) == 3, len(ds)
    for row in table.find(place=TEST_CITY_1, _streamed=True, _step=1):
        row["temperature"] = -1
        table.update(row, ["id"])
    # The streamed updates were persisted.
    assert table.count(place=TEST_CITY_1, temperature=-1) == 3


def test_distinct_single_column(table):
    x = list(table.distinct("place"))
    assert len(x) == 2, x


def test_distinct_multi_column(table):
    x = list(table.distinct("place", "date"))
    assert len(x) == 6, x


def test_distinct_with_clause(table):
    x = list(
        table.distinct(
            "place",
            "date",
            table.table.columns.date >= datetime(2011, 1, 2, 0, 0),
        )
    )
    assert len(x) == 4, x


def test_distinct_with_filter(table):
    x = list(table.distinct("temperature", place=TEST_CITY_1))
    assert len(x) == 3, x
    x = list(table.distinct("temperature", place=[TEST_CITY_1, TEST_CITY_2]))
    assert len(x) == 6, x


def test_distinct_limit_offset_and_missing_column(table):
    assert len(list(table.distinct("temperature", _limit=1))) == 1

    all_rows = list(table.distinct("temperature"))
    offset_rows = list(table.distinct("temperature", _offset=1))
    assert len(offset_rows) == len(all_rows) - 1

    with pytest.raises(DatasetError, match=r"^No such column: nonexistent_col$"):
        list(table.distinct("nonexistent_col"))


def test_distinct_requires_column(table):
    # Filter-only call, no column name: same misuse class as an unknown
    # column, so it must raise rather than silently return no rows.
    with pytest.raises(
        DatasetError, match=r"^distinct\(\) requires at least one column name$"
    ):
        list(table.distinct(place="Berlin"))


def test_distinct_on_nonexistent_table(db):
    missing = db.load_table("truly_missing_distinct_table_xyz")
    assert list(missing.distinct("col")) == []


def test_distinct_case_insensitive(db):
    # has_column() folds case, but distinct() indexed self.table.c with the
    # raw caller string, so a case-mismatched column passed the has_column
    # check and then KeyError'd on the exact-match column collection.
    tbl = db["distinct_case_insensitive"]
    tbl.insert({"Year": 2000})
    tbl.insert({"Year": 2000})
    tbl.insert({"Year": 2001})
    rows = list(tbl.distinct("year"))
    assert len(rows) == 2, rows


def test_insert_many(table):
    data = TEST_DATA * 100
    table.insert_many(data, chunk_size=13)
    assert len(table) == len(data) + len(TEST_DATA), (len(table), len(data))


def test_insert_many_returns_count(db):
    tbl = db["insert_many_returns_count"]
    result = tbl.insert_many([{"n": i} for i in range(5)], chunk_size=2)
    assert result == 5


def test_insert_many_chunk_size_flush(db):
    tbl = db["insert_many_chunk_flush"]
    tbl.insert({"n": 0})  # pre-create the "n" column so the sync step is a no-op
    tbl.delete()

    conn = db.executable
    original_execute = conn.execute
    calls = []

    def spy(*args, **kwargs):
        calls.append(1)
        return original_execute(*args, **kwargs)

    conn.execute = spy
    try:
        tbl.insert_many([{"n": i} for i in range(4)], chunk_size=2)
    finally:
        del conn.execute

    # 4 rows at chunk_size=2 must flush twice (2 execute calls), not once
    # per row and not once for the whole batch.
    assert len(calls) == 2, calls
    assert len(tbl) == 4


def test_insert_many_preserves_server_default(db):
    # A column omitted from a given row must fall back to its DB default,
    # matching insert(). The old pad-to-union step bound an explicit NULL,
    # overriding the server_default.
    tbl = db["insert_many_server_default"]
    tbl.create_column("status", db.types.text, server_default="active")
    tbl.insert_many([{"id": 1, "status": "custom"}, {"id": 2}])
    assert tbl.find_one(id=1)["status"] == "custom"
    assert tbl.find_one(id=2)["status"] == "active"


def test_chunked_insert(table):
    data = TEST_DATA * 100
    with chunked.ChunkedInsert(table) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(table) == len(data) + len(TEST_DATA), (len(table), len(data))


def test_chunked_insert_callback(table):
    data = TEST_DATA * 100
    n_items = 0

    def callback(queue):
        nonlocal n_items
        n_items += len(queue)

    with chunked.ChunkedInsert(table, callback=callback) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(data) == n_items
    assert len(table) == len(data) + len(TEST_DATA)


def test_chunked_insert_invalid_callback(table):
    # A non-callable callback is rejected at construction time.
    with pytest.raises(chunked.InvalidCallbackError):
        chunked.ChunkedInsert(table, callback="not callable")


def test_chunked_flush_threshold_and_default(db):
    assert chunked.ChunkedInsert(db["chunk_default"]).chunksize == 1000
    assert chunked.ChunkedUpdate(db["chunk_default2"], ["id"]).chunksize == 1000

    tbl = db["chunk_threshold"]
    seen = []

    def callback(queue):
        seen.append(len(queue))

    with chunked.ChunkedInsert(tbl, chunksize=2, callback=callback) as inserter:
        inserter.insert({"a": 1})
        inserter.insert({"a": 2})  # hits the chunksize=2 threshold, auto-flushes
        inserter.insert({"a": 3})  # flushed on __exit__ instead
    assert seen == [2, 1], seen


def test_chunked_insert_preserves_values(db):
    tbl = db["chunk_heterogeneous"]
    with chunked.ChunkedInsert(tbl) as inserter:
        inserter.insert({"a": 1, "b": "x"})
        inserter.insert({"a": 2})  # missing "b", must be padded with NULL

    rows = list(tbl.find(order_by="a"))
    assert rows[0]["a"] == 1 and rows[0]["b"] == "x"
    assert rows[1]["a"] == 2 and rows[1]["b"] is None


def test_chunked_insert_preserves_default_across_chunks(db):
    # ChunkedInsert padded to a lifetime self.fields union that was never
    # reset, so a column supplied in an earlier chunk got bound as NULL in a
    # later chunk that omitted it — overriding the DB default across flushes.
    tbl = db["chunked_default_across_chunks"]
    tbl.create_column("status", db.types.text, server_default="active")
    with chunked.ChunkedInsert(tbl, chunksize=1) as inserter:
        inserter.insert({"id": 1, "status": "custom"})  # chunk 1 supplies it
        inserter.insert({"id": 2})  # chunk 2 omits it -> keeps the default
    assert tbl.find_one(id=1)["status"] == "custom"
    assert tbl.find_one(id=2)["status"] == "active"


def test_chunked_update_groups_by_field_set(db):
    # Two queued rows sharing the same field set must be batched into a
    # single table.update_many() call, not one call per row.
    tbl = db["chunk_update_groups"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}])

    calls = []
    original_update_many = tbl.update_many

    def spy(rows, keys, **kwargs):
        rows = list(rows)
        calls.append(len(rows))
        return original_update_many(rows, keys, **kwargs)

    tbl.update_many = spy
    try:
        updater = chunked.ChunkedUpdate(tbl, ["id"])
        updater.update({"id": 1, "n": 10})
        updater.update({"id": 2, "n": 20})
        updater.flush()
    finally:
        del tbl.update_many

    assert calls == [2], calls
    assert tbl.find_one(id=1)["n"] == 10
    assert tbl.find_one(id=2)["n"] == 20


def test_chunked_update_callback(db):
    tbl = db["chunked_update_cb"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}, {"id": 3, "n": 3}])
    seen = []

    def callback(queue):
        seen.append(len(queue))

    # chunksize=2 forces an auto-flush mid-stream, exercising both the queue
    # threshold path and the ChunkedUpdate callback.
    updater = chunked.ChunkedUpdate(tbl, ["id"], chunksize=2, callback=callback)
    updater.update({"id": 1, "n": 10})
    updater.update({"id": 2, "n": 20})
    updater.update({"id": 3, "n": 30})
    updater.flush()

    assert sum(seen) == 3, seen
    assert tbl.find_one(id=1)["n"] == 10
    assert tbl.find_one(id=3)["n"] == 30


def test_update_many(db):
    tbl = db["update_many_test"]
    tbl.insert_many([{"temp": 10}, {"temp": 20}, {"temp": 30}])
    tbl.update_many([{"id": 1, "temp": 50}, {"id": 3, "temp": 50}], "id")

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"]


def test_update_many_heterogeneous_columns(db):
    tbl = db["update_many_hetero"]
    tbl.insert_many([{"id": 1, "x": "x1", "y": "y1"}, {"id": 2, "x": "x2", "y": "y2"}])

    # Rows with different value-column sets must be grouped and updated with
    # separate statements: a column missing from a given row's dict must be
    # left untouched, not bound as NULL.
    tbl.update_many([{"id": 1, "x": "x1-new"}, {"id": 2, "y": "y2-new"}], "id")

    row1 = tbl.find_one(id=1)
    row2 = tbl.find_one(id=2)
    assert row1["x"] == "x1-new"
    assert row1["y"] == "y1"
    assert row2["x"] == "x2"
    assert row2["y"] == "y2-new"


def test_update_many_heterogeneous_columns_across_chunks(db):
    tbl = db["update_many_hetero_chunks"]
    tbl.insert_many([{"id": 1, "x": "x1", "y": "y1"}, {"id": 2, "x": "x2", "y": "y2"}])

    # chunk_size=1 forces each row into its own flushed chunk; the column
    # grouping must not leak across chunks either.
    tbl.update_many(
        [{"id": 1, "x": "x1-new"}, {"id": 2, "y": "y2-new"}], "id", chunk_size=1
    )

    row1 = tbl.find_one(id=1)
    row2 = tbl.find_one(id=2)
    assert row1["x"] == "x1-new"
    assert row1["y"] == "y1"
    assert row2["x"] == "x2"
    assert row2["y"] == "y2-new"


def test_update_many_auto_creates_columns(db):
    # update_many never called _sync_columns, so its ensure/types params were
    # dead and a new value column raised a raw CompileError. With ensure
    # defaulting on, the column must be created before the UPDATE.
    tbl = db["update_many_autocreate"]
    tbl.insert_many([{"id": 1}, {"id": 2}])
    tbl.update_many([{"id": 1, "note": "hello"}], "id")
    assert "note" in tbl.columns
    assert tbl.find_one(id=1)["note"] == "hello"


def test_update_many_on_deferred_table_raises(db):
    # primary_id=False with no columns defers creation; an empty update row
    # gives it no column to create it with. update_many now routes through the
    # same _sync_columns choke point as insert()/update(), so this raises a
    # clear DatasetError instead of the old bare KeyError (key-bearing row) or
    # incidental "missing key column" message.
    tbl = db.create_table("deferred_update_many", primary_id=False)
    with pytest.raises(
        DatasetError,
        match=r"^Cannot write to 'deferred_update_many': "
        r"no columns to create it with\.$",
    ):
        tbl.update_many([{}], ["id"])


def test_update_many_missing_key_column_raises(db):
    tbl = db["update_many_missing_key"]
    tbl.insert_many([{"id": 1, "n": 1}])
    with pytest.raises(DatasetError, match=r"^Row is missing key column: 'id'$"):
        tbl.update_many([{"n": 2}], "id")


def test_update_many_value_column_named_like_key(db):
    # A value column literally named like the WHERE bindparam ('_id') used to
    # collide with the key bind: the rename overwrote it and WHERE/SET shared
    # the bind, so the column was set to the key value instead of its own.
    tbl = db["update_many_underscore_col"]
    tbl.insert_many([{"id": 1, "_id": "keep-me"}, {"id": 2, "_id": "keep-me-2"}])
    tbl.update_many([{"id": 1, "_id": "updated"}], "id")
    assert tbl.find_one(id=1)["_id"] == "updated"
    assert tbl.find_one(id=2)["_id"] == "keep-me-2"


def test_update_many_case_insensitive_key(db):
    # A case-mismatched key (['ID'] against an 'id' column) must resolve and
    # update the row, not KeyError on the exact-match column collection.
    tbl = db["update_many_case_key"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}])
    tbl.update_many([{"ID": 1, "n": 10}], ["ID"])
    assert tbl.find_one(id=1)["n"] == 10
    assert tbl.find_one(id=2)["n"] == 2


def test_update_many_chunk_size_flush(db):
    tbl = db["update_many_chunk_flush"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}])
    # chunk_size=1 forces a flush after every row, not just at the end.
    tbl.update_many([{"id": 1, "n": 10}, {"id": 2, "n": 20}], "id", chunk_size=1)
    assert tbl.find_one(id=1)["n"] == 10
    assert tbl.find_one(id=2)["n"] == 20


def test_update_many_returns_count(db):
    tbl = db["update_many_returns_count"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}, {"id": 3, "n": 3}])
    result = tbl.update_many([{"id": 1, "n": 10}, {"id": 2, "n": 20}], "id")
    assert result == 2


def test_update_many_returns_count_across_column_groups(db):
    # Two rows with different value-column sets land in separate groups
    # within the same flush; the returned count must sum across all of
    # them, not just the last group executed.
    tbl = db["update_many_returns_count_groups"]
    tbl.insert_many([{"id": 1, "x": "x1", "y": "y1"}, {"id": 2, "x": "x2", "y": "y2"}])
    result = tbl.update_many([{"id": 1, "x": "x1-new"}, {"id": 2, "y": "y2-new"}], "id")
    assert result == 2


def test_update_many_chunk_size_flush_count(db):
    tbl = db["update_many_chunk_flush_count"]
    tbl.insert_many([{"id": i, "n": i} for i in range(4)])

    conn = db.executable
    original_execute = conn.execute
    calls = []

    def spy(*args, **kwargs):
        calls.append(1)
        return original_execute(*args, **kwargs)

    conn.execute = spy
    try:
        tbl.update_many([{"id": i, "n": i * 10} for i in range(4)], "id", chunk_size=2)
    finally:
        del conn.execute

    # 4 rows at chunk_size=2 must flush twice, not once per row and not
    # once for the whole batch.
    assert len(calls) == 2, calls


def _write_row(tbl, method_name, row, keys, **kwargs):
    if method_name == "insert":
        tbl.insert(row, **kwargs)
    elif method_name == "insert_ignore":
        tbl.insert_ignore(row, keys, **kwargs)
    elif method_name == "insert_many":
        tbl.insert_many([row], **kwargs)
    elif method_name == "update":
        tbl.update(row, keys, **kwargs)
    elif method_name == "upsert":
        tbl.upsert(row, keys, **kwargs)
    elif method_name == "upsert_many":
        tbl.upsert_many([row], keys, **kwargs)
    else:
        raise ValueError(method_name)


@pytest.mark.parametrize(
    "method_name",
    ["insert", "insert_ignore", "insert_many", "update", "upsert", "upsert_many"],
)
def test_ensure_false_and_types_forwarding(db, method_name):
    tbl = db[f"ensure_types_{method_name}"]
    tbl.insert({"id": 1, "place": "seed"})

    # ensure=False: a column absent from the row's keys must not be created.
    # id=2/3 are fresh rows so insert()/insert_many() (which always INSERT,
    # unlike insert_ignore/update/upsert) don't collide with the id=1 seed.
    _write_row(
        tbl,
        method_name,
        {"id": 2, "place": "seed2", "newcol": 123},
        ["id"],
        ensure=False,
    )
    assert "newcol" not in tbl.columns

    # types=: an explicit type overrides the guessed one for a new column.
    _write_row(
        tbl,
        method_name,
        {"id": 3, "place": "seed3", "typedcol": 123},
        ["id"],
        types={"typedcol": db.types.text},
    )
    assert isinstance(tbl.table.c["typedcol"].type, TEXT)


def test_ensure_creates_index(db):
    tbl = db["ensure_index_insert_ignore"]
    tbl.insert_ignore({"a": 1}, ["a"])
    assert tbl.has_index(["a"]) is True

    tbl_no_ensure = db["ensure_index_insert_ignore_off"]
    tbl_no_ensure.insert({"a": 1})
    tbl_no_ensure.insert_ignore({"a": 2}, ["a"], ensure=False)
    assert tbl_no_ensure.has_index(["a"]) is False

    tbl2 = db["ensure_index_upsert"]
    tbl2.upsert({"a": 1}, ["a"])
    assert tbl2.has_index(["a"]) is True

    tbl2_no_ensure = db["ensure_index_upsert_off"]
    tbl2_no_ensure.insert({"a": 1})
    tbl2_no_ensure.upsert({"a": 2}, ["a"], ensure=False)
    assert tbl2_no_ensure.has_index(["a"]) is False


def test_chunked_update(db):
    tbl = db["update_many_test"]
    tbl.insert_many(
        [
            {"temp": 10, "location": "asdf"},
            {"temp": 20, "location": "qwer"},
            {"temp": 30, "location": "asdf"},
        ]
    )

    chunked_tbl = chunked.ChunkedUpdate(tbl, ["id"])
    chunked_tbl.update({"id": 1, "temp": 50})
    chunked_tbl.update({"id": 2, "location": "asdf"})
    chunked_tbl.update({"id": 3, "temp": 50})
    chunked_tbl.flush()

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"] == 50
    assert tbl.find_one(id=2)["location"] == tbl.find_one(id=3)["location"] == "asdf"


def test_upsert_many(db):
    # Also tests updating on records with different attributes
    tbl = db["upsert_many_test"]

    weight = 100
    tbl.upsert_many([{"age": 10}, {"weight": weight}], "id")
    assert tbl.find_one(id=1)["age"] == 10

    tbl.upsert_many([{"id": 1, "age": 70}, {"id": 2, "weight": weight / 2}], "id")
    assert tbl.find_one(id=2)["weight"] == weight / 2


def test_upsert_many_duplicate_keys_last_wins(db):
    tbl = db["upsert_many_duplicate_keys"]
    # Two rows sharing the same key in a single batch: a single up-front
    # exists-check can't see one row's insert when classifying the next,
    # so only the last occurrence's values must survive.
    tbl.upsert_many([{"id": 1, "value": "first"}, {"id": 1, "value": "second"}], "id")
    assert len(tbl) == 1
    assert tbl.find_one(id=1)["value"] == "second"


def test_upsert_many_duplicate_keys_last_wins_across_chunks(db):
    tbl = db["upsert_many_duplicate_keys_chunks"]
    # chunk_size=1 splits the duplicate pair across two batches: the
    # second batch's exists-check must see the first batch's insert, same
    # as the single-batch case above.
    tbl.upsert_many(
        [{"id": 1, "value": "first"}, {"id": 1, "value": "second"}],
        "id",
        chunk_size=1,
    )
    assert len(tbl) == 1
    assert tbl.find_one(id=1)["value"] == "second"


def test_upsert_many_composite_key(db):
    tbl = db["upsert_many_composite_key"]
    tbl.upsert_many(
        [{"a": 1, "b": 1, "value": "x"}, {"a": 1, "b": 2, "value": "y"}],
        ["a", "b"],
    )
    assert len(tbl) == 2

    # Update the first pair and insert a brand-new pair in the same call.
    tbl.upsert_many(
        [{"a": 1, "b": 1, "value": "x-updated"}, {"a": 2, "b": 1, "value": "z"}],
        ["a", "b"],
    )
    assert len(tbl) == 3
    assert tbl.find_one(a=1, b=1)["value"] == "x-updated"
    assert tbl.find_one(a=1, b=2)["value"] == "y"
    assert tbl.find_one(a=2, b=1)["value"] == "z"


def test_upsert_many_case_insensitive_key(db):
    # A case-mismatched key (['ID'] against an 'id' column) must update the
    # existing row, not KeyError on the exact-match column collection nor
    # silently reroute the row to a duplicate INSERT.
    tbl = db["upsert_many_case_key"]
    tbl.insert_many([{"id": 1, "n": 1}])
    tbl.upsert_many([{"ID": 1, "n": 10}], ["ID"])
    assert len(tbl) == 1
    assert tbl.find_one(id=1)["n"] == 10


def test_upsert_many_heterogeneous_columns_batched(db):
    tbl = db["upsert_many_hetero_batched"]
    tbl.insert_many([{"id": 1, "x": "x1", "y": "y1"}, {"id": 2, "x": "x2", "y": "y2"}])

    # Regression check: the batched rewrite must still route through
    # update_many's per-group SET clause (A1's fix), not NULL out columns
    # missing from a given row.
    tbl.upsert_many([{"id": 1, "x": "x1-new"}, {"id": 2, "y": "y2-new"}], "id")

    row1 = tbl.find_one(id=1)
    row2 = tbl.find_one(id=2)
    assert row1["x"] == "x1-new"
    assert row1["y"] == "y1"
    assert row2["x"] == "x2"
    assert row2["y"] == "y2-new"


def test_upsert_many_batched_is_fast(db):
    tbl = db["upsert_many_timing"]
    rows = [{"id": i, "value": i} for i in range(1, 501)]

    start = time.perf_counter()
    tbl.upsert_many(rows, "id")
    elapsed = time.perf_counter() - start

    assert len(tbl) == 500
    # Soft smoke check, not a strict perf gate: the batched rewrite should
    # comfortably finish 500 new-row upserts well under a second.
    assert elapsed < 2.0, elapsed


def test_upsert_many_default_chunk_size_does_not_crash_on_sqlite(db):
    # The batch existence check builds one OR-of-AND clause per distinct
    # key; SQLite's default expression-tree depth limit is 1000, so a
    # batch of >=999 distinct keys at the default chunk_size=1000 must not
    # build one giant clause, or this raises "Expression tree is too large".
    tbl = db["upsert_many_default_chunk_crash"]
    rows = [{"id": i, "value": i} for i in range(1000)]
    result = tbl.upsert_many(rows, "id")
    assert result == 1000
    assert len(tbl) == 1000


def test_upsert_many_returns_count(db):
    tbl = db["upsert_many_returns_count"]
    tbl.insert_many([{"id": 1, "n": 1}])
    result = tbl.upsert_many([{"id": 1, "n": 10}, {"id": 2, "n": 20}], "id")
    assert result == 2  # one update, one insert


def test_upsert_many_returns_count_duplicate_split_across_chunk(db):
    # chunk_size=2 puts the duplicate id=1 pair in the first batch and the
    # new id=2 row in a second batch; the duplicate must still collapse to
    # one write, not be double-counted.
    tbl = db["upsert_many_count_duplicate_chunk"]
    result = tbl.upsert_many(
        [{"id": 1, "value": "a"}, {"id": 1, "value": "b"}, {"id": 2, "value": "c"}],
        "id",
        chunk_size=2,
    )
    assert result == 2
    assert tbl.find_one(id=1)["value"] == "b"


def test_upsert_many_returns_count_across_multiple_batches(db):
    # chunk_size=1 forces 4 separate batches: two updates, then two
    # inserts. The returned count must accumulate across every batch, not
    # just reflect the last one.
    tbl = db["upsert_many_count_multi_batch"]
    tbl.insert_many([{"id": 1, "n": 1}, {"id": 2, "n": 2}])
    result = tbl.upsert_many(
        [
            {"id": 1, "n": 10},
            {"id": 2, "n": 20},
            {"id": 3, "n": 30},
            {"id": 4, "n": 40},
        ],
        "id",
        chunk_size=1,
    )
    assert result == 4


def test_upsert_many_creates_index(db):
    tbl = db["upsert_many_creates_index"]
    tbl.upsert_many([{"a": 1}], ["a"])
    assert tbl.has_index(["a"]) is True


def test_upsert_many_ensure_false_skips_index_creation(db):
    tbl = db["upsert_many_ensure_false_index"]
    tbl.insert({"a": 1})
    tbl.upsert_many([{"a": 2}], ["a"], ensure=False)
    assert tbl.has_index(["a"]) is False


def test_drop_operations(table):
    assert table._table is not None, "table shouldn't be dropped yet"
    table.drop()
    assert table._table is None, "table should be dropped now"
    assert list(table.all()) == [], table.all()
    assert table.count() == 0, table.count()


def test_table_drop(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db


def test_drop_evicts_table_cache(db, table):
    t1 = db["weather"]
    assert t1 is table
    t1.drop()
    assert db["weather"] is not t1


def test_table_drop_then_create(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db
    db["weather"].insert({"foo": "bar"})


def test_columns(table):
    cols = table.columns
    assert len(list(cols)) == 4, "column count mismatch"
    assert "date" in cols and "temperature" in cols and "place" in cols


def test_has_column_none_and_columns_before_sync(db, table):
    assert table.has_column(None) is False

    # Evict from the wrapper cache to force a fresh reflection of _columns
    # (starts at the None sentinel, not an empty dict).
    db._tables.pop("weather", None)
    fresh = db.load_table("weather")
    assert "place" in fresh.columns


def test_table_property_returns_table_not_none(db):
    tbl = db["snapshot_test"]
    tbl.insert({"a": 1})
    real = tbl._table
    assert real is not None

    # A concurrent drop/_flush_tables can null _table between the property's
    # internal reads. Serve the valid table on the first reads, then None: the
    # property must still return the snapshotted table, never None (which would
    # AttributeError on .select()/.insert() downstream).
    reads = []

    class _NullingTable:
        def __get__(self, obj, objtype=None):
            reads.append(1)
            return real if len(reads) <= 2 else None

        def __set__(self, obj, value):
            pass

    type(tbl)._table = _NullingTable()
    try:
        result = tbl.table
    finally:
        del type(tbl)._table
    assert result is real


def test_sync_table_add_existing_column_is_idempotent(table):
    before = set(table.columns)
    table._sync_table((Column("place", Unicode),))
    assert set(table.columns) == before


def test_sync_table_concurrent_different_columns(tmp_path):
    # Two threads first-writing DISTINCT columns to the SAME deferred Table
    # race to create it. The create block re-checks _table under the lock, so
    # the loser adds its column to the winner's table instead of overwriting
    # _table with a schema-mismatched object (which then never creates its own
    # column and fails to write it). A file-backed DB is needed for the
    # threads to share a database.
    db = connect(f"sqlite:///{tmp_path / 'race.db'}")
    try:
        tbl = db["race_tbl"]  # deferred; shared across both threads

        original_reflect = type(tbl)._reflect_table
        original_warn = type(tbl)._threading_warn
        barrier = threading.Barrier(2)
        reflected = threading.local()
        holds_lock = threading.Event()
        release_winner = threading.Event()
        warn_calls = []
        warn_lock = threading.Lock()

        def synced_reflect(self):
            # First reflect per thread: sync at the barrier so both threads see
            # _table=None and pass the lock-free create check before either
            # grabs the lock (_reflect_table releases db.lock before we wait).
            if not getattr(reflected, "done", False):
                reflected.done = True
                original_reflect(self)
                barrier.wait()
            else:
                original_reflect(self)

        def synced_warn(self):
            with warn_lock:
                first = not warn_calls
                warn_calls.append(1)
            if first:
                # The winner holds db.lock but hasn't created yet; keep holding
                # so the loser blocks on the lock with _table still read as None
                # (the stale decision the create-block re-check must catch).
                holds_lock.set()
                release_winner.wait(timeout=5)
            original_warn(self)

        errors = []

        def writer(col):
            # Call _sync_table directly with a distinct column so the
            # create-race lands on the real columns (going through insert()
            # would pre-create the table via the has_column precheck).
            try:
                tbl._sync_table((Column(col, Unicode),))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        type(tbl)._reflect_table = synced_reflect
        type(tbl)._threading_warn = synced_warn
        try:
            threads = [
                threading.Thread(target=writer, args=("a",)),
                threading.Thread(target=writer, args=("b",)),
            ]
            for t in threads:
                t.start()
            holds_lock.wait(timeout=5)
            time.sleep(0.2)  # let the loser block on the lock with _table=None
            release_winner.set()
            for t in threads:
                t.join()
        finally:
            type(tbl)._reflect_table = original_reflect
            type(tbl)._threading_warn = original_warn

        assert not errors, errors

        # Verify the real DB schema via a fresh reflection, immune to any
        # _table-cache poisoning on the shared wrapper: the loser overwriting
        # _table would leave its column uncreated in the DB.
        db._tables.pop("race_tbl", None)
        fresh = db.load_table("race_tbl")
        assert fresh.has_column("a"), fresh.columns
        assert fresh.has_column("b"), fresh.columns
    finally:
        db.close()


def test_load_missing_table_raises(db):
    tbl = db.load_table("truly_missing_load_table_xyz")
    with pytest.raises(
        DatasetError, match=r"^Table does not exist: truly_missing_load_table_xyz$"
    ):
        tbl.insert({"a": 1})


def test_insert_empty_row_on_deferred_table_raises(db):
    # primary_id=False with no columns yet defers table creation; writing an
    # empty row never gives it a column to be created with. This must raise
    # a clear DatasetError, not a raw driver OperationalError.
    tbl = db.create_table("deferred_columnless", primary_id=False)
    with pytest.raises(
        DatasetError,
        match=r"^Cannot write to 'deferred_columnless': "
        r"no columns to create it with\.$",
    ):
        tbl.insert({})


@pytest.mark.skipif(not IS_SQLITE, reason="drop_column succeeds on non-SQLite backends")
def test_drop_column_guards():
    # A standalone connection, not the shared `db` fixture: we close it
    # mid-test, which would break the fixture's own teardown.
    db = connect()
    tbl = db["drop_column_guard"]
    tbl.insert({"a": 1})

    with pytest.raises(
        RuntimeError, match=r"^SQLite does not support dropping columns\.$"
    ):
        tbl.drop_column("a")

    db.close()
    with pytest.raises(
        RuntimeError, match=r"^Cannot drop columns when no engine is available\.$"
    ):
        tbl.drop_column("a")


def test_threading_warn_fires_message_and_category(db):
    stop = threading.Event()
    helper = threading.Thread(target=stop.wait)
    helper.start()
    try:
        assert threading.active_count() > 1
        db.begin()
        try:
            with pytest.warns(RuntimeWarning) as record:
                db["threading_warn_test"].insert({"a": 1})
        finally:
            db.rollback()
    finally:
        stop.set()
        helper.join()

    assert len(record) == 1
    assert record[0].category is RuntimeWarning
    assert str(record[0].message) == (
        "Changing the database schema inside a transaction "
        "in a multi-threaded environment is likely to lead "
        "to race conditions and synchronization issues."
    )


def test_threading_warn_silent_single_thread(db):
    db.begin()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes a failure here
            db["threading_warn_silent_test"].insert({"a": 1})
    finally:
        db.rollback()


def test_iter(table):
    c = 0
    for _row in table:
        c += 1
    assert c == len(table)


def test_update(table):
    date = datetime(2011, 1, 2)
    res = table.update(
        {"date": date, "temperature": -10, "place": TEST_CITY_1}, ["place", "date"]
    )
    assert res == 1
    m = table.find_one(place=TEST_CITY_1, date=date)
    assert m["temperature"] == -10, f"new temp. should be -10 but is {m['temperature']}"


def test_update_missing_key_column_returns_zero(table):
    # Unlike update_many (which raises, see
    # test_update_many_missing_key_column_raises), update()'s _keys_to_args
    # tolerates a key column missing from the row by defaulting it to None,
    # which then matches zero rows instead of raising.
    result = table.update({"place": "Berlin"}, ["place", "date"])
    assert result == 0


def test_create_column(db, table):
    flt = db.types.float
    table.create_column("foo", flt)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    assert "foo" in table.columns, table.columns


def test_create_column_forwards_kwargs(db, table):
    # SQLite's ALTER TABLE requires a default when adding a NOT NULL column.
    table.create_column(
        "nullable_test", db.types.text, nullable=False, server_default="x"
    )
    assert table.table.c["nullable_test"].nullable is False


def test_create_column_existing_logs_debug(table, caplog):
    with caplog.at_level(logging.DEBUG):
        table.create_column("place", table.db.types.text)
    assert "Column exists" in caplog.text


def test_sync_columns_ensure_false_and_explicit_types(db):
    tbl = db["sync_columns_test"]
    tbl.insert({"id": 1})

    tbl.insert({"id": 2, "unknown": "x"}, ensure=False)
    assert "unknown" not in tbl.columns

    # For a brand-new column there's no established canonical casing yet,
    # so types= must match the row's own key exactly; a mismatched-case key
    # is silently ignored and the type falls back to guessing from the value.
    tbl.insert({"id": 3, "MyCol": 123}, types={"mycol": db.types.text})
    assert isinstance(tbl.table.c["MyCol"].type, BIGINT)

    tbl.insert({"id": 4, "OtherCol": 123}, types={"OtherCol": db.types.text})
    assert isinstance(tbl.table.c["OtherCol"].type, TEXT)


@pytest.mark.parametrize(
    "name,example,expected_type",
    [
        ("colfloat", 0.1, Float),
        ("colint", 1, BIGINT),
        ("coltext", "test", TEXT),
        ("colbig", 11111111111, BIGINT),
        ("colneg", -11111111111, BIGINT),
    ],
)
def test_ensure_column(table, name, example, expected_type):
    table.create_column_by_example(name, example)
    assert name in table.table.c, table.table.c
    assert isinstance(table.table.c[name].type, expected_type), table.table.c[name].type


def test_key_order(db, table):
    res = db.query("SELECT temperature, place FROM weather LIMIT 1")
    keys = list(res.next().keys())
    assert keys[0] == "temperature"
    assert keys[1] == "place"


def test_empty_query(table):
    empty = list(table.find(place="not in data"))
    assert len(empty) == 0, empty


def test_create_index(table):
    table.create_index(["place"])
    expected_name = index_name("weather", ["place"])
    indexes = table.db.inspect.get_indexes("weather")
    matched = [i for i in indexes if i["name"] == expected_name]
    assert len(matched) == 1, indexes
    assert matched[0]["column_names"] == ["place"]
    assert table.has_index(["place"]) is True


def test_create_index_dedups_columns(db):
    # has_index dedups its columns with dict.fromkeys; create_index must
    # too, or a repeated column emits ON t (a, a) — rejected by MySQL
    # (ERROR 1060) and SQLite alike.
    tbl = db["create_index_dedup"]
    tbl.insert({"a": 1})
    tbl.create_index(["a", "a"])
    indexes = tbl.db.inspect.get_indexes("create_index_dedup")
    assert len(indexes) == 1, indexes
    assert indexes[0]["column_names"] == ["a"], indexes


def test_create_index_missing_column_raises(table):
    with pytest.raises(DatasetError, match=r"^No such column: nonexistent_col$"):
        table.create_index(["nonexistent_col"])


def test_create_index_requires_existing_table(db):
    tbl = db.load_table("truly_missing_index_table_xyz")
    with pytest.raises(DatasetError, match=r"^Table has not been created yet\.$"):
        tbl.create_index(["a"])


def test_has_index(db, table):
    assert table.has_index(["id"]) is True  # primary key
    assert table.has_index(["temperature"]) is False  # not indexed
    assert table.has_index(["nonexistent_col"]) is False

    missing = db.load_table("truly_missing_has_index_table_xyz")
    assert missing.has_index(["a"]) is False


def test_has_index_composite_prefix(db):
    tbl = db["has_index_composite"]
    tbl.insert({"a": 1, "b": 2})
    tbl.create_index(["a", "b"])

    assert tbl.has_index(["a"]) is True, "leading column is a real prefix"
    assert tbl.has_index(["b"]) is False, "trailing column alone is not a prefix"

    # Since ["b"] isn't covered by the composite index, create_index must
    # actually create a second, single-column index for it.
    tbl.create_index(["b"])
    assert tbl.has_index(["b"]) is True

    indexes = tbl.db.inspect.get_indexes("has_index_composite")
    assert len(indexes) == 2, indexes


def test_has_index_thread_safe_cache(tmp_path):
    # A file-backed DB, not the default `:memory:` one: SQLite in-memory
    # databases are per-connection, and each thread gets its own connection,
    # so concurrent threads would otherwise each see an empty database.
    db = connect(f"sqlite:///{tmp_path / 'threads.db'}")
    try:
        tbl = db["has_index_thread_safe"]
        tbl.insert({"a": 1})
        tbl.create_index(["a"])

        # A fresh Table instance with an empty in-memory _indexes cache, so
        # every concurrent has_index() call misses the cache and races on
        # the inspector read + cache append.
        db._tables.pop("has_index_thread_safe", None)
        fresh = db.load_table("has_index_thread_safe")

        original_get_indexes = Inspector.get_indexes

        def slow_get_indexes(self, *args, **kwargs):
            result = original_get_indexes(self, *args, **kwargs)
            time.sleep(0.05)
            return result

        Inspector.get_indexes = slow_get_indexes
        results = []

        def worker():
            results.append(fresh.has_index(["a"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            Inspector.get_indexes = original_get_indexes

        assert all(results)
        assert len(fresh._indexes) == 1, fresh._indexes
    finally:
        db.close()


def test_indexes_cache_invalidated_on_drop(table):
    table.create_index(["place"])
    assert table.has_index(["place"]) is True

    table.drop()
    table.insert({"place": "Berlin"})

    # The dropped table's index is gone; the cached "has an index" answer
    # from before the drop must not leak into the recreated table.
    assert table.has_index(["place"]) is False
    indexes = table.db.inspect.get_indexes(table.name)
    assert indexes == []
