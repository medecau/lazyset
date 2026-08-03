"""lazyset — databases for lazy people.

lazyset makes reading and writing a SQL database as simple as working with a
JSON file. It is a thin layer over [SQLAlchemy](https://www.sqlalchemy.org/)
with no ORM models: connect, grab a table, and insert / find plain dicts.
Tables and columns are created on demand. Works on SQLite (the default),
PostgreSQL, and MySQL.

```python
import lazyset

db = lazyset.connect('sqlite:///:memory:')
table = db['sometable']
table.insert(dict(name='John Doe', age=37))
john = table.find_one(name='John Doe')
```

Start at `connect`, then reach for the four `Table` write verbs
(`Table.insert`, `Table.upsert`, `Table.update`, `Table.delete`) and the read
helpers (`Table.find`, `Table.find_one`, `Table.count`, `Table.distinct`). The
two guides below cover the same ground in tutorial form.

.. include:: ../docs/quickstart.md

.. include:: ../docs/queries.md
"""

import os
import warnings
from typing import Any

from lazyset.database import Database
from lazyset.table import Table
from lazyset.types import Types
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
    "Types",
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
    """Open a new connection to a database.

    *url* can be any valid
    [SQLAlchemy engine URL](https://docs.sqlalchemy.org/en/latest/core/engines.html#sqlalchemy.create_engine).
    If *url* is not given it falls back to *DATABASE_URL* from the environment.
    Returns a `Database`. *engine_kwargs* is passed straight to SQLAlchemy, e.g.
    *engine_kwargs={'pool_recycle': 3600}* avoids a
    [DB connection timeout](https://docs.sqlalchemy.org/en/latest/core/pooling.html#setting-pool-recycle).
    Set *row_type* to an alternate dict-like class to change the container rows
    are returned in.

    ```python
    db = lazyset.connect('sqlite:///factbook.db')
    ```

    One of the main features of lazyset is that it creates tables and columns
    automatically as data is inserted. This can be disabled with the
    ``auto_create`` argument, and overridden per call on most write methods.

    To run custom SQLite pragmas on connect, pass them as *on_connect_statements*
    (a list of strings). See the full
    [list of PRAGMAs here](https://www.sqlite.org/pragma.html).
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
