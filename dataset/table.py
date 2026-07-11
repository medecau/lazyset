import logging
import threading
import warnings
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import false, func, select
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.schema import Column, Index
from sqlalchemy.schema import Table as SQLATable
from sqlalchemy.sql import and_, expression, or_
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
    # The OR-of-AND existence check (upsert_many's classifier and
    # update_many's non-sane-multi-rowcount fallback) builds one clause per
    # distinct key; SQLite's default expression-tree depth limit is 1000, so
    # it is sub-batched at this size independently of the caller's chunk_size.
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
        if self._table is None:
            self._sync_table(())
        if self._table is None:
            # Deferred columnless auto-create table: transient view until
            # the first column is added.
            return SQLATable(self.name, self.db.metadata, schema=self.db.schema)
        return self._table

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
            chunk.append(dict(row))

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
            key_prefix = "_" + key_prefix
        val_prefix = "v_"
        while any(name.startswith(val_prefix) for name in existing):
            val_prefix = "_" + val_prefix

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

                for value_cols, group_items in groups.items():
                    cols_list = sorted(value_cols)
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
                    group_rows: list[MutableRow] = []
                    for key_values, value_dict in group_items:
                        group_row: MutableRow = {
                            f"{key_prefix}{i}": v for i, v in enumerate(key_values)
                        }
                        for j, col in enumerate(cols_list):
                            group_row[f"{val_prefix}{j}"] = value_dict[col]
                        group_rows.append(group_row)

                    rp = self.db.executable.execute(stmt, group_rows)
                    if rp.supports_sane_multi_rowcount():
                        updated += rp.rowcount
                    else:
                        # Dialect-dead on SQLite/PG/MySQL. Sub-batch the
                        # existence check (SQLite caps the expression tree at
                        # 1000) and union the matched key tuples so duplicate
                        # keys are counted once, not summed.
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
                        updated += len(matched)
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

    def upsert_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> int:
        """
        Sorts multiple input rows into upserts and inserts. Inserts are passed
        to insert and upserts are updated.

        See :py:meth:`upsert() <dataset.Table.upsert>` and
        :py:meth:`insert_many() <dataset.Table.insert_many>`.

        Returns the number of rows updated plus the number of rows inserted.
        """
        # 2b1947e replaced a bulk implementation with a one-by-one loop,
        # since doing it in bulk ran into column-creation issues. The batch
        # below resolves that by union-syncing columns once per batch before
        # classifying rows, mirroring insert_many's own pre-scan.
        keys = ensure_strings(keys)

        updated = 0
        inserted = 0
        batch: list[WriteRow] = []
        for index, row in enumerate(rows):
            batch.append(row)

            # Upsert when chunk_size is fulfilled or this is the last row
            if len(batch) == chunk_size or index == len(rows) - 1:
                # Last-wins dedup by key-tuple: a single up-front exists
                # check can't see one row's insert when classifying the next
                # row in the same batch, so only the last occurrence per key
                # is ever written. A row that doesn't specify every key
                # column at all (e.g. relying on an autoincrement id) has no
                # real key identity to dedup on, so it always goes straight
                # to insert, same as the row-by-row loop did.
                # Normalize keys and each row's column names so a
                # case-mismatched key (e.g. ['ID'] against an 'id' column)
                # classifies and matches correctly, instead of KeyError-ing on
                # the exact-match column collection or silently rerouting the
                # row to a duplicate INSERT. Storing the normalized rows keeps
                # the downstream update_many/insert_many aligned to the real
                # column names.
                norm_keys = [self._get_column_name(k) for k in keys]
                deduped: dict[tuple[SQLWriteValue, ...], MutableRow] = {}
                always_insert: list[MutableRow] = []
                for row_ in batch:
                    norm_row = {
                        self._get_column_name(c): v for c, v in row_.items()
                    }
                    if all(k in norm_row for k in norm_keys):
                        key_tuple = tuple(norm_row[k] for k in norm_keys)
                        deduped[key_tuple] = norm_row
                    else:
                        always_insert.append(norm_row)

                sample: MutableRow = {}
                for synced_row in (*deduped.values(), *always_insert):
                    for k, v in synced_row.items():
                        if k not in sample:
                            sample[k] = v
                self._sync_columns(sample, ensure, types=types)

                if self._check_ensure(ensure):
                    self.create_index(norm_keys)

                # Checked in its own sub-batches, independent of the
                # caller's chunk_size (see _EXISTS_CHECK_BATCH above).
                existing_keys: set[tuple[SQLWriteValue, ...]] = set()
                all_key_tuples = list(deduped.keys())
                batch_size = self._EXISTS_CHECK_BATCH
                for start in range(0, len(all_key_tuples), batch_size):
                    key_tuples = all_key_tuples[start : start + batch_size]
                    clause = or_(
                        *(
                            and_(
                                *(
                                    self.table.c[k] == v
                                    for k, v in zip(norm_keys, key_tuple, strict=True)
                                )
                            )
                            for key_tuple in key_tuples
                        )
                    )
                    rp = self.db.executable.execute(
                        select(*(self.table.c[k] for k in norm_keys)).where(clause)
                    )
                    existing_keys.update(tuple(result_row) for result_row in rp)

                to_update = [
                    row_ for kt, row_ in deduped.items() if kt in existing_keys
                ]
                to_insert = always_insert + [
                    row_ for kt, row_ in deduped.items() if kt not in existing_keys
                ]

                if to_update:
                    updated += self.update_many(
                        to_update, norm_keys, len(to_update), ensure=False
                    )
                if to_insert:
                    inserted += self.insert_many(
                        to_insert, len(to_insert), ensure=False
                    )

                batch = []
        return updated + inserted

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
        rp = self.db.executable.execute(stmt)
        self.db._auto_commit()
        return rp.rowcount

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
                self._table = table
                self._table.create(self.db.executable, checkfirst=True)
                self._columns = None
                self.db._auto_commit()
        elif len(columns):
            with self.db.lock:
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
        if op in ("like",):
            return self.table.c[column].like(value)
        if op in ("ilike",):
            return self.table.c[column].ilike(value)
        if op in ("notlike",):
            return self.table.c[column].notlike(value)
        if op in ("notilike",):
            return self.table.c[column].notilike(value)
        if op in (">", "gt"):
            return self.table.c[column] > value
        if op in ("<", "lt"):
            return self.table.c[column] < value
        if op in (">=", "gte"):
            return self.table.c[column] >= value
        if op in ("<=", "lte"):
            return self.table.c[column] <= value
        if op in ("=", "==", "is"):
            return self.table.c[column] == value
        if op in ("!=", "<>", "not"):
            return self.table.c[column] != value
        if op in ("in",):
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"'in' filter requires a list, got {type(value)}")
            return self.table.c[column].in_(value)
        if op in ("notin",):
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"'notin' filter requires a list, got {type(value)}")
            return self.table.c[column].notin_(value)
        if op in ("between", ".."):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError("'between' filter requires a list of two values")
            start, end = value
            return self.table.c[column].between(start, end)
        if op in ("startswith",):
            if not isinstance(value, str):
                raise QueryError("'startswith' filter requires a string")
            return self.table.c[column].startswith(value, autoescape=True)
        if op in ("endswith",):
            if not isinstance(value, str):
                raise QueryError("'endswith' filter requires a string")
            return self.table.c[column].endswith(value, autoescape=True)
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
                for op, op_value in value.items():
                    clauses.append(self._generate_clause(column, op, op_value))
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
        row_ = dict(row)
        args = {k: row_.pop(k, None) for k in keys}
        return args, row_

    def create_column(
        self,
        name: str,
        type: ColumnType,
        **kwargs: object,  # noqa: A002
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

    def create_index(
        self, columns: Sequence[str], name: str | None = None, **kw: object
    ) -> None:
        """Create an index to speed up queries on a table.

        If no ``name`` is given, a deterministic name is generated from the
        table and column names.
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

            if not self.has_index(columns):
                self._threading_warn()
                name = name or index_name(self.name, columns)
                columns_ = [self.table.c[c] for c in columns]

                # MySQL crashes out if you try to index very long text fields,
                # apparently. This defines (a somewhat random) prefix that
                # will be captured by the index, after which I assume the engine
                # conducts a more linear scan:
                mysql_length = {}
                for col in columns_:
                    if isinstance(col.type, MYSQL_LENGTH_TYPES):
                        mysql_length[col.name] = 10
                kw["mysql_length"] = mysql_length

                idx = Index(name, *columns_, **kw)  # type: ignore[arg-type]
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
