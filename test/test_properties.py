"""Property and regression tests for the pure helpers in ``dataset.util``.

These functions have no database dependency, so they are tested in isolation
here rather than through the DB fixtures. The regression tests below pin the
three bugs fixed alongside them; the Hypothesis property tests assert the
ideal invariants across generated inputs.

All ``@given`` tests here are over pure functions with no fixtures, which
avoids the function-scoped-fixture health-check that ``@given`` triggers on
DB-backed tests.
"""

import re
import string
from datetime import date, datetime

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st
from sqlalchemy.types import BigInteger, Boolean, Date, DateTime, TypeEngine

from lazyset.types import ColumnType, Types
from lazyset.util import (
    SchemaError,
    ensure_strings,
    index_name,
    normalize_column_key,
    normalize_column_name,
    normalize_table_name,
)

TYPES = Types()

# Alphanumeric tokens keep generated URLs / identifiers well-formed (no
# ':', '@', '/', '%' to confuse the parsers).
ALNUM = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=20)
# Valid column-name characters: no '.', '-', or whitespace, but include a few
# multi-byte code points to exercise the UTF-8 byte-trim loop.
COLNAME = st.text(
    alphabet=string.ascii_letters + string.digits + "_€éñ中",
    min_size=1,
    max_size=100,
)
# Table names additionally permit '.' and '-'; still no whitespace.
TABLENAME = st.text(
    alphabet=string.ascii_letters + string.digits + "_.-€",
    min_size=1,
    max_size=100,
)


# ---------------------------------------------------------------------------
# Regression tests for the util.py name-normalization fixes.
# ---------------------------------------------------------------------------


def test_normalize_column_name_rejects_dot_past_truncation():
    """A trailing '.' beyond byte 63 must still be rejected.

    Previously the ``[:63]`` slice ran before the charset check, so a long
    name ending in '.' was silently truncated-and-accepted.
    """
    with pytest.raises(SchemaError):
        normalize_column_name("a" * 63 + ".")
    with pytest.raises(SchemaError):
        normalize_column_name("a" * 70 + "-")


def test_index_name_distinct_for_ambiguous_columns():
    """Column lists that differ only in '||' placement must not collide."""
    assert index_name("t", ["a", "b||c"]) != index_name("t", ["a||b", "c"])


def test_normalize_error_messages():
    """Anchored messages pin the exact wording, not just that *some* error fires."""
    with pytest.raises(SchemaError, match=r"^123 is not a valid column name\.$"):
        normalize_column_name(123)  # type: ignore

    long_name = "a" * 63 + "."
    expected = re.escape(f"{long_name!r} is not a valid column name.")
    with pytest.raises(SchemaError, match=f"^{expected}$"):
        normalize_column_name(long_name)

    with pytest.raises(SchemaError, match=r"^Invalid table name: 123$"):
        normalize_table_name(123)  # type: ignore

    with pytest.raises(SchemaError, match=r"^Invalid table name: ''$"):
        normalize_table_name("  ")


# ---------------------------------------------------------------------------
# normalize_column_name
# ---------------------------------------------------------------------------


@given(name=COLNAME)
@example(name="a" * 100)
@example(name="€" * 40)
def test_normalize_column_name_invariants(name):
    out = normalize_column_name(name)
    # Default (SQLite/MySQL): no length cap at all — the DB owns identifier
    # length (only the char-validity/strip invariants hold here).
    assert len(out) >= 1
    assert "." not in out
    assert "-" not in out
    assert out == out.strip()
    # Idempotent: normalizing an already-normalized name is a no-op.
    assert normalize_column_name(out) == out


@given(name=COLNAME)
@example(name="a" * 100)
@example(name="€" * 40)
def test_normalize_column_name_max_bytes_invariants(name):
    # PostgreSQL call sites pass max_bytes=63 (PG truncates identifiers to
    # 63 bytes server-side; emulating it keeps our name equal to the stored
    # one — that's the delegation boundary).
    out = normalize_column_name(name, max_bytes=63)
    assert len(out.encode("utf-8")) < 64
    assert normalize_column_name(out, max_bytes=63) == out


