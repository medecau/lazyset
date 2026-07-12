# dataset ChangeLog

*The changelog has only been started with version 0.3.12, previous
changes must be reconstructed from revision history.*

* **2.0.0**: Major modernization and type annotations
  - **Type annotations**: Full `mypy --strict` compliance across all modules
  - **PEP 561**: Added `py.typed` marker for downstream type checking
  - **New types**: Exported `OutRow`, `RowFactory`, `QueryError` for downstream use
  - **`RowFactory`**: The `row_type` parameter is now typed as `Callable[[Iterable[tuple[str, Any]]], OutRow]` instead of `type`
  - **`QueryError`**: New exception subclass of `DatasetError` for invalid filter operations
  - **`primary_type`**: Changed from `Types` to `ColumnType` (SQLAlchemy `TypeEngine`) — the actual accepted type
  - **`insert`/`insert_ignore`/`upsert`**: Return type changed from `int | bool` to `Any` (primary keys can be any type)
  - **`update`**: Always returns the number of updated rows (`int`); removed the `return_count` parameter — the count is now returned unconditionally (return type was `bool | int`) *(breaking)*
  - **`delete`**: Now returns the number of deleted rows (`int`) instead of a bool *(breaking)*
  - **`insert_many`/`update_many`/`upsert_many`**: Now return the number of rows affected (`int`) instead of `None`, matching their singular siblings (`insert`/`update`/`upsert`) *(breaking)*
  - **Removed `banal` dependency**: Replaced `ensure_list` with typed `ensure_strings` utility
  - **`update_many`**: Fixed mutation of input rows — rows are now copied before modification
  - **`update_many`**: Fixed a bug where the SET clause accumulated every column seen across the whole call (and across chunks), NULLing out any column missing from a given row instead of leaving it untouched
  - **`upsert_many`**: Rewritten as a batched partition-then-bulk operation (union-sync columns once per batch, single exists-check, delegate to `update_many`/`insert_many`) instead of a per-row loop — substantially faster on large inputs. The exists-check itself is now split into sub-batches of 500 keys, independent of `chunk_size`: at the default `chunk_size=1000`, a single OR-of-AND clause covering 999+ distinct keys exceeded SQLite's expression-tree depth limit (1000) and crashed out of the box
  - **`safe_url`**: Only the userinfo password is masked; a `:password@` sequence appearing in the path or query is no longer mangled
  - **`normalize_column_name`**: Now rejects a column name whose only invalid character (`.` or `-`) lies past byte 63, instead of silently truncating-and-accepting it — charset validation runs before length truncation *(minor behavior change)*
  - **`index_name`**: Auto-generated index names now use an injective column join, so distinct column sets no longer collide; generated names for auto-indexes change (cosmetic — names are never used for lookup)
  - **`create_table(primary_id=False)`**: A columnless table now defers creation until the first column is added, instead of eagerly emitting `CREATE TABLE t ()` — this previously failed on SQLite and MySQL; PostgreSQL now behaves the same way for consistency *(minor behavior change)*
  - **Known limitation (documented, not new)**: on SQLite and MySQL, schema DDL (table/column creation) is not transactional. Run inside an explicit `db.begin()`, it commits immediately and is not undone by a later `rollback()`, even single-threaded.
  - **Dev tooling**: Added `mypy` to dev dependencies, `make lint` now runs both ruff and mypy
  - **Mutation testing**: Added `mutmut` (dev-only dependency) and ~50 targeted tests, closing
    SQLite-killable mutation survivors from 333 to 115. The remaining survivors fall into two
    accepted, out-of-scope buckets — not gaps:
    - *Dialect-guarded* (~20): code paths that never execute on SQLite, e.g. `drop_column`'s body,
      `create_index`'s MySQL text-length prefix, `has_index`'s `schema=` forward, and
      `Database.__init__`'s postgres/mysql dialect-name literals. Killable only under
      `DATABASE_URL=postgresql://… uv run mutmut run` (or MySQL).
    - *Behaviorally equivalent* (~95): no correct assertion can distinguish the mutant from the
      original, e.g. `and_(True, *clauses)` (no-op identity), `_step`/`chunk_size` boundary swaps,
      `WHERE NULL` vs `WHERE false()` (both exclude all rows), `fetchall` vs `fetchmany` batching,
      `checkfirst`/`autoincrement` defaults, WAL PRAGMA case-folding, `_indexes` cache poisoning.
    Re-triaging from scratch isn't needed for a future `mutmut run`; diff against this baseline.

    **Update (post `Table` hardening pass)**: the fixes/rewrites above (`update_many`, `upsert_many`,
    `insert_ignore`, `has_index`, `create_index`, `_sync_table`, `_reflect_table`, `distinct`, and
    more) substantially grew `table.py`'s branch count, so a from-scratch `mutmut run` now generates
    1204 candidate mutants (up from 333) with 126 survivors (up from 115 in raw count, but a smaller
    proportion of the total). Of these, 73 sit in functions touched by this pass and were individually
    re-triaged: 14 were genuine new gaps, closed with targeted tests (e.g. `insert_ignore`'s
    untested new-row return value, `update_many`/`upsert_many`'s row-count accumulation across
    multiple column-groups/batches, `update()`'s tolerant-missing-key-column path, `distinct()` on a
    table that doesn't exist yet); the remaining 59 are dialect-guarded or behaviorally equivalent,
    matching the categories above. The other 67 survivors sit in code this pass didn't touch and were
    not individually re-reviewed — assumed to be the same pre-existing buckets, per the note above.
  - **Second hardening pass (variant analysis)**: fixed a batch of validated sibling bugs of the
    first hardening pass. Behavior changes worth noting:
    - **`update`/`insert_ignore`/`upsert`**: now raise `DatasetError("No such column: …")` when a
      key column is absent from the table, instead of silently inserting a duplicate row
      (`insert_ignore`/`upsert` with `ensure=False`) or returning 0 (`update`). The lenient
      `find`/`count`/`delete` read path is unchanged. *(behavior change)*
    - **`update_many`**: now honours its previously-dead `ensure`/`types` params — a new value
      column is created before the UPDATE, and an empty write to a deferred `primary_id=False`
      table raises a clear `DatasetError` instead of a raw `CompileError`/`KeyError`. *(behavior
      change)*
    - **`normalize_column_key`**: no longer collapses internal spaces (kept only the deliberate
      case/whitespace folding, `upper().strip()`). Columns like `"full name"` and `"fullname"`
      are now distinct instead of silently conflated on a reflected schema. *(behavior change)*
  - **`upsert_many`**: Now uses the database's native UPSERT (`INSERT … ON CONFLICT DO UPDATE` on
    SQLite/PostgreSQL, `ON DUPLICATE KEY UPDATE` on MySQL) with a UNIQUE arbiter index on `keys`,
    instead of a Python SELECT-then-classify pass. Row identity is decided by SQL equality — this
    fixes `'5'` vs `5` creating duplicate rows, `None`-valued keys silently vanishing, and the
    classify-then-write race window. Behavior changes: *(behavior change)*
    - `ensure=False` now requires a pre-existing unique index or primary key on exactly `keys`
      (the arbiter); without one the database raises its own error. The old path needed no index.
    - The `ensure` side effect now creates a **UNIQUE** index (named `uq_…`, distinct from the
      `ix_…` name the singular `upsert()` creates). If the table already contains rows with
      duplicate values for `keys`, this raises a clear `DatasetError`.
    - `None`-valued keys always INSERT (NULLs are distinct in a unique index), unlike the
      singular `upsert()`'s IS NULL matching.
    - The return value now counts input rows processed; within-call repeated keys are no longer
      collapsed before counting, and the updated-vs-inserted split is not reported.
    - Backend-decided caveats: on MySQL the upsert fires on *any* unique key (not just `keys`) and
      the default collation merges `'A'`/`'a'`; on PostgreSQL a key repeated within one chunk
      raises ("cannot affect row a second time") — dedupe first or lower `chunk_size`.
  - **`create_index`**: New optional `unique=` flag (gated on an exact-column unique index/PK, not
    `has_index`'s prefix match); `dataset.util.index_name` gains a `prefix=` parameter.
  - **`in`/`notin` filters**: Values are passed as ordinary (expanding) bind parameters again,
    delegating rendering and type coercion to SQLAlchemy — an earlier inline-literal rewrite
    (`literal_execute`) crashed on `bytes` and could mis-render other types. Lists larger than the
    backend's bind-variable limit (~32k on modern SQLite, 65535 on PostgreSQL/MySQL) now raise the
    backend's own error; chunk huge IN-lists in the caller. *(behavior change)*
  - **Identifier length caps**: `normalize_column_name`/`normalize_table_name` now apply an
    identifier-length cap **only on PostgreSQL** (which truncates identifiers to 63 bytes
    server-side; emulating it keeps our in-memory name equal to the stored one) via a new optional
    `max_bytes=` parameter. On SQLite and MySQL no cap is applied at all — the database owns
    identifier length (MySQL's limit is 64 *characters* and it errors on overflow; SQLite is
    unbounded). This drops the earlier unconditional 63-*character* cap, which silently
    truncated/forked long or reflected names on those backends: a reflected >63-char SQLite column
    was normalized to a nonexistent 63-char name, so `_column_keys` (store) and `_get_column_name`
    (lookup) — both running this same normalization — disagreed with the real DB column.
    Auto-generated index names keep their own unconditional byte cap, independent of this
    parameter. *(behavior change)*
  - **Build system**: Migrated from setuptools to modern pyproject.toml with Hatchling (PEP 621)
  - **Linting**: Replaced flake8 with ruff for faster, more comprehensive linting
  - **CI/CD**: Updated GitHub Actions to use modern action versions (checkout@v4, setup-python@v5)
  - **SQLAlchemy 2.x**: Full support for SQLAlchemy 2.0+ with backward compatibility to 1.4.0
  - **Transaction handling**: Fixed autobegin semantics and DDL lock contention for SQLAlchemy 2.x
  - **Testing**: Switched from nose to pytest, improved test fixtures and cleanup
  - **Database support**: Added lock timeout configurations for PostgreSQL and MySQL in CI
  - **Python support**: Now requires Python 3.10+, tested on 3.10-3.13
  - **Documentation**: Updated installation instructions, copyright year, and added comprehensive CLAUDE.md
  - **Metadata**: Changed development status from Alpha to Production/Stable
  - **License**: Renamed LICENSE.txt to LICENSE for standard convention
  - **Dependencies**: Updated SQLAlchemy constraint to allow versions up to 3.0.0
  - **Filter operators**: An unrecognized operator (e.g. a typo like `{'contains': …}`) now raises
    `QueryError` instead of silently returning an empty result *(behavior change)*
  - **`has_index`**: Now matches an ordered leftmost prefix of an existing index (or the primary
    key), not any column subset — `upsert`/`insert_ignore` may create one extra index on first use
    against tables whose index only covered the old, looser match *(behavior change)*
  - **`insert_many`/`ChunkedInsert`**: A column omitted from a given row now takes the database's
    server-side default (or NULL) instead of an explicitly bound NULL that overrode
    `server_default`; rows may be reordered within a chunk (grouped by column set);
    `pad_chunk_columns` was removed from `dataset.util` and `ChunkedInsert` no longer has a
    `fields` attribute *(behavior change)*
  - **`ChunkedUpdate`**: `flush()` now issues a single `update_many` per flush and lets it group
    the queue internally, instead of pre-grouping with `sort`/`groupby` first (which relied on the
    partial-order `dict_keys.__lt__` and could scatter a keyset across non-adjacent groups). Fewer
    commits, identical net writes; no public API change (internal)
  - **`distinct()`/`create_index()`**: `distinct()` with no column names and `create_index()` on a
    column that doesn't exist now raise `DatasetError` instead of silently returning nothing /
    creating nothing *(behavior change)*
  - **`make_sqlite_url`**: The URI form now percent-encodes the database path, so `?`/`#`/`%` in a
    filename can't mangle the query string *(behavior change)*
* 1.6.2: Fix distinct() to respect _limit and _offset parameters (#424).
* 1.6.1: Fix add_column method compatibility with Alembic 1.11+ (#423).
* 1.6.0: Pin SQLAlchemy below 2.0.0 for compatibility.
* 1.5.2: Consider primary key when checking for indexes (#382). Add missing arguments for query method (#391).
* 1.5.1: Improve row conversion compatibility across SQLAlchemy 1.3 and 1.4.
* 1.5.0: Add support for custom SQLite pragmas via `on_connect_statements` parameter. Switch from nose to pytest for testing.
* 1.2.0: Add support for views, multiple comparison operators.
  Remove support for Python 2.
* 1.1.0: Introduce `types` system to shortcut for SQLA types.
* 1.0.0: Massive re-factor and code cleanup.
* 0.6.0: Remove sqlite_datetime_fix for automatic int-casting of dates,
  make table['foo', 'bar'] an alias for table.distinct('foo', 'bar'),
  check validity of column and table names more thoroughly, rename
  reflectMetadata constructor argument to reflect_metadata, fix
  ResultIter to not leave queries open (so you can update in a loop).
* 0.5.7: dataset Databases can now have customized row types. This allows,
  for example, information to be retrieved in attribute-accessible dict
  subclasses, such as stuf.
* 0.5.4: Context manager for transactions, thanks to @victorkashirin.
* 0.5.1: Fix a regression where empty queries would raise an exception.
* 0.5: Improve overall code quality and testing, including Travis CI.
  An advanced __getitem__ syntax which allowed for the specification 
  of primary keys when getting a table was dropped. 
  DDL is no longer run against a transaction, but the base connection. 
* 0.4: Python 3 support and switch to alembic for migrations.
* 0.3.15: Fixes to update and insertion of data, thanks to @cli248
  and @abhinav-upadhyay.
* 0.3.14: dataset went viral somehow. Thanks to @gtsafas for
  refactorings, @alasdairnicol for fixing the Freezfile example in 
  the documentation. @diegoguimaraes fixed the behaviour of insert to
  return the newly-created primary key ID. table.find_one() now
  returns a dict, not an SQLAlchemy ResultProxy. Slugs are now generated
  using the Python-Slugify package, removing slug code from dataset. 
* 0.3.13: Fixed logging, added support for transformations on result
  rows to support slug generation in output (#28).
* 0.3.12: Makes table primary key's types and names configurable, fixing
  #19. Contributed by @dnatag.
