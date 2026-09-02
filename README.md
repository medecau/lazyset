# lazyset: databases for lazy people

[![CI](https://github.com/medecau/lazyset/actions/workflows/ci.yml/badge.svg)](https://github.com/medecau/lazyset/actions/workflows/ci.yml)

**lazyset** makes reading and writing a SQL database as simple as working with a
JSON file. It is a thin layer over [SQLAlchemy](https://www.sqlalchemy.org/) with
no ORM models — connect, grab a table, and insert / find plain dicts. Tables and
columns are created on demand. Works with SQLite (the default), PostgreSQL and
MySQL.

> **Hard fork of [pudo/dataset](https://github.com/pudo/dataset).** It imports as
> **`lazyset`** (`import lazyset`) and is **not on PyPI** — the name is too close
> to an existing project there, so it is installed from git. The redesigned API —
> four self-describing write verbs and one honest `auto_create` flag — breaks
> from upstream and is not a drop-in upgrade; see `CHANGELOG.md` under 0.1.0 for
> the full list of breaking changes.

## Install

Straight from git — there is no PyPI package to `pip install lazyset`:

```bash
pip install git+https://github.com/medecau/lazyset
```

Pin a release by appending its tag, which is what you want in anything you have
to reproduce later — the bare URL tracks `main` and moves under you:

```bash
pip install git+https://github.com/medecau/lazyset@v0.1.0
uv add "lazyset @ git+https://github.com/medecau/lazyset@v0.1.0"
```

SQLite needs nothing extra — its driver ships with Python. The other two
backends need a DBAPI driver, which the `postgresql` and `mysql` extras pull in:

```bash
pip install "lazyset[postgresql] @ git+https://github.com/medecau/lazyset@v0.1.0"
uv add "lazyset[mysql] @ git+https://github.com/medecau/lazyset@v0.1.0"
```

## Usage

```python
import lazyset

db = lazyset.connect("sqlite:///:memory:")
table = db["user"]

# four self-describing write verbs — each takes one row OR an iterable of rows
table.insert(dict(name="John Doe", age=46, country="China"))
table.upsert(dict(name="Jane Doe", age=37, country="France"), ["name"])
table.update(dict(name="John Doe", age=47), ["name"])
table.delete(country="France")

# read it back
john = table.find_one(name="John Doe")
for row in table.find(age={">=": 21}, _order_by="name"):
    print(row["name"])
```

## Documentation

Full API reference and guides: <https://medecau.github.io/lazyset/> — generated from
docstrings with [pdoc](https://pdoc.dev/) and published to GitHub Pages by CI.
Build them locally with `make docs` (output in `site/`).

## License

MIT — see `LICENSE`.