def test_normalize_column_name_multibyte_not_bytecapped():
    # 40 multibyte chars = 120 bytes: legal on SQLite (unbounded) and MySQL
    # (64 *chars*), so the default must keep the name whole — byte-trimming
    # it silently forked a differently-named column from the caller's.
    name = "€" * 40
    assert normalize_column_name(name) == name


def test_normalize_names_long_default_not_capped():
    # Default path (SQLite/MySQL): the DB owns identifier length, so a long
    # ASCII name is kept whole — no char cap. SQLite is unbounded; MySQL's
    # limit is 64 chars and it errors on overflow rather than truncating.
    # Truncating here forked store-side (_column_keys) from lookup-side
    # (_get_column_name) for reflected >63-char columns.
    assert normalize_column_name("a" * 100) == "a" * 100
    assert normalize_table_name("t" * 100) == "t" * 100
    # PostgreSQL (max_bytes=63) still emulates its server-side byte cap.
    assert normalize_column_name("a" * 100, max_bytes=63) == "a" * 63
    assert normalize_table_name("t" * 100, max_bytes=63) == "t" * 63


@given(s=st.text())
@example(s="a" * 63)
def test_normalize_column_name_rejects_dot(s):
    # Appending '.' makes any input invalid, regardless of length.
    with pytest.raises(SchemaError):
        normalize_column_name(s + ".")


@given(s=st.text(alphabet=" \t\n\r\f\v", max_size=10))
def test_normalize_column_name_rejects_blank(s):
    with pytest.raises(SchemaError):
        normalize_column_name(s)


def test_normalize_column_name_byte_boundary():
    """max_bytes=63 (PostgreSQL): a 63-char/64-byte name loses the overflow char."""
    name = "a" * 62 + "é"
    assert len(name) == 63
    assert len(name.encode("utf-8")) == 64

    out = normalize_column_name(name, max_bytes=63)
    assert out == "a" * 62
    assert len(out.encode("utf-8")) == 62
    # Without the PostgreSQL byte cap the name fits and is kept whole.
    assert normalize_column_name(name) == name


# ---------------------------------------------------------------------------
# normalize_column_key
# ---------------------------------------------------------------------------


@given(x=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126)))
def test_normalize_column_key_invariants(x):
    out = normalize_column_key(x)
    assert out is not None
    assert out == out.upper()
    assert out == out.strip()
    assert normalize_column_key(out) == out  # idempotent
    # Case-insensitive: upper/lower forms collapse to the same key.
    assert normalize_column_key(x.lower()) == normalize_column_key(x.upper())


def test_normalize_column_key_none():
    assert normalize_column_key(None) is None


def test_normalize_column_key_exact():
    assert normalize_column_key(123) is None  # type: ignore
    # Internal spaces are significant (only surrounding whitespace is folded).
    assert normalize_column_key("a b") == "A B"
    assert normalize_column_key("  a b  ") == "A B"


def test_normalize_column_key_preserves_internal_space():
    # "full name" and "fullname" are distinct columns; the old space-collapse
    # mapped both to one key, silently conflating them on a reflected schema.
    assert normalize_column_key("full name") == "FULL NAME"
    assert normalize_column_key("full name") != normalize_column_key("fullname")


# ---------------------------------------------------------------------------
# normalize_table_name
# ---------------------------------------------------------------------------


@given(name=TABLENAME)
@example(name="t" * 100)
def test_normalize_table_name_invariants(name):
    out = normalize_table_name(name)
    # Default (SQLite/MySQL): no length cap — the DB owns identifier length.
    assert len(out) >= 1
    assert normalize_table_name(out) == out  # idempotent


@given(s=st.text(alphabet=" \t\n\r", max_size=10))
def test_normalize_table_name_rejects_blank(s):
    with pytest.raises(SchemaError):
        normalize_table_name(s)


def test_normalize_table_name_byte_boundary():
    """max_bytes=63 (PostgreSQL): a 63-char/64-byte name loses the overflow char."""
    name = "a" * 62 + "é"
    assert len(name) == 63
    assert len(name.encode("utf-8")) == 64

    out = normalize_table_name(name, max_bytes=63)
    assert out == "a" * 62
    assert len(out.encode("utf-8")) == 62


