# dataset

Lightweight Python library for reading/writing databases as easily as JSON — a thin layer over SQLAlchemy, no ORM models. Supports SQLite (default), PostgreSQL, MySQL. **Design bias: simplicity over features — don't over-engineer.**

## Layout
- `dataset/database.py` — `Database`: connection, transactions, `query()`
- `dataset/table.py` — `Table`: CRUD, schema management, filter operators (`_generate_clause`)
- `dataset/types.py` — type mapping (`Types.guess`), `ColumnType`
- `dataset/util.py` — `ResultIter`, name normalization, type aliases
- `dataset/chunked.py` — `ChunkedInsert` / `ChunkedUpdate`
- `test/` — pytest; `conftest.py` = `db` + `table` fixtures, `sample_data.py` = shared test data, `test_properties.py` = Hypothesis tests for pure helpers

## Commands
- Deps/env: `uv` (`uv run`, `uv sync`). Run tests: `make test` (or `uv run pytest`).
- `make lint` — ruff + `mypy --strict` (only `dataset/` is typed; ruff also checks `test/`). Run before committing.
- `make format` — apply ruff format. Test another backend with `DATABASE_URL=postgresql://… make test`.

## Must-follow
- **Keep docs current:** when you change code, commands, deps, or public API, update the docs the change touches in the same pass — this `CLAUDE.md`, `README.md`, `docs/*.rst`, `CHANGELOG.md`. A stale doc is a bug.
- **Multi-backend:** every change must work on SQLite, PostgreSQL, AND MySQL. Branch on `db.is_sqlite` / `is_postgres` / `is_mysql`. SQLite can't drop columns; MySQL text indexes need a prefix length (10 chars).
- **Backward compatibility:** never rename public API params/methods — this library is widely deployed. Prefer `# noqa` over a breaking rename (e.g. `create_column`'s `type` shadows the builtin — keep it).
- **Auto-commit:** call `db._auto_commit()` after any write made outside an explicit transaction.
- **Thread safety:** hold `self.db.lock` for schema ops; connections are thread-local; transaction depth lives in `self.local.tx`.
- **Don't mutate input rows:** copy to `dict()` before modifying (see `update_many`, `_queue_add`).
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
Out of scope: FK/relations, Python-side JOINs, async, DB-native UPSERT (implemented via SELECT + INSERT/UPDATE).
