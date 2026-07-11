from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha1
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from sqlalchemy import Connection, ResultProxy
from sqlalchemy.engine import Row
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
SQLWriteValue = (
    SQLPlainValue
    | dict[str, SQLPlainValue]  # JSON, JSONB
    | list[SQLPlainValue]  # JSON arrays
)

# Type alias for input rows (dict-like with SQL-compatible values)
WriteRow = Mapping[str, SQLWriteValue]
# Mutable row dict — used where rows are built up or mutated in place
MutableRow = dict[str, SQLWriteValue]
OutRow = Mapping[str, Any]
RowFactory = Callable[[Iterable[tuple[str, Any]]], OutRow]

row_factory: RowFactory = OrderedDict


def convert_row(factory: RowFactory, row: Row[Any]) -> OutRow:
    return factory(row._mapping.items())  # type: ignore[arg-type]


class DatasetError(Exception):
    pass


class QueryError(DatasetError):
    pass


def iter_result_proxy(
    rp: ResultProxy[Any], step: int | None = None
) -> Iterator[Row[Any]]:
    """Iterate over the ResultProxy."""
    while True:
        chunk = rp.fetchall() if step is None else rp.fetchmany(size=step)
        if not chunk:
            break
        yield from chunk


def make_sqlite_url(
    path: str,
    cache: str | None = None,
    timeout: int | None = None,
    mode: str | None = None,
    check_same_thread: bool = True,
    immutable: bool = False,
    nolock: bool = False,
) -> str:
    # NOTE: this PR
    # https://gerrit.sqlalchemy.org/c/sqlalchemy/sqlalchemy/+/1474/
    # added support for URIs in SQLite
    # The full list of supported URIs is a combination of:
    # https://docs.python.org/3/library/sqlite3.html#sqlite3.connect
    # and
    # https://www.sqlite.org/uri.html
    params: dict[str, Any] = {}
    if cache:
        assert cache in ("shared", "private")
        params["cache"] = cache
    if timeout:
        # Note: if timeout is None, it uses the default timeout
        params["timeout"] = timeout
    if mode:
        assert mode in ("ro", "rw", "rwc")
        params["mode"] = mode
    if nolock:
        params["nolock"] = 1
    if immutable:
        params["immutable"] = 1
    if not check_same_thread:
        params["check_same_thread"] = "false"
    if not params:
        return "sqlite:///" + path
    params["uri"] = "true"
    return "sqlite:///file:" + path + "?" + urlencode(params)


class ResultIter(Iterator[OutRow]):
    """SQLAlchemy ResultProxies are not iterable to get a
    list of dictionaries. This is to wrap them."""

    def __init__(
        self,
        result_proxy: ResultProxy[Any] | None,
        row_type: RowFactory = row_factory,
        step: int | None = None,
        connection: Connection | None = None,
    ):
        self.row_type = row_type
        self.result_proxy = result_proxy
        self._conn = connection
        if result_proxy is None:
            self.keys: list[str] = []
            self._iter: Iterator[Row[Any]] = iter([])
        else:
            try:
                self.keys = list(result_proxy.keys())
                self._iter = iter_result_proxy(result_proxy, step=step)
            except ResourceClosedError:
                self.keys = []
                self._iter = iter([])

    def __next__(self) -> OutRow:
        try:
            return convert_row(self.row_type, next(self._iter))
        except StopIteration:
            self.close()
            raise

    next = __next__

    def __iter__(self) -> Iterator[OutRow]:
        return self

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


def normalize_column_name(name: str) -> str:
    """Check if a string is a reasonable thing to use as a column name."""
    if not isinstance(name, str):
        raise ValueError(f"{name!r} is not a valid column name.")

    # Validate the full stripped name *before* truncating: a trailing "."
    # or "-" beyond byte 63 would otherwise be sliced off and slip through.
    name = name.strip()
    if not len(name) or "." in name or "-" in name:
        raise ValueError(f"{name!r} is not a valid column name.")

    # Column names can be 63 *bytes* max in postgresql. Limit to 63
    # characters first, then trim any trailing multi-byte overflow.
    name = name[:63]
    while len(name.encode("utf-8")) >= 64:
        name = name[: len(name) - 1]
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


def normalize_table_name(name: str) -> str:
    """Check if the table name is obviously invalid."""
    if not isinstance(name, str):
        raise ValueError(f"Invalid table name: {name!r}")
    # Validate emptiness on the stripped name before truncating.
    name = name.strip()
    if not len(name):
        raise ValueError(f"Invalid table name: {name!r}")
    # Table names, like columns, are capped at 63 *bytes* in PostgreSQL:
    # limit to 63 characters first, then trim any trailing multi-byte overflow.
    name = name[:63]
    while len(name.encode("utf-8")) >= 64:
        name = name[: len(name) - 1]
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


def index_name(table: str, columns: list[str]) -> str:
    """Generate an artificial index name, capped at 63 bytes."""
    # Netstring-style join so distinct column lists never collide:
    # ["a", "b||c"] and ["a||b", "c"] must hash to different names.
    sig = "".join(f"{len(c)}:{c}" for c in columns)
    key = sha1(sig.encode("utf-8")).hexdigest()[:16]
    # PostgreSQL caps identifiers at 63 bytes and MySQL errors past 64. Keep
    # the hash suffix intact (it carries the column identity, so distinct
    # column sets stay distinct) and byte-trim the ix_<table> prefix,
    # decoding codepoint-safely so a multi-byte char is never split.
    prefix = f"ix_{table}".encode()[: 63 - 1 - len(key)].decode("utf-8", "ignore")
    return f"{prefix}_{key}"