def test_normalize_table_name_multibyte_not_bytecapped():
    # A 40-char multibyte table name is legal on SQLite/MySQL; the
    # unconditional 63-*byte* trim silently pointed the Table object at a
    # truncated (different) table name on those backends.
    name = "€" * 40
    assert normalize_table_name(name) == name


# ---------------------------------------------------------------------------
# ensure_strings
# ---------------------------------------------------------------------------


@given(xs=st.lists(st.text()))
def test_ensure_strings_list_preserves_order(xs):
    out = ensure_strings(xs)
    assert out == list(xs)  # order and count preserved
    assert ensure_strings(out) == out  # idempotent


@given(s=st.text())
def test_ensure_strings_wraps_str(s):
    assert ensure_strings(s) == [s]


def test_ensure_strings_none():
    assert ensure_strings(None) == []


# ---------------------------------------------------------------------------
# Types.guess
# ---------------------------------------------------------------------------


def _is_column_type(value: object) -> bool:
    if isinstance(value, TypeEngine):
        return True
    return isinstance(value, type) and issubclass(value, TypeEngine)


@given(
    sample=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(),
        st.binary(),
        st.dates(),
        st.datetimes(),
        st.decimals(allow_nan=False, allow_infinity=False),
        st.dictionaries(st.text(), st.integers()),
        st.lists(st.integers()),
    )
)
def test_guess_is_total(sample):
    # guess never raises and always returns a usable column type.
    result: ColumnType = TYPES.guess(sample)
    assert _is_column_type(result)


def test_guess_ordering():
    # bool is a subclass of int, and datetime of date: the more specific type
    # must win, so the isinstance checks are ordered narrowest-first.
    assert TYPES.guess(True) is Boolean
    assert TYPES.guess(False) is Boolean
    assert TYPES.guess(1) is BigInteger
    assert TYPES.guess(datetime(2020, 1, 1)) is DateTime
    assert TYPES.guess(date(2020, 1, 1)) is Date


def test_guess_passthrough():
    # An explicit SQLAlchemy type instance is returned as-is, and a type
    # object is instantiated (documented behaviour of guess).
    instance = BigInteger()
    assert TYPES.guess(instance) is instance
    assert isinstance(TYPES.guess(BigInteger), BigInteger)


# ---------------------------------------------------------------------------
# index_name
# ---------------------------------------------------------------------------


@given(table=ALNUM, cols=st.lists(st.text(), min_size=1, max_size=5))
def test_index_name_format(table, cols):
    name = index_name(table, cols)
    prefix = f"ix_{table}_"
    assert name.startswith(prefix)
    suffix = name[len(prefix) :]
    assert len(suffix) == 16
    assert all(c in string.hexdigits for c in suffix)


@given(
    a=st.lists(st.text(), min_size=1, max_size=5),
    b=st.lists(st.text(), min_size=1, max_size=5),
)
@example(a=["a", "b||c"], b=["a||b", "c"])
def test_index_name_distinct_column_lists(a, b):
    assume(a != b)
    assert index_name("t", a) != index_name("t", b)


def test_index_name_exact_value():
    # Precomputed sha1("1:a1:b")[:16] pins the netstring join separator.
    assert index_name("t", ["a", "b"]) == "ix_t_95253b90414f24c6"


def test_index_name_prefix():
    # The uq_ prefix (unique arbiter indexes) must never collide with the
    # ix_ name for the same table/columns, while sharing the identity hash.
    ix = index_name("t", ["a"])
    uq = index_name("t", ["a"], prefix="uq")
    assert ix.startswith("ix_t_")
    assert uq.startswith("uq_t_")
    assert ix[-16:] == uq[-16:]
    assert ix != uq


def test_index_name_byte_capped():
    # A long table name would overflow PostgreSQL's 63-byte identifier limit;
    # the name must be capped while keeping the 16-char hash suffix, so
    # distinct column sets on the same table still get distinct names.
    long_table = "t" * 100
    name = index_name(long_table, ["a"])
    assert len(name.encode("utf-8")) <= 63
    assert name.endswith(index_name("t", ["a"])[-16:])
    assert index_name(long_table, ["a"]) != index_name(long_table, ["b"])
