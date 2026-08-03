"""Result iteration, name normalization, exceptions, and the SQL type aliases.

These are the plumbing helpers behind `Database` and `Table`: the `Results`
row iterator, the `DatasetError` exception family, and the `WriteRow` /
`SQLValue` type aliases used across the public API.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha1
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import Connection, ResultProxy
from sqlalchemy.engine import Row as SARow
from sqlalchemy.exc import ResourceClosedError

QUERY_STEP = 1000

# Type definitions for SQL values and rows
SQLPlainValue = (
    None  # NULL
    | bool  # BOOLEAN
    | int  # INTEGER, BIGINT
    | float  # FLOAT, REAL, DOUBLE
    | str  # VARCHAR, TEXT, CHAR
    | bytes  # BINARY, BLOB, BYTEA
    | Decimal  # NUMERIC, DECIMAL
    | date  # DATE
    | datetime  # DATETIME, TIMESTAMP
)
# A single value that can be written to a column (JSON columns accept a
# dict/list of plain values).
SQLValue = (
    SQLPlainValue
    | dict[str, SQLPlainValue]  # JSON, JSONB
    | list[SQLPlainValue]  # JSON arrays
)
# A value accepted by a find()/count()/delete()/where filter: a plain
# equality value, a membership sequence (turned into an ``IN`` query), or a
# ``{operator: value}`` mapping (see Table._generate_clause).
FilterValue = (
    SQLValue
    | list[SQLValue]
    | tuple[SQLValue, ...]
    | set[SQLValue]
    | dict[str, SQLValue]
)

# Type alias for input rows (dict-like with SQL-compatible values)
WriteRow = Mapping[str, SQLValue]
# Mutable row dict — used where rows are built up or mutated in place
MutableRow = dict[str, SQLValue]
# A row read back from the database (values already converted by the driver).
Row = Mapping[str, Any]
RowFactory = Callable[[Iterable[tuple[str, Any]]], Row]


def convert_row(factory: RowFactory, row: SARow[Any]) -> Row:
    return factory(row._mapping.items())  # type: ignore[arg-type]


class DatasetError(Exception):
    """Base class for every error raised by lazyset."""


class QueryError(DatasetError):
    """An invalid filter or query construction was requested."""


class SchemaError(DatasetError, ValueError):
    """A schema constraint was violated (bad name, missing column to create).

    Inherits `ValueError` as well as `DatasetError` so callers
    that historically caught ``ValueError`` on invalid identifiers keep
    working.
    """


class NoSuchColumnError(SchemaError):
    """A referenced column does not exist on the table."""


def iter_result_proxy(
    rp: ResultProxy[Any], step: int | None = None
) -> Iterator[SARow[Any]]:
    """Iterate over the ResultProxy."""
    while True:
        chunk = rp.fetchall() if step is None else rp.fetchmany(size=step)
        if not chunk:
            break
        yield from chunk


class Results(Iterator[Row]):
    """Wrap a SQLAlchemy ResultProxy as an iterator of dict-like rows.

    Also usable as a context manager so the underlying result/connection is
    released on exit:

        with table.find(country='France') as rows:
            for row in rows:
                ...
    """

    def __init__(
        self,
        result_proxy: ResultProxy[Any] | None,
        row_type: RowFactory = dict,
        step: int | None = None,
        connection: Connection | None = None,
    ):
        self.row_type = row_type
        self.result_proxy = result_proxy
        self._conn = connection
        if result_proxy is None:
            self.keys: list[str] = []
            self._iter: Iterator[SARow[Any]] = iter([])
        else:
            try:
                self.keys = list(result_proxy.keys())
                self._iter = iter_result_proxy(result_proxy, step=step)
            except ResourceClosedError:
                self.keys = []
                self._iter = iter([])

    def __next__(self) -> Row:
        try:
            return convert_row(self.row_type, next(self._iter))
        except StopIteration:
            self.close()
            raise

    def __iter__(self) -> Iterator[Row]:
        return self

    def __enter__(self) -> "Results":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self.result_proxy is not None:
            self.result_proxy.close()
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def ensure_strings(value: str | Iterable[str] | None) -> list[str]:
    """Normalize a string-or-list-of-strings argument to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def normalize_column_name(name: str, max_bytes: int | None = None) -> str:
    """Check if a string is a reasonable thing to use as a column name.

    ``max_bytes`` is the dialect's identifier *byte* limit: PostgreSQL call
    sites pass 63 (PG truncates identifiers to 63 bytes server-side, so
    emulating it keeps our in-memory name equal to the stored one). SQLite
    and MySQL have no byte limit — MySQL's is 64 *characters* — so the
    default applies only the character cap.
    """
    if not isinstance(name, str):
        raise SchemaError(f"{name!r} is not a valid column name.")

    # Validate the full stripped name *before* truncating: a trailing "."
    # or "-" beyond the cap would otherwise be sliced off and slip through.
    name = name.strip()
    if not len(name) or "." in name or "-" in name:
        raise SchemaError(f"{name!r} is not a valid column name.")

    if max_bytes is not None:
        # PostgreSQL truncates identifiers to max_bytes bytes server-side;
        # emulate so our in-memory name equals the stored one. SQLite and
        # MySQL have no byte limit here — the DB owns identifier length.
        name = name[:max_bytes]  # fast char pre-cap
        while len(name.encode("utf-8")) > max_bytes:  # codepoint-safe byte trim
            name = name[:-1]
    return name


