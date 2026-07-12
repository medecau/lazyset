import logging
import threading
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, create_engine, event, inspect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.schema import MetaData
from sqlalchemy.sql import text
from sqlalchemy.sql.expression import Executable

from dataset.table import Table
from dataset.types import ColumnType, Types
from dataset.util import (
    QUERY_STEP,
    DatasetError,
    Results,
    RowFactory,
    SchemaError,
    normalize_column_key,
    normalize_table_name,
    safe_url,
)

log = logging.getLogger(__name__)


class Database:
    """A database object represents a SQL database with multiple tables."""

    def __init__(
        self,
        url: str,
        schema: str | None = None,
        engine_kwargs: dict[str, Any] | None = None,
        auto_create: bool = True,
        row_type: RowFactory = dict,
        sqlite_wal_mode: bool = True,
        on_connect_statements: list[str] | None = None,
    ) -> None:
        """Configure and connect to the database."""
        if engine_kwargs is None:
            engine_kwargs = {}

        parsed_url = urlparse(url)
        # if parsed_url.scheme.lower() in 'sqlite':
        #     # ref: https://github.com/pudo/dataset/issues/163
        #     if 'poolclass' not in engine_kwargs:
        #         engine_kwargs['poolclass'] = StaticPool

        self.lock = threading.RLock()
        self.local = threading.local()
        self.connections: dict[int, Connection] = {}

        if len(parsed_url.query):
            query = parse_qs(parsed_url.query)
            if schema is None:
                schema_qs = query.get("schema", query.get("searchpath", []))
                if len(schema_qs):
                    schema = schema_qs.pop()

        self.schema = schema
        self.engine: Engine | None = create_engine(url, **engine_kwargs)
        assert self.engine is not None
        self.is_postgres = self.engine.dialect.name == "postgresql"
        self.is_sqlite = self.engine.dialect.name == "sqlite"
        self.is_mysql = "mysql" in self.engine.dialect.name
        # PostgreSQL truncates identifiers to 63 *bytes* server-side;
        # emulating that keeps our in-memory names equal to the stored ones.
        # SQLite/MySQL have no byte limit, so no byte trim is applied there.
        self._max_ident_bytes: int | None = 63 if self.is_postgres else None
        # Defensive copy: we append the WAL pragma below, and must not mutate
        # a list the caller still holds a reference to.
        on_connect_statements = list(on_connect_statements or [])

        def _run_on_connect(dbapi_con: Any, con_record: Any) -> None:
            # reference:
            # https://stackoverflow.com/questions/9671490/how-to-set-sqlite-pragma-statements-with-sqlalchemy
            # https://stackoverflow.com/a/7831210/1890086
            for statement in on_connect_statements:
                dbapi_con.execute(statement)

        if self.is_sqlite and parsed_url.path != "" and sqlite_wal_mode:
            # we only enable WAL mode for sqlite databases that are not in-memory
            on_connect_statements.append("PRAGMA journal_mode=WAL")

        if len(on_connect_statements):
            event.listen(self.engine, "connect", _run_on_connect)

        self.types = Types(is_postgres=self.is_postgres)
        self.url = url
        self.row_type: RowFactory = row_type
        self.auto_create = auto_create
        self._tables: dict[str, Table] = {}

    @property
    def _executable(self) -> Connection:
        """Connection against which statements will be executed."""
        with self.lock:
            tid = threading.get_ident()
            if tid not in self.connections:
                if self.engine is None:
                    raise DatasetError("Database is closed")
                self.connections[tid] = self.engine.connect()
            return self.connections[tid]

    @property
    def _op(self) -> Operations:
        """Get an alembic operations context."""
        ctx = MigrationContext.configure(self._executable)
        return Operations(ctx)

    @property
    def _inspect(self) -> Inspector:
        """Get a SQLAlchemy inspector."""
        return inspect(self._executable)

    @property
    def _metadata(self) -> MetaData:
        """Return a SQLAlchemy schema cache object."""
        return MetaData(schema=self.schema)

    @property
    def in_transaction(self) -> bool:
        """Check if this database is in a transactional context."""
        if not hasattr(self.local, "tx"):
            return False
        return len(self.local.tx) > 0

    def _release_connection(self) -> None:
        """Close and release the current thread's connection back to the pool."""
        with self.lock:
            tid = threading.get_ident()
            conn = self.connections.pop(tid, None)
            if conn is not None:
                conn.close()

    def _flush_tables(self) -> None:
        """Clear the table metadata after transaction rollbacks.

        Holds the lock and snapshots the shared table registry before
        iterating: a concurrent table() mutates self._tables under the
        same lock, which would otherwise resize the dict mid-iteration. All
        three caches are reset — nulling only _table leaves _column_keys
        short-circuiting on a stale _columns dict, so a rolled-back
        in-transaction ADD COLUMN would keep reading True.
        """
        with self.lock:
            for table in list(self._tables.values()):
                table._table = None
                table._columns = None
                table._indexes = []

    def _auto_commit(self) -> None:
        """Commit pending changes when not in an explicit transaction.

        In SQLAlchemy 2.x, connections use "autobegin" which starts a
        transaction on first use. This method commits that transaction
        after each write operation when the user has not started an
        explicit transaction via ``begin()``/``with db:``.
        """
        if not self.in_transaction:
            self._executable.commit()

    def begin(self) -> None:
        """Enter a transaction explicitly.

        No data will be written until the transaction has been committed.
        """
        if not hasattr(self.local, "tx"):
            self.local.tx = []
        if not self._executable.in_transaction():
            # No active transaction; start an explicit one (master semantics).
            self.local.tx.append(self._executable.begin())
        else:
            # An autobegin transaction is already active (e.g., from a read);
            # track the nesting depth without starting a second transaction.
            self.local.tx.append(True)

    def commit(self) -> None:
        """Commit the current transaction.

        Make all statements executed since the transaction was begun permanent.
        """
        if hasattr(self.local, "tx") and self.local.tx:
            tx = self.local.tx.pop()
            if not self.local.tx:
                if tx is not True:
                    tx.commit()
                else:
                    self._executable.commit()
                self._release_connection()

    def rollback(self) -> None:
        """Roll back the current transaction.

        Discard all statements executed since the transaction was begun.
        """
        if hasattr(self.local, "tx") and self.local.tx:
            tx = self.local.tx.pop()
            if not self.local.tx:
                if tx is not True:
                    tx.rollback()
                else:
                    self._executable.rollback()
                self._release_connection()
            self._flush_tables()

    def __enter__(self) -> "Database":
        """Start a transaction."""
        self.begin()
        return self

    def __exit__(
        self, error_type: object, error_value: object, traceback: object
    ) -> None:
        """End a transaction by committing or rolling back."""
        if error_type is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
                raise
        else:
            self.rollback()

    def close(self) -> None:
        """Close all database connections and dispose of the engine.

        Releases all pooled connections and makes this object unusable.
        This should be called when the database is no longer needed,
        especially in multi-threaded or connection-pooled setups.
        """
        with self.lock:
            for conn in self.connections.values():
                conn.close()
            self.connections.clear()
            # Dispose and null the engine under the same lock so a concurrent
            # executable()/create_table() can't slip in and build a connection
            # on a half-torn-down engine (orphaned connection).
            if self.engine is not None:
                self.engine.dispose()
            self._tables = {}
            self.engine = None

    @property
    def tables(self) -> list[str]:
        """Get a listing of all tables that exist in the database."""
        return self._inspect.get_table_names(schema=self.schema)

    @property
    def views(self) -> list[str]:
        """Get a listing of all views that exist in the database."""
        return self._inspect.get_view_names(schema=self.schema)

    def __contains__(self, table_name: str) -> bool:
        """Check if the given table name exists in the database."""
        try:
            table_name = normalize_table_name(
                table_name, max_bytes=self._max_ident_bytes
            )
            if table_name in self.tables:
                return True
            return table_name in self.views
        except ValueError:
            return False

    def table(
        self,
        table_name: str,
        *,
        must_exist: bool = False,
        primary_id: str | Literal[False] | None = None,
        primary_type: ColumnType | None = None,
        primary_increment: bool | None = None,
    ) -> Table:
        """Load or create a table and return a :py:class:`Table <dataset.Table>`.

        This is the single table accessor; ``db[table_name]`` is shorthand for
        ``db.table(table_name)``. With ``auto_create`` enabled (the default) the
        table is created on the first write if it does not exist yet.

        ``primary_id`` / ``primary_type`` / ``primary_increment`` configure the
        primary key **at creation time** and are ignored once the table exists.
        The default is an auto-incrementing integer ``id``; pass
        ``primary_id=False`` for no primary key, or a ``db.types`` value as
        ``primary_type`` (text primary keys are the caller's to keep unique).
        Pass ``must_exist=True`` to require an existing table — a missing table
        then raises :py:class:`SchemaError <dataset.SchemaError>` instead of
        being auto-created (use it to read a table you did not create).

        Repeated calls return the same cached handle. Requesting a ``primary_id``
        that contradicts the cached handle, or the primary key of an existing
        database table, raises :py:class:`SchemaError <dataset.SchemaError>`
        rather than silently ignoring the request (the pre-3.0 behaviour).
        ::

            table = db.table('population')
            table = db['population']  # shorthand

            # custom primary key, applied only when the table is created
            db.table('cities', primary_id='city', primary_type=db.types.text)
            db.table('cities', primary_id='city',
                     primary_type=db.types.string(25))
            db.table('log', primary_id=False)  # no primary key

            # read-only access to a table created elsewhere
            existing = db.table('population', must_exist=True)
        """
        if isinstance(primary_type, str):
            raise SchemaError(
                "Text-based primary_type support is dropped, use db.types."
            )
        table_name = normalize_table_name(table_name, max_bytes=self._max_ident_bytes)
        with self.lock:
            cached = self._tables.get(table_name)
            if cached is not None:
                self._reject_primary_conflict(
                    cached._primary_id, primary_id, table_name
                )
                if must_exist and not cached.exists:
                    raise SchemaError(f"Table does not exist: {table_name}")
                return cached
            # Only reflect when we actually need to: must_exist and an explicit
            # primary_id are the deliberate, careful paths. The default
            # db[name] stays lazy — no round-trip, the Table creates itself on
            # first write.
            if must_exist or primary_id is not None:
                exists = self._inspect.has_table(table_name, schema=self.schema)
                if must_exist and not exists:
                    raise SchemaError(f"Table does not exist: {table_name}")
                if exists and primary_id is not None:
                    self._reject_primary_conflict(
                        self._existing_primary_id(table_name), primary_id, table_name
                    )
            table = Table(
                self,
                table_name,
                primary_id=primary_id,
                primary_type=primary_type,
                primary_increment=primary_increment,
                auto_create=self.auto_create and not must_exist,
            )
            self._tables[table_name] = table
            return table

    def _existing_primary_id(self, table_name: str) -> str | Literal[False]:
        """Return an existing table's sole primary-key column, or ``False``.

        A composite (or absent) primary key returns ``False`` — dataset only
        models single-column primary keys, so anything else cannot match a
        caller-supplied ``primary_id`` and is reported as a conflict.
        """
        pk = self._inspect.get_pk_constraint(table_name, schema=self.schema)
        columns = pk.get("constrained_columns") or []
        return columns[0] if len(columns) == 1 else False

    @staticmethod
    def _reject_primary_conflict(
        current: str | Literal[False],
        requested: str | Literal[False] | None,
        table_name: str,
    ) -> None:
        """Raise if an explicit ``primary_id`` contradicts the known one.

        ``current`` is the primary key already configured (a cached handle) or
        reflected (an existing table); ``requested`` is what the caller passed.
        ``None`` means "unspecified" and never conflicts. String comparison is
        case-insensitive, matching column-name normalization. The pre-3.0
        accessors silently ignored a mismatching ``primary_id`` here.
        """
        if requested is None:
            return
        if isinstance(requested, str) and isinstance(current, str):
            if normalize_column_key(requested) == normalize_column_key(current):
                return
        elif requested == current:  # both False, or False vs a name
            return
        raise SchemaError(
            f"Table {table_name!r} already has primary_id={current!r}, "
            f"cannot reconfigure to primary_id={requested!r}"
        )

    def __getitem__(self, table_name: str) -> Table:
        """Get a table by name — shorthand for :py:meth:`table`."""
        return self.table(table_name)

    def _ipython_key_completions_(self) -> list[str]:
        """Completion for table names with IPython."""
        return self.tables

    def query(
        self,
        query: str | Executable,
        params: Mapping[str, Any] | None = None,
        *,
        _step: int | None = QUERY_STEP,
        **kwargs: Any,
    ) -> Results:
        """Run a statement on the database directly.

        Allows for the execution of arbitrary read/write queries. A query can
        either be a plain text string, or a `SQLAlchemy expression
        <https://docs.sqlalchemy.org/en/21/tutorial/data_select.html#tutorial-selecting-data>`_.
        If a plain string is passed in, it will be converted to an expression
        automatically.

        Bind a named parameter in the query (i.e. ``SELECT * FROM tbl WHERE a =
        :foo``) by passing the value as a keyword argument (``foo='bar'``). For
        bind names that collide with reserved words, or with ``params`` /
        ``_step`` themselves, pass the whole mapping as ``params`` instead.
        ``_step`` sets the result fetch batch size (``None`` fetches all rows in
        one go).
        ::

            statement = 'SELECT user, COUNT(*) c FROM photos GROUP BY user'
            for row in db.query(statement):
                print(row['user'], row['c'])

        The returned iterator will yield each result sequentially.
        """
        if isinstance(query, str):
            query = text(query)
        binds = {**params, **kwargs} if params is not None else kwargs
        if binds:
            rp = self._executable.execute(query, binds)
        else:
            rp = self._executable.execute(query)
        return Results(rp, row_type=self.row_type, step=_step)

    def __repr__(self) -> str:
        """Text representation contains the URL."""
        return f"<Database({safe_url(self.url)})>"
