# lazyset

Lightweight Python library for reading/writing databases as easily as JSON — a thin layer over SQLAlchemy, no ORM models. Supports SQLite (default), PostgreSQL, MySQL. **Design bias: simplicity over features — don't over-engineer.**

**Hard fork of [pudo/dataset](https://github.com/pudo/dataset).** The distribution and import name are both `lazyset` (first release 0.1.0). This fork deliberately breaks from upstream: four self-describing write verbs (`insert`/`upsert`/`update`/`delete`, each taking one row **or** any iterable of rows), one honestly-named `auto_create` flag, and loud errors where 2.x was silently wrong. There are no upstream users of the fork to shepherd — see the fork-line policy under Must-follow.

## Layout
- `lazyset/database.py` — `Database`: connection, transactions, `query()`, `db[name]` / `db.table(...)` accessors
- `lazyset/table.py` — `Table`: CRUD, schema management, filter operators (`_generate_clause`)
- `lazyset/types.py` — type mapping (`Types.guess`), `ColumnType`
- `lazyset/util.py` — `Results`, name normalization, exceptions, type aliases
- `test/` — pytest; `conftest.py` = `db` + `table` fixtures, `sample_data.py` = shared test data, `test_properties.py` = Hypothesis tests for pure helpers

## Commands
- Deps/env: `uv` (`uv run`, `uv sync`). Run tests: `make test` (or `uv run pytest`).
- `make lint` — ruff + `mypy --strict` (only `lazyset/` is typed; ruff also checks `test/`). Run before committing.
- `make format` — apply ruff format. Test another backend with `DATABASE_URL=postgresql://… make test`.
- Mutation testing: `uv run mutmut run` then `uv run mutmut results` (dev dep). Known-accepted survivor buckets are documented in `CHANGELOG.md` under 2.0.0 — diff against that baseline rather than re-triaging from scratch.

## Must-follow
- **Keep docs current:** when you change code, commands, deps, or public API, update the docs the change touches in the same pass — this `CLAUDE.md`, `README.md`, the docstrings, `docs/*.md`, `CHANGELOG.md`. A stale doc is a bug.
- **Multi-backend:** every change must work on SQLite, PostgreSQL, AND MySQL. Branch on `db.is_sqlite` / `is_postgres` / `is_mysql`. MySQL text indexes need a prefix length (10 chars). `drop_column` is attempted on every backend and the DB decides (SQLite ≥ 3.35 supports it; older SQLite raises).
- **Sane single-row rowcount is assumed:** `update()`/`delete()` return `rp.rowcount` directly, with no COUNT fallback — lazyset requires a dialect where `supports_sane_rowcount` holds (SQLite, PostgreSQL and MySQL all do). The *multi*-row fallback in `_flush_update_chunk` stays: `supports_sane_multi_rowcount` is genuinely False on psycopg2.
- **Fork-line — breaking changes are sanctioned:** this is a hard fork with no upstream users to shepherd, so renaming/removing public API is allowed when it makes the surface clearer. The redesign does exactly that: `ensure`→`auto_create`; `insert_many`/`update_many`/`upsert_many` folded into `insert`/`update`/`upsert` (`Mapping` **or** `Iterable[Mapping]`, dispatched by shape); `chunked.py` deleted in favour of `insert(gen, chunk_size=N)`; accessors collapsed to `db[name]` / `db.table(...)`; `ResultIter`→`Results`, `OutRow`→`Row`. Rules that still bind: rename deliberately, land the change with its doc + test sweep in the **same pass**, and record every break in `CHANGELOG.md`. `create_column`'s `type` param shadows the builtin — keep it (`# noqa`); a cosmetic rename buys nothing.
- **Auto-commit:** call `db._auto_commit()` after any write made outside an explicit transaction.
- **Thread safety:** hold `self.db.lock` for schema ops; connections are thread-local; transaction depth lives in `self.local.tx`.
- **Don't mutate input rows:** copy to `dict()` before modifying (see `update`, `insert` iterable paths).
- **Delegate to SQLAlchemy and the database.** Prefer built-in SQLAlchemy behavior and database-enforced semantics over re-implementing them in Python:
  - Let SQLAlchemy bind and render values (bind parameters, type coercion, identifier quoting, dialect literal rendering) — never hand-roll SQL-value serialization.
  - Let the database decide row existence, equality, collation, NULL semantics, server-side defaults, and identifier limits — don't classify/compare in Python where SQL semantics (NULL, type affinity, case-insensitive collation) would differ.
  - A "performance" rewrite that moves such a decision into Python must clear correctness-vs-the-DB as its acceptance bar. Reintroducing a hand-rolled version of what the stack already does correctly is a bug, not an optimization.
- **Version:** bump with `bump2version`, never edit `lazyset/__init__.py` directly.

## Conventions
- **Types:** all `lazyset/` code passes `mypy --strict` and ships `py.typed`. Use `WriteRow` (`Mapping`) at public boundaries, `MutableRow` (`dict`) internally where mutation happens — copy only at that boundary. `ColumnType` for `primary_type` / `create_column`. Reach for `# type: ignore` only for SQLAlchemy-stub gaps or `**kwargs` forwarding.
- **Columns:** match case-insensitively via `_get_column_name()`; `_column_keys` preserves the real DB names.
- **Errors:** `DatasetError` is the base, `QueryError` for invalid filters; name exception classes with an `Error` suffix.
- **Python 3.10+:** `X | None`, builtin generics (`list[str]`, `dict[...]`).

## Transactions (SQLAlchemy 2.x)
2.x "autobegin" starts a transaction on first use; `_auto_commit()` commits after writes when no explicit transaction is open. Nested and non-nested transactions must both work — test both, and with multiple threads. See `begin()` / `commit()` / `rollback()` in `database.py`.

## Docs & scope
API reference is generated from docstrings with **pdoc** (`make docs` → `site/`) and published to **GitHub Pages** by CI (`ci.yml`). The two narrative guides live as `docs/quickstart.md` / `docs/queries.md` and are pulled into the `lazyset` landing page via pdoc's `.. include::` in `lazyset/__init__.py` (paths resolve against the source checkout, not the wheel). Docstring cross-refs are bare backtick identifiers (`` `Table.find` ``), not Sphinx roles. Changelog in `CHANGELOG.md`. Release: `bump2version` → update `CHANGELOG.md` → `make dists` → push a signed tag `vX.Y.Z` (tag push auto-publishes to PyPI via `cd.yml`).
Out of scope: FK/relations, Python-side JOINs, async. DB-native UPSERT is the single `upsert()` algorithm (`ON CONFLICT DO UPDATE` / `ON DUPLICATE KEY UPDATE`); key-only rows degrade to `DO NOTHING` / ODKU-noop, which is why there is no separate `insert_ignore()`. `upsert()` requires `keys` as the conflict arbiter and creates the UNIQUE arbiter index under `auto_create`.