def normalize_column_key(name: str | None) -> str | None:
    """Return a comparable column name.

    Folds case and surrounding whitespace (the deliberate, tested
    case-insensitivity), but preserves internal spaces: "full name" and
    "fullname" are distinct columns and must not collapse to one key.
    """
    if name is None or not isinstance(name, str):
        return None
    return name.upper().strip()


def normalize_table_name(name: str, max_bytes: int | None = None) -> str:
    """Check if the table name is obviously invalid.

    ``max_bytes`` follows the same dialect rule as
    `normalize_column_name`: PostgreSQL call sites pass 63; the
    default applies only the character cap.
    """
    if not isinstance(name, str):
        raise SchemaError(f"Invalid table name: {name!r}")
    # Validate emptiness on the stripped name before truncating.
    name = name.strip()
    if not len(name):
        raise SchemaError(f"Invalid table name: {name!r}")
    if max_bytes is not None:
        # PostgreSQL truncates identifiers to max_bytes bytes server-side;
        # emulate so our in-memory name equals the stored one. SQLite and
        # MySQL have no byte limit here — the DB owns identifier length.
        name = name[:max_bytes]  # fast char pre-cap
        while len(name.encode("utf-8")) > max_bytes:  # codepoint-safe byte trim
            name = name[:-1]
    return name


def safe_url(url: str) -> str:
    """Remove password from printed connection URLs.

    Only the userinfo portion of the netloc is rewritten, so a ``:pw@``
    sequence that happens to appear in the path or query is left intact.
    """
    parsed = urlparse(url)
    if parsed.password is None:
        return url
    userinfo, _, host = parsed.netloc.rpartition("@")
    user, _, _ = userinfo.partition(":")
    return urlunparse(parsed._replace(netloc=f"{user}:*****@{host}"))


def index_name(table: str, columns: list[str], prefix: str = "ix") -> str:
    """Generate an artificial index name, capped at 63 bytes.

    ``prefix`` distinguishes index families on the same table/columns:
    plain indexes use the default ``ix``, unique arbiter indexes use ``uq``.
    """
    # Netstring-style join so distinct column lists never collide:
    # ["a", "b||c"] and ["a||b", "c"] must hash to different names.
    sig = "".join(f"{len(c)}:{c}" for c in columns)
    key = sha1(sig.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    # PostgreSQL caps identifiers at 63 bytes and MySQL errors past 64. Keep
    # the hash suffix intact (it carries the column identity, so distinct
    # column sets stay distinct) and byte-trim the <prefix>_<table> part,
    # decoding codepoint-safely so a multi-byte char is never split.
    stem = f"{prefix}_{table}".encode()[: 63 - 1 - len(key)].decode("utf-8", "ignore")
    return f"{stem}_{key}"
