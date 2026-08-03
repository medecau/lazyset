"""The `Table` object: CRUD, schema management, and query helpers.

`Table` is the workhorse of lazyset. The four write verbs (`Table.insert`,
`Table.upsert`, `Table.update`, `Table.delete`) each take one row **or** any
iterable of rows; `Table.find` / `Table.find_one` / `Table.count` /
`Table.distinct` read them back. Columns and the table itself are created on
first write when ``auto_create`` is on.
"""

import logging
import threading
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, overload

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, NoSuchTableError
from sqlalchemy.schema import Column, Index
from sqlalchemy.schema import Table as SQLATable
from sqlalchemy.sql import and_, expression, or_
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.expression import (
    ClauseElement,
    ColumnElement,
    UnaryExpression,
    bindparam,
)

from lazyset.types import MYSQL_LENGTH_TYPES, ColumnType, Types
from lazyset.util import (
    QUERY_STEP,
    DatasetError,
    MutableRow,
    NoSuchColumnError,
    QueryError,
    Results,
    Row,
    SchemaError,
    SQLValue,
    WriteRow,
    ensure_strings,
    index_name,
    normalize_column_key,
    normalize_column_name,
    normalize_table_name,
)

if TYPE_CHECKING:
    from lazyset.database import Database

log = logging.getLogger(__name__)


