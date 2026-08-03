# Advanced filters

lazyset provides two ways to run queries: `Table.find` and `Database.query`. The
table find helper provides limited but simple filtering:

```python
results = table.find(column={operator: value})
# e.g.:
results = table.find(name={'like': '%mole rat%'})
```

A special form is keyword searches on specific columns:

```python
results = table.find(value=5)
# equal to:
results = table.find(value={'=': 5})

# Lists, tuples and sets are turned into IN queries:
results = table.find(category=('foo', 'bar'))
# equal to:
results = table.find(value={'in': ('foo', 'bar')})
```

The following comparison operators are supported:

| Operator | Description |
| --- | --- |
| `gt`, `>` | Greater than |
| `lt`, `<` | Less than |
| `gte`, `>=` | Greater or equal |
| `lte`, `<=` | Less or equal |
| `!=`, `<>`, `not` | Not equal to a single value |
| `in` | Value is in the given sequence |
| `notin` | Value is not in the given sequence |
| `like`, `ilike` | Text search; ILIKE is case-insensitive. Use `%` as a wildcard |
| `notlike` | Like text search, but the pattern must not match |
| `between`, `..` | Value is between the two values in the given tuple |
| `startswith` | String starts with |
| `endswith` | String ends with |

An unrecognized operator name raises `QueryError` rather than silently matching
nothing.

Filtering on a column that does not exist on the table raises
`NoSuchColumnError` (in 2.x this silently matched no rows).

`_order_by`, `_limit`, `_offset`, `_step` and `_streamed` are reserved keyword
arguments on `Table.find` — all leading-underscore, so they never collide with a
column filter. A column whose name is not a valid keyword argument, or that
would clash with a reserved modifier, can still be filtered through the `where=`
mapping (or a positional SQLAlchemy core expression, see below):

```python
# filter a column literally named "_limit"
results = table.find(where={'_limit': 5})

# order by a real column named "created" (modifiers take column names)
results = table.find(status='open', _order_by='created')
```

You can also pass
[SQLAlchemy core expressions](https://docs.sqlalchemy.org/en/latest/tutorial/data_select.html#tutorial-selecting-data)
directly into `Table.find` as positional arguments. Access the underlying
SQLAlchemy table via `table.table` and its columns via `table.table.columns`:

```python
from sqlalchemy import or_

# Get a column object:
city = table.table.columns.city
# Use a SQLAlchemy clause:
results = table.find(city.ilike('amsterda%'))

# Combine with OR:
country = table.table.columns.country
results = table.find(or_(city == 'Amsterdam', country == 'Germany'))

# Combine SQLAlchemy clauses with keyword filters:
results = table.find(city.ilike('new%'), country='US')
```

These clauses also work with `Table.count`, `Table.find_one`, and
`Table.delete`.

## Queries using raw SQL

To run more complex queries with JOINs, or GROUP BY-style aggregation, use
`Database.query` to run raw SQL. It also supports parameterisation to avoid SQL
injection:

```python
statement = 'SELECT user, COUNT(*) c FROM photos GROUP BY user'
for row in db.query(statement):
    print(row['user'], row['c'])

# With parameter binding:
results = db.query('SELECT * FROM users WHERE age > :min_age', min_age=21)

# For bind names that aren't valid keyword arguments (reserved words, or names
# colliding with params / _step), pass a params mapping:
results = db.query('SELECT * FROM users WHERE country = :from', {'from': 'US'})
```

For fully programmatic, composable query building, consider using
[SQLAlchemy core expressions](https://docs.sqlalchemy.org/en/latest/tutorial/data_select.html#tutorial-selecting-data)
directly.
