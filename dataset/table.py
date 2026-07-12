import logging
import threading
import warnings
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import false, func, select
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

from dataset.types import MYSQL_LENGTH_TYPES, ColumnType, Types
from dataset.util import (
    QUERY_STEP,
    DatasetError,
    MutableRow,
    OutRow,
    QueryError,
    ResultIter,
    SQLWriteValue,
    WriteRow,
    ensure_strings,
    index_name,
    normalize_column_key,
    normalize_column_name,
    normalize_table_name,
)

if TYPE_CHECKING:
    from dataset.database import Database

log = logging.getLogger(__name__)


class Table:
    """Represents a table in a database and exposes common operations."""

    PRIMARY_DEFAULT = "id"
    # The OR-of-AND existence check (update_many's non-sane-multi-rowcount
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
        self.name = normalize_table_name(table_name)
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
            return SQLATable(self.name, self.db.metadata, schema=self.db.schema)
        return table

    @property
    def _column_keys(self) -> dict[str, str]:
        """Get a dictionary of all columns and their case mapping."""
        if not self.exists:
            return {}
        with self.db.lock:
            if self._columns is None:
                # Initialise the table if it doesn't exist
                table = self.table
                self._columns = {}
                for column in table.columns:
                    name = normalize_column_name(column.name)
                    key = normalize_column_key(name)
                    if key in self._columns:
                        log.warning("Duplicate column: %s", name)
                    if key is None:
                        log.warning("Invalid column name: %s", name)
                        continue
                    self._columns[key] = name
            return self._columns

    @property
    def columns(self) -> list[str]:
        """Get a listing of all columns that exist in the table."""
        return list(self._column_keys.values())

    def has_column(self, column: str | None) -> bool:
        """Check if a column with the given name exists on this table."""
        if column is None:
            return False
        key = normalize_column_key(normalize_column_name(column))
        return key in self._column_keys

    def _get_column_name(self, name: str) -> str:
        """Find the best column name with case-insensitive matching."""
        name = normalize_column_name(name)
        key = normalize_column_key(name)
        if key is None:
            return name
        return self._column_keys.get(key, name)

    def insert(
        self,
        row: WriteRow,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """Add a ``row`` dict by inserting it into the table.

        If ``ensure`` is set, any of the keys of the row are not
        table columns, they will be created automatically.

        During column creation, ``types`` will be checked for a key
        matching the name of a column to be created, and the given
        SQLAlchemy column type will be used. Otherwise, the type is
        guessed from the row value, defaulting to a simple unicode
        field.
        ::

            data = dict(title='I am a banana!')
            table.insert(data)

        Returns the inserted row's primary key.
        """
        row = self._sync_columns(row, ensure, types=types)
        res = self.db.executable.execute(self.table.insert().values(row))
        self.db._auto_commit()
        if res.inserted_primary_key is not None and len(res.inserted_primary_key) > 0:
            return res.inserted_primary_key[0]
        return True

    def insert_ignore(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """Add a ``row`` dict into the table if the row does not exist.

        If rows with matching ``keys`` exist no change is made.

        Setting ``ensure`` results in automatically creating missing columns,
        i.e., keys of the row are not table columns.

        During column creation, ``types`` will be checked for a key
        matching the name of a column to be created, and the given
        SQLAlchemy column type will be used. Otherwise, the type is
        guessed from the row value, defaulting to a simple unicode
        field.

        With ``ensure`` on (the default), an index on ``keys`` is created
        as a side effect. Pass ``ensure=False`` if you don't want that,
        e.g. on a locked-down or very large table.
        ::

            data = dict(id=10, title='I am a banana!')
            table.insert_ignore(data, ['id'])
        """
        row = self._sync_columns(row, ensure, types=types)
        if self._check_ensure(ensure):
            self.create_index(keys)
        args, _ = self._keys_to_args(row, keys)
        if self.count(**args) == 0:
            # row was already synced above; avoid insert()'s own redundant
            # _sync_columns pass by writing directly.
            res = self.db.executable.execute(self.table.insert().values(row))
            self.db._auto_commit()
            if (
                res.inserted_primary_key is not None
                and len(res.inserted_primary_key) > 0
            ):
                return res.inserted_primary_key[0]
            return True
        return False

    def insert_many(
        self,
        rows: Sequence[WriteRow],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> int:
        """Add many rows at a time.

        This is significantly faster than adding them one by one. Per default
        the rows are processed in chunks of 1000 per commit, unless you specify
        a different ``chunk_size``.

        See :py:meth:`insert() <dataset.Table.insert>` for details on
        the other parameters.
        ::

            rows = [dict(name='Dolly')] * 10000
            table.insert_many(rows)

        Returns the number of rows inserted.
        """
        # Sync table before inputting rows. Column creation is legitimately
        # call-wide, so union every row's keys for the _sync_columns pass.
        sync_row: MutableRow = {}
        for row in rows:
            # Get a sample of the new column(s) from the row: dict membership
            # is O(1), unlike testing against a rebuilt list every row.
            for key in row:
                if key not in sync_row:
                    sync_row[key] = row[key]
        self._sync_columns(sync_row, ensure, types=types)

        inserted = 0
        chunk: list[MutableRow] = []
        for index, row in enumerate(rows):
            # Normalize column names (case-insensitive match against the real
            # DB names), copying the caller's dict — same as update_many and
            # upsert_many. A raw dict(row) left e.g. {'NAME': …} as an unused
            # executemany param and stored NULL in the 'name' column.
            chunk.append({self._get_column_name(k): v for k, v in row.items()})

            # Insert when chunk_size is fulfilled or this is the last row
            if len(chunk) == chunk_size or index == len(rows) - 1:
                # Group by the exact column set so an omitted column is left
                # out of its group's INSERT and the DB applies its default,
                # rather than being padded to the union with an explicit NULL
                # (which would override server_default). executemany requires
                # a uniform key set per statement, which the grouping ensures.
                # Rows may be reordered across keyset groups within a chunk;
                # only visible via autoincrement PK order, and the method
                # returns a count, so no contract breaks.
                groups: dict[frozenset[str], list[MutableRow]] = {}
                for chunk_row in chunk:
                    groups.setdefault(frozenset(chunk_row), []).append(chunk_row)
                for group_rows in groups.values():
                    self.db.executable.execute(self.table.insert(), group_rows)
                self.db._auto_commit()
                inserted += len(chunk)
                chunk = []
        return inserted

    def update(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> int:
        """Update a row in the table.

        The update is managed via the set of column names stated in ``keys``:
        they will be used as filters for the data to be updated, using the
        values in ``row``.
        ::

            # update all entries with id matching 10, setting their title
            # columns
            data = dict(id=10, title='I am a banana!')
            table.update(data, ['id'])

        If keys in ``row`` update columns not present in the table, they will
        be created based on the settings of ``ensure`` and ``types``, matching
        the behavior of :py:meth:`insert() <dataset.Table.insert>`.

        Since the same ``row`` dict supplies both the filter (``keys``) and
        the new values, a key column's own value can never be changed via
        ``update()`` — it is only ever used to find the row, not to set it.

        Returns the number of rows matched by ``keys``.
        """
        row = self._sync_columns(row, ensure, types=types)
        args, row = self._keys_to_args(row, keys)
        clause = self._args_to_clause(args)
        if not len(row):
            return self.count(clause)
        stmt = self.table.update().where(clause).values(row)
        rp = self.db.executable.execute(stmt)
        self.db._auto_commit()
        if rp.supports_sane_rowcount():
            return rp.rowcount
        return self.count(clause)

    def update_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> int:
        """Update many rows in the table at a time.

        This is significantly faster than updating them one by one. Per default
        the rows are processed in chunks of 1000 per commit, unless you specify
        a different ``chunk_size``.

        See :py:meth:`update() <dataset.Table.update>` for details on
        the other parameters.

        Returns the number of rows matched.
        """
        keys = ensure_strings(keys)

        # Sync columns up front, mirroring insert_many's pre-scan (key
        # columns included): a new value column is created honouring the
        # previously-dead ensure/types params, and an empty write to a
        # deferred table raises the same clear DatasetError as insert()/
        # update() instead of a raw CompileError or a bare KeyError.
        sample: MutableRow = {}
        for row in rows:
            for col in row:
                if col not in sample:
                    sample[col] = row[col]
        self._sync_columns(sample, ensure, types=types)

        # Normalize key names now that the columns exist, so a case-mismatched
        # key (e.g. ['ID'] against an 'id' column) resolves instead of raising
        # a bare KeyError on the exact-match column collection.
        keys = [self._get_column_name(k) for k in keys]

        # Bind names must not collide with a real column: a value column
        # literally named like the WHERE bind (e.g. '_id') would otherwise
        # overwrite it, and WHERE/SET sharing the bind would set the column to
        # the key value. Derive key/value prefixes provably disjoint from
        # every actual column name and build each param dict with only these
        # synthetic keys.
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
                for i, key in enumerate(keys)
            ),
        )

        updated = 0
        chunk: list[WriteRow] = []
        for index, row in enumerate(rows):
            chunk.append(row)

            # Update when chunk_size is fulfilled or this is the last row
            if len(chunk) == chunk_size or index == len(rows) - 1:
                # Group rows by their exact value-column set so a column a row
                # omits is left untouched instead of NULLed. With ensure=False
                # an unknown value column was never created, so drop it (like
                # update()) rather than compile an UPDATE for it. Store the
                # (key values, value dict) per row; the synthetic bind names
                # are assigned per group so ordering is consistent.
                groups: dict[
                    frozenset[str], list[tuple[list[SQLWriteValue], MutableRow]]
                ] = {}
                for row_ in chunk:
                    normalized = {
                        self._get_column_name(col): val for col, val in row_.items()
                    }
                    key_values: list[SQLWriteValue] = []
                    for key in keys:
                        if key not in normalized:
                            raise DatasetError(f"Row is missing key column: {key!r}")
                        key_values.append(normalized.pop(key))
                    value_dict = {
                        col: val
                        for col, val in normalized.items()
                        if self.has_column(col)
                    }
                    groups.setdefault(frozenset(value_dict), []).append(
                        (key_values, value_dict)
                    )

                def count_matched(group_rows: list[MutableRow]) -> int:
                    # Sub-batch the existence check (SQLite caps the
                    # expression tree at 1000) and union the matched key
                    # tuples so duplicate keys are counted once, not summed.
                    matched: set[tuple[SQLWriteValue, ...]] = set()
                    step = self._EXISTS_CHECK_BATCH
                    for start in range(0, len(group_rows), step):
                        sub = group_rows[start : start + step]
                        clause = or_(
                            *(
                                and_(
                                    *(
                                        self.table.c[key] == gr[f"{key_prefix}{i}"]
                                        for i, key in enumerate(keys)
                                    )
                                )
                                for gr in sub
                            )
                        )
                        rp2 = self.db.executable.execute(
                            select(*(self.table.c[k] for k in keys)).where(clause)
                        )
                        matched.update(tuple(r) for r in rp2)
                    return len(matched)

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
                        # A row carrying only key columns has nothing to SET;
                        # mirror update()'s `if not len(row)` case and count
                        # the matched keys instead of compiling an empty
                        # (invalid) UPDATE.
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
                    rp = self.db.executable.execute(stmt, group_rows)
                    if rp.supports_sane_multi_rowcount():
                        updated += rp.rowcount
                    else:
                        # Dialect-dead on SQLite/PG/MySQL. Sub-batch via
                        # count_matched so duplicate keys are counted once.
                        updated += count_matched(group_rows)
                self.db._auto_commit()
                chunk = []
        return updated

    def upsert(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """An UPSERT is a smart combination of insert and update.

        If rows with matching ``keys`` exist they will be updated, otherwise a
        new row is inserted in the table.

        With ``ensure`` on (the default), an index on ``keys`` is created
        as a side effect. Pass ``ensure=False`` if you don't want that,
        e.g. on a locked-down or very large table.
        ::

            data = dict(id=10, title='I am a banana!')
            table.upsert(data, ['id'])
        """
        row = self._sync_columns(row, ensure, types=types)
        if self._check_ensure(ensure):
            self.create_index(keys)
        row_count = self.update(row, keys, ensure=False)
        if row_count == 0:
            return self.insert(row, ensure=False)
        return True

    def _upsert_stmt(
        self, group_cols: frozenset[str], norm_keys: Sequence[str]
    ) -> Insert:
        """Build one dialect-native upsert statement for a column group.

        The SET side references the proposed row values (``excluded`` /
        ``inserted``), so a single statement serves every row in the group
        under executemany. Unknown columns (possible with ``ensure=False``)
        are left out of the SET; the extra parameter keys are ignored at
        execute time, matching insert_many.
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

    def upsert_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> int:
        """Insert-or-update many rows at a time using a DB-native UPSERT.

        Each chunk is written with a single ``INSERT ... ON CONFLICT DO
        UPDATE`` (SQLite/PostgreSQL) or ``ON DUPLICATE KEY UPDATE`` (MySQL)
        per column group: the database decides row existence by SQL equality
        on ``keys``, atomically per statement.

        With ``ensure`` on (the default), a UNIQUE index on ``keys`` is
        created as a side effect — the conflict arbiter the native upsert
        requires. This raises :py:class:`DatasetError <dataset.DatasetError>`
        if the table already contains rows with duplicate values for
        ``keys``. With ``ensure=False`` a unique index or primary key on
        exactly ``keys`` must already exist, or the database raises its own
        error.

        Notes on SQL semantics (all backend-decided): ``None``-valued keys
        always insert (NULLs are distinct in a unique index); on MySQL the
        upsert fires on *any* unique key of the table, and its default
        collation treats ``'A'``/``'a'`` as duplicates; on PostgreSQL a key
        repeated *within* one chunk raises ("cannot affect row a second
        time") — deduplicate beforehand or lower ``chunk_size``.

        See :py:meth:`insert_many() <dataset.Table.insert_many>` for details
        on the other parameters.

        Returns the number of input rows processed (the insert/update split
        is not reported by executemany on any backend).
        """
        keys = ensure_strings(keys)

        # Sync table once up front: column creation is call-wide, so union
        # every row's keys for the _sync_columns pass (mirrors insert_many).
        sync_row: MutableRow = {}
        for row in rows:
            for key in row:
                if key not in sync_row:
                    sync_row[key] = row[key]
        self._sync_columns(sync_row, ensure, types=types)

        # Normalize key names now that the columns exist, so a
        # case-mismatched key (e.g. ['ID'] against an 'id' column) resolves
        # to the real column name used by the arbiter index and statement.
        norm_keys = [self._get_column_name(k) for k in keys]

        if self._check_ensure(ensure):
            self.create_index(norm_keys, unique=True)

        # One compiled statement per column set, cached across chunks.
        stmts: dict[frozenset[str], Insert] = {}
        processed = 0
        chunk: list[MutableRow] = []
        for index, row in enumerate(rows):
            chunk.append({self._get_column_name(k): v for k, v in row.items()})

            # Upsert when chunk_size is fulfilled or this is the last row
            if len(chunk) == chunk_size or index == len(rows) - 1:
                # Group by the exact column set (like insert_many): an
                # omitted column is left out of its group's INSERT and SET,
                # so the DB applies its default on insert and leaves the
                # column untouched on update.
                groups: dict[frozenset[str], list[MutableRow]] = {}
                for chunk_row in chunk:
                    groups.setdefault(frozenset(chunk_row), []).append(chunk_row)
                for group_cols, group_rows in groups.items():
                    stmt = stmts.get(group_cols)
                    if stmt is None:
                        stmt = stmts[group_cols] = self._upsert_stmt(
                            group_cols, norm_keys
                        )
                    self.db.executable.execute(stmt, group_rows)
                self.db._auto_commit()
                processed += len(chunk)
                chunk = []
        return processed

    def delete(self, *clauses: ColumnElement[bool], **filters: SQLWriteValue) -> int:
        """Delete rows from the table.

        Keyword arguments can be used to add column-based filters. The filter
        criterion will always be equality:
        ::

            table.delete(place='Berlin')

        If no arguments are given, all records are deleted.

        Returns the number of deleted rows.
        """
        if not self.exists:
            return 0
        clause = self._args_to_clause(filters, clauses=clauses)
        stmt = self.table.delete().where(clause)
        # On dialects without sane rowcount, rp.rowcount is unreliable; count
        # the matching rows BEFORE the delete (afterwards they are gone).
        # Dead on SQLite/PostgreSQL/MySQL (all sane) — parity with update().
        pre = 0
        if not self.db.executable.dialect.supports_sane_rowcount:
            pre = self.count(clause)
        rp = self.db.executable.execute(stmt)
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
                    self.db.metadata,
                    schema=self.db.schema,
                    autoload_with=self.db.executable,
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
                table = SQLATable(self.name, self.db.metadata, schema=self.db.schema)
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
                table.create(self.db.executable, checkfirst=True)
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
                self.db.op.add_column(self.name, column, schema=self.db.schema)
        self._reflect_table()
        self.db._auto_commit()

    def _sync_columns(
        self,
        row: WriteRow,
        ensure: bool | None,
        types: dict[str, ColumnType] | None = None,
    ) -> MutableRow:
        """Create missing columns (or the table) prior to writes.

        If automatic schema generation is disabled (``ensure`` is ``False``),
        this will remove any keys from the ``row`` for which there is no
        matching column.
        """
        ensure = self._check_ensure(ensure)
        types = types or {}
        types = {self._get_column_name(k): v for (k, v) in types.items()}
        out = {}
        sync_columns = {}
        for name, value in row.items():
            name = self._get_column_name(name)
            if self.has_column(name):
                out[name] = value
            elif ensure:
                _type = types.get(name)
                if _type is None:
                    _type = self.db.types.guess(value)
                sync_columns[name] = Column(name, _type)
                out[name] = value
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

    def _check_ensure(self, ensure: bool | None) -> bool:
        if ensure is None:
            return self.db.ensure_schema
        return ensure

    def _generate_clause(
        self, column: str, op: str, value: SQLWriteValue
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
                clauses.append(false())
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
                continue
            if ordering.startswith("-"):
                orderings.append(self.table.c[column].desc())
            else:
                orderings.append(self.table.c[column].asc())
        return orderings

    def _keys_to_args(
        self, row: WriteRow, keys: Sequence[str]
    ) -> tuple[MutableRow, MutableRow]:
        keys = [self._get_column_name(k) for k in ensure_strings(keys)]
        # A key column absent from the table (not merely from the row) would
        # compile to false() downstream, silently making insert_ignore/upsert
        # insert a duplicate every call and update() return 0. Raise instead.
        # The lenient false() posture of find/count/delete is unaffected —
        # only the write path routes through here.
        for k in keys:
            if not self.has_column(k):
                raise DatasetError(f"No such column: {k}")
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
        ::

            table.create_column('created_at', db.types.datetime)

        `type` corresponds to an SQLAlchemy type as described by
        `dataset.db.Types`. Additional keyword arguments are passed
        to the constructor of `Column`, so that default values, and
        options like `nullable` and `unique` can be set.
        ::

            table.create_column('key', unique=True, nullable=False)
            table.create_column('food', default='banana')
        """
        name = self._get_column_name(name)
        if self.has_column(name):
            log.debug(f"Column exists: {name}")
            return
        self._sync_table((Column(name, type, **kwargs),))  # type: ignore[arg-type]

    def create_column_by_example(self, name: str, value: SQLWriteValue) -> None:
        """
        Explicitly create a new column ``name`` with a type that is appropriate
        to store the given example ``value``.  The type is guessed in the same
        way as for the insert method with ``ensure=True``.
        ::

            table.create_column_by_example('length', 4.2)

        If a column of the same name already exists, no action is taken, even
        if it is not of the type we would have created.
        """
        type_ = self.db.types.guess(value)
        self.create_column(name, type_)

    def drop_column(self, name: str) -> None:
        """
        Drop the column ``name``.
        ::

            table.drop_column('created_at')

        """
        if self.db.engine is None:
            raise RuntimeError("Cannot drop columns when no engine is available.")
        if self.db.engine.dialect.name == "sqlite":
            raise RuntimeError("SQLite does not support dropping columns.")
        name = self._get_column_name(name)
        with self.db.lock:
            if not self.exists or not self.has_column(name):
                log.debug("Column does not exist: %s", name)
                return

            self._threading_warn()
            self.db.op.drop_column(self.table.name, name, schema=self.table.schema)
            self._reflect_table()
            self.db._auto_commit()

    def drop(self) -> None:
        """Drop the table from the database.

        Deletes both the schema and all the contents within it.
        """
        with self.db.lock:
            if self.exists:
                self._threading_warn()
                self.table.drop(self.db.executable, checkfirst=True)
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
            indexes = self.db.inspect.get_indexes(self.name, schema=self.db.schema)
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
        indexes = self.db.inspect.get_indexes(self.name, schema=self.db.schema)
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
        ::

            table.create_index(['name', 'country'])
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
                    raise DatasetError(f"No such column: {column}")

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
                mysql_length = {
                    col.name: 10
                    for col in columns_
                    if isinstance(col.type, MYSQL_LENGTH_TYPES)
                }
                kw["mysql_length"] = mysql_length
                if unique:
                    kw["unique"] = True

                idx = Index(name, *columns_, **kw)  # type: ignore[arg-type]
                if unique:
                    # Existing duplicate key values make the arbiter index
                    # impossible to build; surface that clearly, never swallow.
                    try:
                        idx.create(self.db.executable)
                    except IntegrityError as exc:
                        if not self.db.in_transaction:
                            # Leave the autobegun transaction usable (on
                            # PostgreSQL it is aborted until rolled back).
                            self.db.executable.rollback()
                        raise DatasetError(
                            f"Cannot create a unique index on {columns!r}: "
                            f"table {self.name!r} already contains rows with "
                            "duplicate values for these columns."
                        ) from exc
                else:
                    idx.create(self.db.executable)
                self.db._auto_commit()

    def find(
        self,
        *_clauses: ColumnElement[bool],
        _limit: int | None = None,
        _offset: int = 0,
        order_by: str | Sequence[str] | None = None,
        _streamed: bool = False,
        _step: int | None = QUERY_STEP,
        **kwargs: SQLWriteValue,
    ) -> ResultIter:
        """Perform a simple search on the table.

        Simply pass keyword arguments as ``filter``.
        ::

            results = table.find(country='France')
            results = table.find(country='France', year=1980)

        Using ``_limit``::

            # just return the first 10 rows
            results = table.find(country='France', _limit=10)

        You can sort the results by single or multiple columns. Append a minus
        sign to the column name for descending order::

            # sort results by a column 'year'
            results = table.find(country='France', order_by='year')
            # return all rows sorted by multiple columns (descending by year)
            results = table.find(order_by=['country', '-year'])

        ``order_by``, along with ``_limit``, ``_offset``, ``_step`` and
        ``_streamed``, are reserved parameter names: a column literally
        named e.g. ``order_by`` can't be passed as an equality filter
        through ``**kwargs``.

        You can also submit filters based on criteria other than equality,
        see :ref:`advanced_filters` for details.

        To run more complex queries with JOINs, or to perform GROUP BY-style
        aggregation, you can also use :py:meth:`db.query() <dataset.Database.query>`
        to run raw SQL queries instead.
        """
        if not self.exists:
            return ResultIter(None, row_type=self.db.row_type)
        if self.db.engine is None:
            raise RuntimeError("Cannot run queries when no engine is available.")

        if _step is False or _step == 0:
            _step = None

        orderings = self._args_to_order_by(order_by)
        args = self._args_to_clause(kwargs, clauses=_clauses)
        query = self.table.select().where(args).limit(_limit).offset(_offset)
        if len(orderings):
            query = query.order_by(*orderings)

        stream_conn = None
        conn = self.db.executable
        if _streamed:
            stream_conn = self.db.engine.connect()
            conn = stream_conn.execution_options(stream_results=True)

        return ResultIter(
            conn.execute(query),
            row_type=self.db.row_type,
            step=_step,
            connection=stream_conn,
        )

    def find_one(
        self, *args: ColumnElement[bool], **kwargs: SQLWriteValue
    ) -> OutRow | None:
        """Get a single result from the table.

        Works just like :py:meth:`find() <dataset.Table.find>` but returns one
        result, or ``None``.
        ::

            row = table.find_one(country='United States')
        """
        if not self.exists:
            return None

        resiter = self.find(*args, _limit=1, _step=None, **kwargs)  # type: ignore[arg-type]
        try:
            for row in resiter:
                return row
        finally:
            resiter.close()
        return None

    def count(self, *_clauses: ColumnElement[bool], **kwargs: SQLWriteValue) -> int:
        """Return the count of results for the given filter set."""
        # NOTE: this does not have support for limit and offset since I can't
        # see how this is useful. Still, there might be compatibility issues
        # with people using these flags. Let's see how it goes.
        if not self.exists:
            return 0

        args = self._args_to_clause(kwargs, clauses=_clauses)
        query = select(func.count()).where(args)
        query = query.select_from(self.table)
        rp = self.db.executable.execute(query)
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
        **kwargs: SQLWriteValue,
    ) -> ResultIter:
        """Return all the unique (distinct) values for the given ``columns``.
        ::

            # returns only one row per year, ignoring the rest
            table.distinct('year')
            # works with multiple columns, too
            table.distinct('year', 'country')
            # you can also combine this with a filter
            table.distinct('year', country='China')
        """
        if not self.exists:
            return ResultIter(None, row_type=self.db.row_type)

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
                    raise DatasetError(f"No such column: {column}")
                columns.append(self.table.c[column])

        clause = self._args_to_clause(kwargs, clauses=clauses)
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

    # Legacy methods for running find queries.
    all = find

    def __iter__(self) -> ResultIter:
        """Return all rows of the table as simple dictionaries.

        Allows for iterating over all rows in the table without explicitly
        calling :py:meth:`find() <dataset.Table.find>`.
        ::

            for row in table:
                print(row)
        """
        return self.find()

    def __repr__(self) -> str:
        """Get table representation."""
        return f"<Table({self.table.name})>"