class Table:
    """Represents a table in a database and exposes common operations."""

    PRIMARY_DEFAULT = "id"
    # Reserved read-modifier keyword names. A leading-underscore key arriving
    # in a read helper's ``**kwargs`` is never a column filter; it routes
    # through ``where=`` or a positional clause instead.
    _RESERVED_KWARGS = frozenset(
        {"_limit", "_offset", "_order_by", "_step", "_streamed"}
    )
    # The OR-of-AND existence check (update's non-sane-multi-rowcount
    # fallback) builds one clause per distinct key; SQLite's default
    # expression-tree depth limit is 1000, so it is sub-batched at this size
    # independently of the caller's chunk_size.
    _EXISTS_CHECK_BATCH = 500

    def __init__(
        self,
        database: "Database",
        table_name: str,
        primary_id: str | Literal[False] | None = None,
        primary_type: ColumnType | None = None,
        primary_increment: bool | None = None,
        auto_create: bool = False,
    ):
        """Initialise the table from database schema."""
        self.db = database
        self.name = normalize_table_name(
            table_name, max_bytes=database._max_ident_bytes
        )
        self._table: SQLATable | None = None
        self._columns: dict[str, str] | None = None
        self._indexes: list[tuple[str, ...]] = []
        self._primary_id: str | Literal[False] = (
            primary_id if primary_id is not None else self.PRIMARY_DEFAULT
        )
        self._primary_type: ColumnType = (
            primary_type if primary_type is not None else Types.integer
        )
        if primary_increment is None:
            primary_increment = self._primary_type in (Types.integer, Types.bigint)
        self._primary_increment = primary_increment
        self._auto_create = auto_create

    @property
    def exists(self) -> bool:
        """Check to see if the table currently exists in the database."""
        if self._table is not None:
            return True
        return self.name in self.db

    @property
    def table(self) -> SQLATable:
        """Get a reference to the table, which may be reflected or created.

        This property guarantees to return a non-None SQLATable instance.
        If the table doesn't exist and auto_create is False, raises DatasetError.
        """
        # Snapshot _table into a local and branch/return on that: a concurrent
        # drop()/_flush_tables() can null self._table between reads, and
        # returning None here would AttributeError on the caller's .select().
        table = self._table
        if table is None:
            self._sync_table(())
            table = self._table
        if table is None:
            # Deferred columnless auto-create table (or a concurrent null in
            # the read gap): transient view until the first column is added.
            return SQLATable(self.name, self.db._metadata, schema=self.db.schema)
        return table

    @property
    def _column_keys(self) -> dict[str, str]:
        """Get a dictionary of all columns and their case mapping."""
        if not self.exists:
            return {}
        # Fast path: the map is built once and published whole (never mutated
        # in place), so once it is non-None a lock-free read is safe — this is
        # read once per cell in the bulk write loops. _reflect_table resets it
        # to None under the lock to invalidate. Snapshot into a local so a
        # concurrent reset can't turn our return value into None mid-read
        # (mirrors the `table` property's snapshot).
        columns = self._columns
        if columns is not None:
            return columns
        with self.db.lock:
            # Re-check under the lock: another thread may have built it while
            # we waited.
            if self._columns is None:
                # Initialise the table if it doesn't exist
                table = self.table
                built: dict[str, str] = {}
                for column in table.columns:
                    name = normalize_column_name(
                        column.name, max_bytes=self.db._max_ident_bytes
                    )
                    key = normalize_column_key(name)
                    if key in built:
                        log.warning("Duplicate column: %s", name)
                    if key is None:
                        log.warning("Invalid column name: %s", name)
                        continue
                    built[key] = name
                # Publish the fully-built map in one atomic assignment so a
                # lock-free reader never observes a partially-filled dict.
                self._columns = built
            return self._columns

    @property
    def columns(self) -> list[str]:
        """Get a listing of all columns that exist in the table."""
        return list(self._column_keys.values())

    def has_column(self, column: str | None) -> bool:
        """Check if a column with the given name exists on this table."""
        if column is None:
            return False
        key = normalize_column_key(
            normalize_column_name(column, max_bytes=self.db._max_ident_bytes)
        )
        return key in self._column_keys

    def _get_column_name(self, name: str) -> str:
        """Find the best column name with case-insensitive matching."""
        name = normalize_column_name(name, max_bytes=self.db._max_ident_bytes)
        key = normalize_column_key(name)
        if key is None:
            return name
        return self._column_keys.get(key, name)

    @overload
    def insert(
        self,
        rows: WriteRow,
        auto_create: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        chunk_size: int = 1000,
    ) -> Any: ...

    @overload
    def insert(
        self,
        rows: Iterable[WriteRow],
        auto_create: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        chunk_size: int = 1000,
    ) -> int: ...

    def insert(
        self,
        rows: WriteRow | Iterable[WriteRow],
        auto_create: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        chunk_size: int = 1000,
    ) -> Any:
        """Insert one row **or** an iterable of rows into the table.

        A single ``Mapping`` inserts one row and returns its primary key (or
        ``None`` if the table has no primary key). Any other iterable of rows
        — a list, or a generator, consumed streamingly — performs a bulk
        insert in chunks of ``chunk_size`` and returns the number of rows
        inserted.

            table.insert(dict(title='I am a banana!'))
            table.insert([dict(name='Dolly')] * 10000)
            table.insert(row for row in source)          # generators too

        With ``auto_create`` on (the default) columns absent from the table
        are created; ``types`` overrides the guessed type for a created
        column. The type is otherwise guessed from the row value, defaulting
        to a text field.
        """
        if isinstance(rows, Mapping):
            return self._insert_one(rows, auto_create, types)
        return self._insert_rows(rows, chunk_size, auto_create, types)

    def _insert_one(
        self,
        row: WriteRow,
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> Any:
        synced = self._sync_columns(row, auto_create, types=types)
        res = self.db._execute_write(self.table.insert().values(synced))
        self.db._auto_commit()
        if res.inserted_primary_key is not None and len(res.inserted_primary_key) > 0:
            return res.inserted_primary_key[0]
        return None

    def _insert_rows(
        self,
        rows: Iterable[WriteRow],
        chunk_size: int,
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        # Consume the iterable streamingly (generators included): buffer a
        # chunk, sync + write it, repeat. No whole-input pre-scan, so a huge
        # or lazy source is never fully materialised.
        inserted = 0
        chunk: list[MutableRow] = []
        for row in rows:
            chunk.append(dict(row))  # copy: never mutate the caller's row
            if len(chunk) >= chunk_size:
                inserted += self._flush_insert_chunk(chunk, auto_create, types)
                chunk = []
        if chunk:
            inserted += self._flush_insert_chunk(chunk, auto_create, types)
        return inserted

    def _flush_insert_chunk(
        self,
        chunk: list[MutableRow],
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        # Column creation is chunk-wide, so union this chunk's keys for one
        # _sync_columns pass (this also enforces the auto_create=False
        # strictness per chunk).
        sync_row: MutableRow = {}
        for row in chunk:
            for key in row:
                if key not in sync_row:
                    sync_row[key] = row[key]
        self._sync_columns(sync_row, auto_create, types=types)

        # Group by the exact column set so an omitted column is left out of
        # its group's INSERT and the DB applies its default, rather than being
        # padded to the union with an explicit NULL (which would override a
        # server_default). executemany needs a uniform key set per statement,
        # which the grouping ensures. Column names are normalised to the real
        # DB names (case-insensitive), so e.g. {'NAME': …} lands in 'name'.
        groups: dict[frozenset[str], list[MutableRow]] = {}
        for row in chunk:
            norm = {self._get_column_name(k): v for k, v in row.items()}
            groups.setdefault(frozenset(norm), []).append(norm)
        for group_rows in groups.values():
            self.db._execute_write(self.table.insert(), group_rows)
        self.db._auto_commit()
        return len(chunk)

    def _make_arbiter(self, keys: Sequence[str], auto_create: bool | None) -> list[str]:
        """Validate the key columns and create the UNIQUE arbiter index.

        Columns must already be synced. Returns the normalized key names.
        """
        norm_keys = [self._get_column_name(k) for k in keys]
        for k in norm_keys:
            if not self.has_column(k):
                raise NoSuchColumnError(f"No such column: {k}")
        if self._check_auto_create(auto_create):
            self.create_index(norm_keys, unique=True)
        else:
            # No arbiter is being created, so one must already exist. Enforce
            # it uniformly in Python: SQLite/PostgreSQL raise natively on a
            # missing ON CONFLICT arbiter, but MySQL's ON DUPLICATE KEY UPDATE
            # silently inserts a duplicate when no unique key matches — so
            # without this check MySQL would quietly corrupt the "keys is the
            # arbiter" contract. Raising here (before any SQL) also avoids
            # leaving PostgreSQL's transaction in an aborted state.
            with self.db.lock:
                if not self._has_unique_index(norm_keys):
                    raise SchemaError(
                        f"{self.name!r} has no UNIQUE index or primary key on "
                        f"{norm_keys} to serve as the upsert conflict arbiter; "
                        "create one or enable auto_create."
                    )
        return norm_keys

    def update(
        self,
        rows: WriteRow | Iterable[WriteRow],
        keys: Sequence[str],
        auto_create: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        chunk_size: int = 1000,
    ) -> int:
        """Update one row **or** an iterable of rows in the table.

        Rows are matched by the column names in ``keys``: those columns filter
        which existing rows to update, using the remaining values in each row.
        A single ``Mapping`` updates by one key set; any other iterable — a
        list, or a generator, consumed streamingly — updates in chunks of
        ``chunk_size``.

            # update all entries with id matching 10, setting their title
            table.update(dict(id=10, title='I am a banana!'), ['id'])
            table.update([dict(id=1, n=10), dict(id=2, n=20)], ['id'])

        Since the same row supplies both the filter (``keys``) and the new
        values, a key column's own value is never changed — it only locates
        the row. New value columns are created per ``auto_create``/``types``,
        as in `Table.insert`.

        Returns the number of rows matched by ``keys``. On drivers with no
        reliable executemany rowcount (notably psycopg2 on PostgreSQL) the
        iterable form returns the number of *distinct key tuples* matched,
        which is lower than the summed count when input rows repeat a key.
        """
        if isinstance(rows, Mapping):
            return self._update_one(rows, keys, auto_create, types)
        return self._update_rows(rows, keys, chunk_size, auto_create, types)

    def _update_one(
        self,
        row: WriteRow,
        keys: Sequence[str],
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        synced = self._sync_columns(row, auto_create, types=types)
        args, values = self._keys_to_args(synced, keys)
        clause = self._args_to_clause(args)
        if not len(values):
            return self.count(clause)
        stmt = self.table.update().where(clause).values(values)
        rp = self.db._execute_write(stmt)
        self.db._auto_commit()
        if rp.supports_sane_rowcount():
            return rp.rowcount
        return self.count(clause)

    def _update_rows(
        self,
        rows: Iterable[WriteRow],
        keys: Sequence[str],
        chunk_size: int,
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        keys = ensure_strings(keys)
        updated = 0
        chunk: list[MutableRow] = []
        for row in rows:
            chunk.append(dict(row))
            if len(chunk) >= chunk_size:
                updated += self._flush_update_chunk(chunk, keys, auto_create, types)
                chunk = []
        if chunk:
            updated += self._flush_update_chunk(chunk, keys, auto_create, types)
        return updated

    def _flush_update_chunk(
        self,
        chunk: list[MutableRow],
        keys: Sequence[str],
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        # Sync this chunk's columns (value columns + any key columns present),
        # enforcing the auto_create=False strictness per chunk. A new value
        # column honours auto_create/types; an empty write to a deferred table
        # raises the same clear DatasetError as the single-row path.
        sample: MutableRow = {}
        for row in chunk:
            for col in row:
                if col not in sample:
                    sample[col] = row[col]
        self._sync_columns(sample, auto_create, types=types)

        # Normalize key names now that columns exist, so a case-mismatched key
        # (e.g. ['ID'] against an 'id' column) resolves.
        norm_keys = [self._get_column_name(k) for k in keys]

        # Bind names provably disjoint from every real column: a value column
        # named like a WHERE bind (e.g. '_id') must not collide with it, and
        # WHERE/SET must not share a bind (which would set the column to the
        # key value).
        existing = {c.name for c in self.table.columns}
        key_prefix = "k_"
        while any(name.startswith(key_prefix) for name in existing):
            key_prefix = f"_{key_prefix}"
        val_prefix = "v_"
        while any(name.startswith(val_prefix) for name in existing):
            val_prefix = f"_{val_prefix}"

        where = and_(
            True,
            *(
                self.table.c[key] == bindparam(f"{key_prefix}{i}")
                for i, key in enumerate(norm_keys)
            ),
        )

        # Group rows by their exact value-column set so a column a row omits is
        # left untouched instead of NULLed. Unknown value columns (possible via
        # case) are dropped rather than compiled into the UPDATE.
        groups: dict[frozenset[str], list[tuple[list[SQLValue], MutableRow]]] = {}
        for row_ in chunk:
            normalized = {self._get_column_name(col): val for col, val in row_.items()}
            key_values: list[SQLValue] = []
            for key in norm_keys:
                if key not in normalized:
                    raise SchemaError(f"Row is missing key column: {key!r}")
                key_values.append(normalized.pop(key))
            value_dict = {
                col: val for col, val in normalized.items() if self.has_column(col)
            }
            groups.setdefault(frozenset(value_dict), []).append(
                (key_values, value_dict)
            )

        def count_matched(group_rows: list[MutableRow]) -> int:
            # Sub-batch the existence check (SQLite caps the expression tree at
            # 1000) and union the matched key tuples so duplicate keys count
            # once, not summed.
            matched: set[tuple[SQLValue, ...]] = set()
            step = self._EXISTS_CHECK_BATCH
            for start in range(0, len(group_rows), step):
                sub = group_rows[start : start + step]
                clause = or_(
                    *(
                        and_(
                            *(
                                self.table.c[key] == gr[f"{key_prefix}{i}"]
                                for i, key in enumerate(norm_keys)
                            )
                        )
                        for gr in sub
                    )
                )
                rp2 = self.db._execute_write(
                    select(*(self.table.c[k] for k in norm_keys)).where(clause)
                )
                matched.update(tuple(r) for r in rp2)
            return len(matched)

        updated = 0
        for value_cols, group_items in groups.items():
            cols_list = sorted(value_cols)
            group_rows: list[MutableRow] = []
            for key_values, value_dict in group_items:
                group_row: MutableRow = {
                    f"{key_prefix}{i}": v for i, v in enumerate(key_values)
                }
                for j, col in enumerate(cols_list):
                    group_row[f"{val_prefix}{j}"] = value_dict[col]
                group_rows.append(group_row)

            if not cols_list:
                # Key-only rows have nothing to SET; count the matched keys
                # instead of compiling an invalid empty UPDATE.
                updated += count_matched(group_rows)
                continue

            stmt = (
                self.table.update()
                .where(where)
                .values(
                    {
                        col: bindparam(f"{val_prefix}{j}", required=False)
                        for j, col in enumerate(cols_list)
                    }
                )
            )
            rp = self.db._execute_write(stmt, group_rows)
            if rp.supports_sane_multi_rowcount():
                updated += rp.rowcount
            else:
                # psycopg2 (PostgreSQL) reports no reliable executemany
                # rowcount: count the distinct matched key tuples instead.
                updated += count_matched(group_rows)
        self.db._auto_commit()
        return updated

    def upsert(
        self,
        rows: WriteRow | Iterable[WriteRow],
        keys: Sequence[str],
        auto_create: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        chunk_size: int = 1000,
    ) -> int:
        """Insert-or-update one row **or** an iterable of rows (native UPSERT).

        Each chunk is written with a single ``INSERT ... ON CONFLICT DO
        UPDATE`` (SQLite/PostgreSQL) or ``ON DUPLICATE KEY UPDATE`` (MySQL)
        per column group: the database decides row existence by SQL equality
        on ``keys``, atomically per statement. ``keys`` is required — it names
        the conflict arbiter — and a genuine unique index (or primary key) on
        exactly ``keys`` is what the upsert conflicts on.

            table.upsert(dict(id=10, title='I am a banana!'), ['id'])
            table.upsert([dict(id=1, n=1), dict(id=2, n=2)], ['id'])

        With ``auto_create`` on (the default) that UNIQUE arbiter index is
        created for you (a no-op when a matching one exists), raising
        `SchemaError` if the table already
        holds rows with duplicate ``keys`` values. With ``auto_create=False``
        the arbiter must already exist, or the database raises.

        Unlike the pre-3.0 UPDATE-then-INSERT upsert, this does **not** update
        every non-unique match: without a unique arbiter there is nothing to
        conflict on. Backend-decided semantics: ``None``-valued keys always
        insert (NULLs are distinct); on MySQL the upsert fires on *any* unique
        key and its default collation treats ``'A'``/``'a'`` as duplicates; on
        PostgreSQL a key repeated *within* one chunk raises ("cannot affect
        row a second time") — deduplicate or lower ``chunk_size``.

        Returns the number of rows submitted (single row = 1); the DB resolves
        the insert/update split, which executemany does not report.
        """
        keys = ensure_strings(keys)
        if not keys:
            raise SchemaError(
                "upsert() requires at least one key column as the conflict arbiter."
            )
        if isinstance(rows, Mapping):
            return self._upsert_rows([rows], keys, chunk_size, auto_create, types)
        return self._upsert_rows(rows, keys, chunk_size, auto_create, types)

    def _upsert_rows(
        self,
        rows: Iterable[WriteRow],
        keys: Sequence[str],
        chunk_size: int,
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> int:
        # One compiled statement per column set, cached across chunks.
        stmts: dict[frozenset[str], Insert] = {}
        processed = 0
        chunk: list[MutableRow] = []
        for row in rows:
            chunk.append(dict(row))
            if len(chunk) >= chunk_size:
                processed += self._flush_upsert_chunk(
                    chunk, keys, auto_create, types, stmts
                )
                chunk = []
        if chunk:
            processed += self._flush_upsert_chunk(
                chunk, keys, auto_create, types, stmts
            )
        return processed

    def _flush_upsert_chunk(
        self,
        chunk: list[MutableRow],
        keys: Sequence[str],
        auto_create: bool | None,
        types: dict[str, ColumnType] | None,
        stmts: dict[frozenset[str], Insert],
    ) -> int:
        sync_row: MutableRow = {}
        for row in chunk:
            for k in row:
                if k not in sync_row:
                    sync_row[k] = row[k]
        self._sync_columns(sync_row, auto_create, types=types)
        norm_keys = self._make_arbiter(keys, auto_create)
        # Group by the exact column set: an omitted column is left out of its
        # group's INSERT and SET, so the DB applies its default on insert and
        # leaves the column untouched on update.
        groups: dict[frozenset[str], list[MutableRow]] = {}
        for row in chunk:
            norm = {self._get_column_name(k): v for k, v in row.items()}
            groups.setdefault(frozenset(norm), []).append(norm)
        for group_cols, group_rows in groups.items():
            stmt = stmts.get(group_cols)
            if stmt is None:
                stmt = stmts[group_cols] = self._upsert_stmt(group_cols, norm_keys)
            self.db._execute_write(stmt, group_rows)
        self.db._auto_commit()
        return len(chunk)

    def _upsert_stmt(
        self, group_cols: frozenset[str], norm_keys: Sequence[str]
    ) -> Insert:
        """Build one dialect-native upsert statement for a column group.

        The SET side references the proposed row values (``excluded`` /
        ``inserted``), so a single statement serves every row in the group
        under executemany. Unknown columns (possible with ``auto_create=False``)
        are left out of the SET; the extra parameter keys are ignored at
        execute time, matching insert().
        """
        non_key = sorted(
            c for c in group_cols if c not in norm_keys and self.has_column(c)
        )
        if self.db.is_mysql:
            my_stmt = mysql_insert(self.table)
            if non_key:
                return my_stmt.on_duplicate_key_update(
                    **{c: my_stmt.inserted[c] for c in non_key}
                )
            # Key-only rows: MySQL has no DO NOTHING, so assign a key to its
            # own proposed value — a no-op on duplicates.
            k0 = norm_keys[0]
            return my_stmt.on_duplicate_key_update(**{k0: my_stmt.inserted[k0]})
        stmt = sqlite_insert(self.table) if self.db.is_sqlite else pg_insert(self.table)
        if non_key:
            return stmt.on_conflict_do_update(
                index_elements=list(norm_keys),
                set_={c: stmt.excluded[c] for c in non_key},
            )
        return stmt.on_conflict_do_nothing(index_elements=list(norm_keys))

    def delete(
        self,
        *clauses: ColumnElement[bool],
        where: Mapping[str, SQLValue] | None = None,
        **filters: SQLValue,
    ) -> int:
        """Delete rows from the table.

        Keyword arguments can be used to add column-based filters. The filter
        criterion will always be equality:

            table.delete(place='Berlin')

        ``where`` is the escape hatch for underscore-named columns, matching
        `Table.find`. If no arguments are given, all
        records are deleted.

        Returns the number of deleted rows.
        """
        if not self.exists:
            return 0
        clause = self._filter_clause(clauses, where, filters)
        stmt = self.table.delete().where(clause)
        # On dialects without sane rowcount, rp.rowcount is unreliable; count
        # the matching rows BEFORE the delete (afterwards they are gone).
        # Dead on SQLite/PostgreSQL/MySQL (all sane) — parity with update().
        pre = 0
        if not self.db._executable.dialect.supports_sane_rowcount:
            pre = self.count(clause)
        rp = self.db._execute_write(stmt)
        self.db._auto_commit()
        return rp.rowcount if rp.supports_sane_rowcount() else pre

    def _reflect_table(self) -> None:
        """Load the tables definition from the database."""
        with self.db.lock:
            self._columns = None
            self._indexes = []
            try:
                self._table = SQLATable(
                    self.name,
                    self.db._metadata,
                    schema=self.db.schema,
                    autoload_with=self.db._executable,
                )
            except NoSuchTableError:
                self._table = None

    def _threading_warn(self) -> None:
        if self.db.in_transaction and threading.active_count() > 1:
            warnings.warn(
                "Changing the database schema inside a transaction "
                "in a multi-threaded environment is likely to lead "
                "to race conditions and synchronization issues.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _sync_table(self, columns: Sequence[Column[Any]]) -> None:
        """Lazy load, create or adapt the table structure in the database.

        This method guarantees that self._table will be set to a non-None value
        after successful execution, unless the table would have no columns at
        all (e.g. ``primary_id=False`` with no columns yet), in which case
        creation is deferred and self._table is left as None. If the table
        cannot be created or loaded, it raises DatasetError.

        Known limitation: on SQLite and MySQL, the DDL run here (CREATE
        TABLE / ADD COLUMN) is not transactional. If this runs inside an
        explicit ``db.begin()``, it commits immediately regardless, and a
        later ``rollback()`` will not undo it — even single-threaded.
        """
        if self._table is None:
            # Load an existing table from the database.
            self._reflect_table()
        if self._table is None:
            # Create the table with an initial set of columns.
            if not self._auto_create:
                raise DatasetError(f"Table does not exist: {self.name}")
            # Keep the lock scope small because this is run very often.
            with self.db.lock:
                # Re-check under the lock: another thread may have created the
                # table (possibly with a different column set) while we waited.
                # Add our columns to it rather than overwrite _table with a
                # DB-mismatched object — that would poison the schema cache and
                # leave our own columns uncreated (checkfirst skips the CREATE).
                if self._table is not None:
                    self._add_missing_columns_locked(columns)
                    return
                self._threading_warn()
                table = SQLATable(self.name, self.db._metadata, schema=self.db.schema)
                if self._primary_id is not False:
                    column = Column(
                        self._primary_id,
                        self._primary_type,
                        primary_key=True,
                        autoincrement=self._primary_increment,
                    )
                    table.append_column(column)
                for column in columns:
                    if column.name != self._primary_id:
                        table.append_column(column)
                if not len(table.columns):
                    # SQLite and MySQL reject "CREATE TABLE t ()", so defer
                    # creation until the first column is added (dataset
                    # creates tables lazily anyway).
                    return
                # Create first, publish after: assigning _table before the
                # CREATE left a poisoned cache behind a failed statement
                # (permissions, disk full, MySQL metadata-lock timeout) —
                # `exists` stuck True with no table in the DB. On failure
                # _table stays None and the next call retries.
                table.create(self.db._executable, checkfirst=True)
                self._table = table
                self._columns = None
                self.db._auto_commit()
        elif len(columns):
            with self.db.lock:
                self._add_missing_columns_locked(columns)

    def _add_missing_columns_locked(self, columns: Sequence[Column[Any]]) -> None:
        """Reflect the table and ADD COLUMN for any column not yet present.

        The caller must hold ``self.db.lock``. Shared by the ordinary
        add-column path and by the create-race loser, whose table another
        thread has already created under the lock.
        """
        self._reflect_table()
        self._threading_warn()
        for column in columns:
            if not self.has_column(column.name):
                self.db._op.add_column(self.name, column, schema=self.db.schema)
        self._reflect_table()
        self.db._auto_commit()

    def _sync_columns(
        self,
        row: WriteRow,
        auto_create: bool | None,
        types: dict[str, ColumnType] | None = None,
    ) -> MutableRow:
        """Create missing columns (or the table) prior to writes.

        With ``auto_create=False`` no schema is generated: a row key that is
        not an existing column raises
        `SchemaError` naming the offending
        column(s) (rather than being silently dropped), and passing ``types``
        is rejected as a dead argument (nothing will be created for it).
        """
        auto_create = self._check_auto_create(auto_create)
        types = types or {}
        types = {self._get_column_name(k): v for (k, v) in types.items()}
        if not auto_create and types:
            # types= only takes effect while creating columns; with creation
            # off it is inert — surface the misuse instead of ignoring it.
            raise SchemaError(
                f"types= is ineffective with auto_create=False on table "
                f"{self.name!r}: no columns will be created for {sorted(types)}."
            )
        out = {}
        sync_columns = {}
        unknown: list[str] = []
        for name, value in row.items():
            name = self._get_column_name(name)
            if self.has_column(name):
                out[name] = value
            elif auto_create:
                _type = types.get(name)
                if _type is None:
                    _type = self.db.types.guess(value)
                sync_columns[name] = Column(name, _type)
                out[name] = value
            else:
                unknown.append(name)
        if unknown:
            # Loud failure: with auto_create=False these keys used to be
            # silently dropped, quietly discarding data the caller wrote.
            raise SchemaError(
                f"Unknown column(s) on table {self.name!r} with "
                f"auto_create=False: {unknown}. Enable auto_create to add "
                "them, or drop them from the row."
            )
        self._sync_table(list(sync_columns.values()))
        # Known limitation (L6): this reads self._table lock-free, so a
        # concurrent drop()/_flush_tables() could null it here and raise a
        # spurious "no columns" error even though the columns were just
        # created. A standalone guard would be cosmetic — it can't close the
        # broader window of unsynchronized _table reads across the write path.
        if self._table is None:
            raise DatasetError(
                f"Cannot write to {self.name!r}: no columns to create it with."
            )
        return out

    def _check_auto_create(self, auto_create: bool | None) -> bool:
        if auto_create is None:
            return self.db.auto_create
        return auto_create

    def _generate_clause(
        self, column: str, op: str, value: SQLValue
    ) -> ColumnElement[bool]:
        col = self.table.c[column]
        match op:
            case "like":
                return col.like(value)
            case "ilike":
                return col.ilike(value)
            case "notlike":
                return col.notlike(value)
            case "notilike":
                return col.notilike(value)
            case ">" | "gt":
                return col > value
            case "<" | "lt":
                return col < value
            case ">=" | "gte":
                return col >= value
            case "<=" | "lte":
                return col <= value
            case "=" | "==" | "is":
                return col == value
            case "!=" | "<>" | "not":
                return col != value
            case "in":
                if not isinstance(value, (list, tuple, set)):
                    raise QueryError(f"'in' filter requires a list, got {type(value)}")
                return col.in_(list(value))
            case "notin":
                if not isinstance(value, (list, tuple, set)):
                    raise QueryError(
                        f"'notin' filter requires a list, got {type(value)}"
                    )
                return col.notin_(list(value))
            case "between" | "..":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise QueryError("'between' filter requires a list of two values")
                start, end = value
                return col.between(start, end)
            case "startswith":
                if not isinstance(value, str):
                    raise QueryError("'startswith' filter requires a string")
                return col.startswith(value, autoescape=True)
            case "endswith":
                if not isinstance(value, str):
                    raise QueryError("'endswith' filter requires a string")
                return col.endswith(value, autoescape=True)
            case _:
                raise QueryError(f"Unrecognized operator: {op}")

    def _args_to_clause(
        self,
        args: MutableRow,
        clauses: Iterable[ColumnElement[bool]] = (),
    ) -> ColumnElement[bool]:
        clauses = list(clauses)
        for column, value in args.items():
            column = self._get_column_name(column)
            if not self.has_column(column):
                # Loud failure: a filter on a column that does not exist used
                # to compile to false() and silently match nothing. The
                # table-missing case stays lenient — callers guard on
                # ``self.exists`` before reaching here.
                raise NoSuchColumnError(f"No such column: {column}")
            elif isinstance(value, (list, tuple, set)):
                clauses.append(self._generate_clause(column, "in", value))
            elif isinstance(value, dict):
                clauses.extend(
                    self._generate_clause(column, op, op_value)
                    for op, op_value in value.items()
                )
            else:
                clauses.append(self._generate_clause(column, "=", value))
        return and_(True, *clauses)

    def _args_to_order_by(
        self, order_by: str | Sequence[str] | None
    ) -> list[UnaryExpression[Any]]:
        orderings: list[UnaryExpression[Any]] = []
        for ordering in ensure_strings(order_by):
            if ordering is None:
                continue
            column = ordering.lstrip("-")
            column = self._get_column_name(column)
            if not self.has_column(column):
                # Loud failure, matching the filter path: ordering by a
                # non-existent column used to be silently dropped.
                raise NoSuchColumnError(f"No such column: {column}")
            if ordering.startswith("-"):
                orderings.append(self.table.c[column].desc())
            else:
                orderings.append(self.table.c[column].asc())
        return orderings

    def _reject_reserved_kwargs(self, kwargs: Mapping[str, Any]) -> None:
        """Reject leading-underscore filter kwargs on the read helpers.

        Leading-underscore names are reserved for read modifiers
        (``_limit``, ``_offset``, ``_order_by``, ``_step``, ``_streamed``);
        one arriving in ``**kwargs`` is either a typo or a modifier passed to
        a method that does not accept it. Filter an underscore-named column
        via ``where={...}`` or a positional SQLAlchemy clause instead.
        """
        for key in kwargs:
            if key.startswith("_"):
                raise QueryError(
                    f"Unknown or misplaced reserved parameter: {key!r}. "
                    "Leading-underscore names are reserved for read modifiers; "
                    "filter an underscore-named column via where={...} or a "
                    "positional SQLAlchemy clause."
                )

    def _filter_clause(
        self,
        clauses: Sequence[ColumnElement[bool]],
        where: Mapping[str, SQLValue] | None,
        kwargs: Mapping[str, SQLValue],
    ) -> ColumnElement[bool]:
        """Validate filter kwargs and build the combined WHERE clause.

        ``where`` is the escape hatch for filtering columns whose names start
        with an underscore or collide with a reserved modifier: its keys
        bypass the leading-underscore rejection that applies to ``kwargs``.
        On a key collision the ``kwargs`` value wins.
        """
        self._reject_reserved_kwargs(kwargs)
        args: MutableRow = dict(where) if where else {}
        args.update(kwargs)
        return self._args_to_clause(args, clauses=clauses)

    def _keys_to_args(
        self, row: WriteRow, keys: Sequence[str]
    ) -> tuple[MutableRow, MutableRow]:
        keys = [self._get_column_name(k) for k in ensure_strings(keys)]
        # A key column absent from the table (not merely from the row) would
        # compile to false() downstream, silently making update() return 0.
        # Raise instead. Only update() routes through here; the lenient
        # false() posture of find/count/delete is unaffected.
        for k in keys:
            if not self.has_column(k):
                raise NoSuchColumnError(f"No such column: {k}")
        row_ = dict(row)
        args = {k: row_.pop(k, None) for k in keys}
        return args, row_

    def create_column(
        self,
        name: str,
        type: ColumnType,
        **kwargs: object,
    ) -> None:
        """Create a new column ``name`` of a specified type.

            table.create_column('created_at', db.types.datetime)

        `type` corresponds to an SQLAlchemy type, most easily referenced
        through ``db.types`` (see `Types`).
        Additional keyword arguments are passed
        to the constructor of `Column`, so that default values, and
        options like `nullable` and `unique` can be set.

            table.create_column('key', unique=True, nullable=False)
            table.create_column('food', default='banana')
        """
        name = self._get_column_name(name)
        if self.has_column(name):
            log.debug(f"Column exists: {name}")
            return
        self._sync_table((Column(name, type, **kwargs),))  # type: ignore[arg-type]

    def create_column_by_example(self, name: str, value: SQLValue) -> None:
        """
        Explicitly create a new column ``name`` with a type that is appropriate
        to store the given example ``value``.  The type is guessed in the same
        way as for the insert method with ``auto_create=True``.

            table.create_column_by_example('length', 4.2)

        If a column of the same name already exists, no action is taken, even
        if it is not of the type we would have created.
        """
        type_ = self.db.types.guess(value)
        self.create_column(name, type_)

    def drop_column(self, name: str) -> None:
        """
        Drop the column ``name``.

            table.drop_column('created_at')

        DROP COLUMN is attempted on every backend; the database decides whether
        it is supported (SQLite gained ``ALTER TABLE ... DROP COLUMN`` in 3.35),
        so an older engine surfaces its own error rather than a preemptive one.
        """
        if self.db.engine is None:
            raise DatasetError("Cannot drop columns when no engine is available.")
        name = self._get_column_name(name)
        with self.db.lock:
            if not self.exists or not self.has_column(name):
                log.debug("Column does not exist: %s", name)
                return

            self._threading_warn()
            self.db._op.drop_column(self.table.name, name, schema=self.table.schema)
            self._reflect_table()
            self.db._auto_commit()

    def drop(self) -> None:
        """Drop the table from the database.

        Deletes both the schema and all the contents within it.
        """
        with self.db.lock:
            if self.exists:
                self._threading_warn()
                self.table.drop(self.db._executable, checkfirst=True)
                self._table = None
                self._columns = None
                self.db._tables.pop(self.name, None)
                self.db._auto_commit()

    def has_index(self, columns: Iterable[str]) -> bool:
        """Check if an index exists to cover the given ``columns``."""
        with self.db.lock:
            if not self.exists:
                return False
            columns_ = tuple(
                dict.fromkeys(self._get_column_name(c) for c in ensure_strings(columns))
            )
            if columns_ in self._indexes:
                return True
            for column in columns_:
                if not self.has_column(column):
                    return False
            indexes = self.db._inspect.get_indexes(self.name, schema=self.db.schema)
            for index in indexes:
                idx_columns = index.get("column_names", [])
                if idx_columns[: len(columns_)] == list(columns_):
                    self._indexes.append(columns_)
                    return True
            pk_columns = [c.name for c in self.table.primary_key.columns]
            if pk_columns[: len(columns_)] == list(columns_):
                self._indexes.append(columns_)
                return True
            return False

    def _has_unique_index(self, columns: Sequence[str]) -> bool:
        """Check for a UNIQUE index (or the primary key) on exactly ``columns``.

        Deliberately not ``has_index``: that matches any ordered leftmost
        prefix and caches non-unique indexes, either of which would
        false-positive here — a plain ``ix_`` index on the same columns must
        not satisfy the upsert arbiter requirement. The caller must hold
        ``self.db.lock``.
        """
        if not self.exists:
            return False
        cols = list(columns)
        indexes = self.db._inspect.get_indexes(self.name, schema=self.db.schema)
        for index in indexes:
            if index.get("unique") and index.get("column_names", []) == cols:
                return True
        pk_columns = [c.name for c in self.table.primary_key.columns]
        return pk_columns == cols

    def create_index(
        self,
        columns: Sequence[str],
        name: str | None = None,
        unique: bool = False,
        **kw: object,
    ) -> None:
        """Create an index to speed up queries on a table.

        If no ``name`` is given, a deterministic name is generated from the
        table and column names. With ``unique``, a UNIQUE index is created
        (under a distinct generated name), gated on an exact-column unique
        index or primary key rather than ``has_index``'s prefix match.

            table.create_index(['name', 'country'])

        This is also how `Table.upsert` obtains its conflict arbiter: under
        ``auto_create`` it calls ``create_index(keys, unique=True)`` once. The
        call is a no-op when a matching unique index (or primary key) on
        exactly ``keys`` already exists, so it is safe to repeat. A
        caller-supplied ``mysql_length``
        dict is merged with the auto-computed text/binary prefix lengths
        rather than replacing them.
        """
        # Dedup like has_index (dict.fromkeys, order-preserving): a repeated
        # column would otherwise emit ON t (a, a) — rejected by MySQL
        # (ERROR 1060) and SQLite alike.
        columns = list(
            dict.fromkeys(self._get_column_name(c) for c in ensure_strings(columns))
        )
        with self.db.lock:
            if not self.exists:
                raise DatasetError("Table has not been created yet.")

            for column in columns:
                if not self.has_column(column):
                    raise NoSuchColumnError(f"No such column: {column}")

            covered = (
                self._has_unique_index(columns) if unique else self.has_index(columns)
            )
            if not covered:
                self._threading_warn()
                name = name or index_name(
                    self.name, columns, prefix="uq" if unique else "ix"
                )
                columns_ = [self.table.c[c] for c in columns]

                # MySQL crashes out if you try to index very long text fields,
                # apparently. This defines (a somewhat random) prefix that
                # will be captured by the index, after which I assume the engine
                # conducts a more linear scan:
                auto_length = {
                    col.name: 10
                    for col in columns_
                    if isinstance(col.type, MYSQL_LENGTH_TYPES)
                }
                # Merge, don't clobber: a caller-supplied mysql_length wins per
                # column, while the auto-computed 10-char prefix fills in for
                # any text/binary column the caller didn't mention. Other
                # backends ignore the mysql_length kwarg entirely.
                caller_length = kw.get("mysql_length")
                if isinstance(caller_length, dict):
                    kw["mysql_length"] = {**auto_length, **caller_length}
                else:
                    kw["mysql_length"] = auto_length
                if unique:
                    kw["unique"] = True

                idx = Index(name, *columns_, **kw)  # type: ignore[arg-type]
                if unique:
                    # Existing duplicate key values make the arbiter index
                    # impossible to build; surface that clearly, never swallow.
                    try:
                        idx.create(self.db._executable)
                    except IntegrityError as exc:
                        if not self.db.in_transaction:
                            # Leave the autobegun transaction usable (on
                            # PostgreSQL it is aborted until rolled back).
                            self.db._executable.rollback()
                        raise DatasetError(
                            f"Cannot create a unique index on {columns!r}: "
                            f"table {self.name!r} already contains rows with "
                            "duplicate values for these columns."
                        ) from exc
                else:
                    idx.create(self.db._executable)
                self.db._auto_commit()

    def find(
        self,
        *_clauses: ColumnElement[bool],
        _limit: int | None = None,
        _offset: int = 0,
        _order_by: str | Sequence[str] | None = None,
        _streamed: bool = False,
        _step: int | None = QUERY_STEP,
        where: Mapping[str, SQLValue] | None = None,
        **kwargs: SQLValue,
    ) -> Results:
        """Perform a simple search on the table.

        Simply pass keyword arguments as ``filter``.

            results = table.find(country='France')
            results = table.find(country='France', year=1980)

        Using ``_limit``:

            # just return the first 10 rows
            results = table.find(country='France', _limit=10)

        You can sort the results by single or multiple columns. Append a minus
        sign to the column name for descending order:

            # sort results by a column 'year'
            results = table.find(country='France', _order_by='year')
            # return all rows sorted by multiple columns (descending by year)
            results = table.find(_order_by=['country', '-year'])

        ``_order_by``, ``_limit``, ``_offset``, ``_step`` and ``_streamed``
        are reserved read modifiers; every leading-underscore name is
        reserved. To filter a column whose name starts with an underscore
        (or otherwise collides), pass it via ``where``:

            results = table.find(where={'_id': 5})

        Filtering or ordering on a column that does not exist raises
        `NoSuchColumnError`.

        You can also submit filters based on criteria other than equality,
        see the **Advanced filters** guide for details.

        To run more complex queries with JOINs, or to perform GROUP BY-style
        aggregation, you can also use `Database.query`
        to run raw SQL queries instead.
        """
        if not self.exists:
            return Results(None, row_type=self.db.row_type)
        if self.db.engine is None:
            raise DatasetError("Cannot run queries when no engine is available.")

        orderings = self._args_to_order_by(_order_by)
        args = self._filter_clause(_clauses, where, kwargs)
        query = self.table.select().where(args).limit(_limit).offset(_offset)
        if len(orderings):
            query = query.order_by(*orderings)

        stream_conn = None
        conn = self.db._executable
        if _streamed:
            stream_conn = self.db.engine.connect()
            conn = stream_conn.execution_options(stream_results=True)

        return Results(
            conn.execute(query),
            row_type=self.db.row_type,
            step=_step,
            connection=stream_conn,
        )

    def find_one(
        self,
        *args: ColumnElement[bool],
        _offset: int = 0,
        where: Mapping[str, SQLValue] | None = None,
        **kwargs: SQLValue,
    ) -> Row | None:
        """Get a single result from the table.

        Works just like `Table.find` but returns one
        result, or ``None``. ``_offset`` and ``where`` behave as on ``find``;
        the result-streaming modifiers (``_step``/``_streamed``) do not apply
        to a single-row fetch.

            row = table.find_one(country='United States')
        """
        if not self.exists:
            return None

        # Validate here too: a reserved modifier in kwargs (e.g. _step) would
        # otherwise collide with the values find_one forces below.
        self._reject_reserved_kwargs(kwargs)
        resiter = self.find(
            *args,
            _limit=1,
            _offset=_offset,
            _step=None,
            where=where,
            **kwargs,  # type: ignore[arg-type]
        )
        try:
            for row in resiter:
                return row
        finally:
            resiter.close()
        return None

    def count(
        self,
        *_clauses: ColumnElement[bool],
        where: Mapping[str, SQLValue] | None = None,
        **kwargs: SQLValue,
    ) -> int:
        """Return the count of results for the given filter set.

        Accepts the same positional clauses, ``where`` escape hatch and
        keyword filters as `Table.find` (but no
        limit/offset).
        """
        if not self.exists:
            return 0

        args = self._filter_clause(_clauses, where, kwargs)
        query = select(func.count()).where(args)
        query = query.select_from(self.table)
        rp = self.db._executable.execute(query)
        res = rp.fetchone()
        if res is not None:
            return int(res[0])
        return 0

    def __len__(self) -> int:
        """Return the number of rows in the table."""
        return self.count()

    def distinct(
        self,
        *args: str | ColumnElement[bool],
        _limit: int | None = None,
        _offset: int | None = 0,
        where: Mapping[str, SQLValue] | None = None,
        **kwargs: SQLValue,
    ) -> Results:
        """Return all the unique (distinct) values for the given ``columns``.

            # returns only one row per year, ignoring the rest
            table.distinct('year')
            # works with multiple columns, too
            table.distinct('year', 'country')
            # you can also combine this with a filter
            table.distinct('year', country='China')

        ``where`` is the escape hatch for underscore-named filter columns,
        matching `Table.find`.
        """
        if not self.exists:
            return Results(None, row_type=self.db.row_type)

        columns = []
        clauses = []
        for column in args:
            if isinstance(column, ClauseElement):
                clauses.append(column)
            else:
                # Mirror _args_to_clause/_keys_to_args: normalize before both
                # the has_column check and the exact-match column lookup, or a
                # case/space-mismatched name passes has_column then KeyErrors.
                column = self._get_column_name(column)
                if not self.has_column(column):
                    raise NoSuchColumnError(f"No such column: {column}")
                columns.append(self.table.c[column])

        clause = self._filter_clause(clauses, where, kwargs)
        if not len(columns):
            raise DatasetError("distinct() requires at least one column name")

        q = (
            expression.select(*columns)
            .distinct()
            .where(clause)
            .limit(_limit)
            .offset(_offset)
            .order_by(*[c.asc() for c in columns])
        )
        return self.db.query(q)

    def __iter__(self) -> Results:
        """Return all rows of the table as simple dictionaries.

        Allows for iterating over all rows in the table without explicitly
        calling `Table.find`.

            for row in table:
                print(row)
        """
        return self.find()

    def __repr__(self) -> str:
        """Get table representation."""
        return f"<Table({self.table.name})>"
