# dataset

Lightweight Python library for reading/writing databases as easily as JSON — a thin layer over SQLAlchemy, no ORM models. Supports SQLite (default), PostgreSQL, MySQL. **Design bias: simplicity over features — don't over-engineer.**

**Hard fork of [pudo/dataset](https://github.com/pudo/dataset).** The import name stays `dataset`; the distribution/repo identity is the fork's own (name TBD). The 3.0 line deliberately breaks from upstream: five self-describing write verbs (`insert`/`insert_ignore`/`upsert`/`update`/`delete`, each taking one row **or** any iterable of rows), one honestly-named `auto_create` flag, and loud errors where 2.x was silently wrong. There are no upstream users of the fork to shepherd — see the fork-line policy under Must-follow.

## Layout
- `dataset/database.py` — `Database`: connection, transactions, `query()`, `db[name]` / `db.table(...)` accessors
- `dataset/table.py` — `Table`: CRUD, schema management, filter operators (`_generate_clause`)
- `dataset/types.py` — type mapping (`Types.guess`), `ColumnType`
- `dataset/util.py` — `Results`, name normalization, exceptions, type aliases
- `test/` — pytest; `conftest.py` = `db` + `table` fixtures, `sample_data.py` = shared test data, `test_properties.py` = Hypothesis tests for pure helpers

## Commands
- Deps/env: `uv` (`uv run`, `uv sync`). Run tests: `make test` (or `uv run pytest`).
- `make lint` — ruff + `mypy --strict` (only `dataset/` is typed; ruff also checks `test/`). Run before committing.
- `make format` — apply ruff format. Test another backend with `DATABASE_URL=postgresql://… make test`.
- Mutation testing: `uv run mutmut run` then `uv run mutmut results` (dev dep). Known-accepted survivor buckets are documented in `CHANGELOG.md` under 2.0.0 — diff against that baseline rather than re-triaging from scratch.

## Must-follow
- **Keep docs current:** when you change code, commands, deps, or public API, update the docs the change touches in the same pass — this `CLAUDE.md`, `README.md`, `docs/*.rst`, `CHANGELOG.md`. A stale doc is a bug.
- **Multi-backend:** every change must work on SQLite, PostgreSQL, AND MySQL. Branch on `db.is_sqlite` / `is_postgres` / `is_mysql`. MySQL text indexes need a prefix length (10 chars). `drop_column` is attempted on every backend and the DB decides (SQLite ≥ 3.35 supports it; older SQLite raises).
- **Fork-line — breaking changes are sanctioned:** this is a hard fork with no upstream users to shepherd, so renaming/removing public API is allowed when it makes the surface clearer. The 3.0 redesign does exactly that: `ensure`→`auto_create`; `insert_many`/`update_many`/`upsert_many` folded into `insert`/`update`/`upsert` (`Mapping` **or** `Iterable[Mapping]`, dispatched by shape); `chunked.py` deleted in favour of `insert(gen, chunk_size=N)`; accessors collapsed to `db[name]` / `db.table(...)`; `ResultIter`→`Results`, `OutRow`→`Row`. Rules that still bind: rename deliberately, land the change with its doc + test sweep in the **same pass**, and record every break in `CHANGELOG.md`. `create_column`'s `type` param shadows the builtin — keep it (`# noqa`); a cosmetic rename buys nothing.
- **Auto-commit:** call `db._auto_commit()` after any write made outside an explicit transaction.
- **Thread safety:** hold `self.db.lock` for schema ops; connections are thread-local; transaction depth lives in `self.local.tx`.
- **Don't mutate input rows:** copy to `dict()` before modifying (see `update`, `insert` iterable paths).
- **Delegate to SQLAlchemy and the database.** Prefer built-in SQLAlchemy behavior and database-enforced semantics over re-implementing them in Python:
  - Let SQLAlchemy bind and render values (bind parameters, type coercion, identifier quoting, dialect literal rendering) — never hand-roll SQL-value serialization.
  - Let the database decide row existence, equality, collation, NULL semantics, server-side defaults, and identifier limits — don't classify/compare in Python where SQL semantics (NULL, type affinity, case-insensitive collation) would differ.
  - A "performance" rewrite that moves such a decision into Python must clear correctness-vs-the-DB as its acceptance bar. Reintroducing a hand-rolled version of what the stack already does correctly is a bug, not an optimization.
- **Version:** bump with `bump2version`, never edit `dataset/__init__.py` directly.

## Conventions
- **Types:** all `dataset/` code passes `mypy --strict` and ships `py.typed`. Use `WriteRow` (`Mapping`) at public boundaries, `MutableRow` (`dict`) internally where mutation happens — copy only at that boundary. `ColumnType` for `primary_type` / `create_column`. Reach for `# type: ignore` only for SQLAlchemy-stub gaps or `**kwargs` forwarding.
- **Columns:** match case-insensitively via `_get_column_name()`; `_column_keys` preserves the real DB names.
- **Errors:** `DatasetError` is the base, `QueryError` for invalid filters; name exception classes with an `Error` suffix.
- **Python 3.10+:** `X | None`, builtin generics (`list[str]`, `dict[...]`).

## Transactions (SQLAlchemy 1.4–2.x)
2.x "autobegin" starts a transaction on first use; `_auto_commit()` commits after writes when no explicit transaction is open. Nested and non-nested transactions must both work — test both, and with multiple threads. See `begin()` / `commit()` / `rollback()` in `database.py`.

## Docs & scope
API + guides: https://dataset.readthedocs.io/ · source in `docs/` (Sphinx/RST, `cd docs && make html`) · changelog in `CHANGELOG.md`. Release: `bump2version` → update `CHANGELOG.md` → `make dists` → push a signed tag `vX.Y.Z` (tag push auto-publishes to PyPI via GitHub Actions).
Out of scope: FK/relations, Python-side JOINs, async. DB-native UPSERT is the single `upsert()` algorithm (`ON CONFLICT DO UPDATE` / `ON DUPLICATE KEY UPDATE`), and `insert_ignore()` is native `DO NOTHING` / ODKU-noop. Both require `keys` as the conflict arbiter and create the UNIQUE arbiter index under `auto_create`.
