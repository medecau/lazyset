# lazyset: databases for lazy people

[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)

**lazyset** makes reading and writing a SQL database as simple as working with a
JSON file. It is a thin layer over [SQLAlchemy](https://www.sqlalchemy.org/) with
no ORM models — connect, grab a table, and insert / find plain dicts. Tables and
columns are created on demand. Works with SQLite (the default), PostgreSQL and
MySQL.

> **Hard fork of [pudo/dataset](https://github.com/pudo/dataset).** It ships as
> **`lazyset`** on PyPI and imports as **`lazyset`** (`import lazyset`). The
> redesigned API — five self-describing write verbs and one honest `auto_create`
> flag — breaks from upstream and is not a drop-in upgrade; see `CHANGELOG.md`
> under 0.1.0 for the full list of breaking changes.

## Install

```bash
pip install lazyset
```

PostgreSQL additionally needs `psycopg2` and MySQL needs `PyMySQL`; SQLite ships
with Python.

## Usage

```python
import lazyset

db = lazyset.connect('sqlite:///:memory:')
table = db['user']

# five self-describing write verbs — each takes one row OR an iterable of rows
table.insert(dict(name='John Doe', age=46, country='China'))
table.insert_ignore(dict(id=1, name='John Doe'), ['id'])
table.upsert(dict(id=1, name='John Q. Doe'), ['id'])
table.update(dict(name='John Doe', age=47), ['name'])
table.delete(country='China')

# read it back
john = table.find_one(name='John Doe')
for row in table.find(age={'>=': 21}, _order_by='name'):
    print(row['name'])
```

## Documentation

Full API reference and guides: <https://OWNER.github.io/REPO/> — generated from
docstrings with [pdoc](https://pdoc.dev/) and published to GitHub Pages by CI.
Build them locally with `make docs` (output in `site/`).

## License

MIT — see `LICENSE`.
