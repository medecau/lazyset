from datetime import datetime

import pytest
from sqlalchemy import Float
from sqlalchemy.exc import ArgumentError
from sqlalchemy.types import BIGINT, TEXT

from dataset import QueryError, chunked

from .sample_data import TEST_CITY_1, TEST_CITY_2, TEST_DATA


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


def test_find_one(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    d = table.find_one(place="Berlin")
    assert d["temperature"] == -10, d
    d = table.find_one(place="Atlantis")
    assert d is None, d


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


def test_find_order_by(table):
    ds = list(table.find(order_by=["temperature"]))
    assert ds[0]["temperature"] == -1, ds
    ds = list(table.find(order_by=["-temperature"]))
    assert ds[0]["temperature"] == 8, ds


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
    "filt",
    [
        {"temperature": {"in": 5}},  # 'in' requires a list
        {"temperature": {"notin": 5}},  # 'notin' requires a list
        {"temperature": {"between": [1]}},  # 'between' requires two values
        {"temperature": {"between": 5}},  # 'between' requires a list
        {"place": {"startswith": 5}},  # 'startswith' requires a string
        {"place": {"endswith": 5}},  # 'endswith' requires a string
    ],
)
def test_find_operator_invalid_value(table, filt):
    with pytest.raises(QueryError):
        list(table.find(**filt))


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


def test_insert_many(table):
    data = TEST_DATA * 100
    table.insert_many(data, chunk_size=13)
    assert len(table) == len(data) + len(TEST_DATA), (len(table), len(data))


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


def test_table_drop_then_create(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db
    db["weather"].insert({"foo": "bar"})


def test_columns(table):
    cols = table.columns
    assert len(list(cols)) == 4, "column count mismatch"
    assert "date" in cols and "temperature" in cols and "place" in cols


def test_drop_column(table):
    try:
        table.drop_column("date")
        assert "date" not in table.columns
    except RuntimeError:
        pass


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


def test_create_column(db, table):
    flt = db.types.float
    table.create_column("foo", flt)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    assert "foo" in table.columns, table.columns


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
