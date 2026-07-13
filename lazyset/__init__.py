import os
import warnings
from typing import Any

from lazyset.database import Database
from lazyset.table import Table
from lazyset.util import (
    DatasetError,
    FilterValue,
    NoSuchColumnError,
    QueryError,
    Results,
    Row,
    RowFactory,
    SchemaError,
    SQLValue,
    WriteRow,
)

# shut up useless SA warning:
warnings.filterwarnings("ignore", "Unicode type received non-unicode bind param value.")
warnings.filterwarnings(
    "ignore", "Skipping unsupported ALTER for creation of implicit constraint"
)

__all__ = [
    "Database",
    "DatasetError",
    "FilterValue",
    "NoSuchColumnError",
    "QueryError",
    "Results",
    "Row",
    "RowFactory",
    "SQLValue",
    "SchemaError",
    "Table",
    "WriteRow",
    "connect",
]
__version__ = "0.1.0"


def connect(
    url: str | None = None,
    schema: str | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    auto_create: bool = True,
    row_type: RowFactory = dict,
    sqlite_wal_mode: bool = True,
    on_connect_statements: list[str] | None = None,
) -> Database:
    """Opens a new connection to a database.

    *url* can be any valid `SQLAlchemy engine URL`_.  If *url* is not defined
    it will try to use *DATABASE_URL* from environment variable.  Returns an
    instance of :py:class:`Database <dataset.Database>`. Additionally,
    *engine_kwargs* will be directly passed to SQLAlchemy, e.g. set
    *engine_kwargs={'pool_recycle': 3600}* will avoid `DB connection timeout`_.
    Set *row_type* to an alternate dict-like class to change the type of
    container rows are stored in.::

        db = dataset.connect('sqlite:///factbook.db')

    One of the main features of `dataset` is to automatically create tables and
    columns as data is inserted. This behaviour can optionally be disabled via
    the `auto_create` argument. It can also be overridden in a lot of the
    data manipulation methods using the `auto_create` flag.

    If you want to run custom SQLite pragmas on database connect, you can add them
    to on_connect_statements as a set of strings. You can view a full
    `list of PRAGMAs here`_.

    .. _SQLAlchemy Engine URL: https://docs.sqlalchemy.org/en/latest/core/engines.html#sqlalchemy.create_engine
    .. _DB connection timeout: https://docs.sqlalchemy.org/en/latest/core/pooling.html#setting-pool-recycle
    .. _list of PRAGMAs here: https://www.sqlite.org/pragma.html
    """
    if url is None:
        url = os.environ.get("DATABASE_URL", "sqlite://")

    return Database(
        url,
        schema=schema,
        engine_kwargs=engine_kwargs,
        auto_create=auto_create,
        row_type=row_type,
        sqlite_wal_mode=sqlite_wal_mode,
        on_connect_statements=on_connect_statements,
    )
