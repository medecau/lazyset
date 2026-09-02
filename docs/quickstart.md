# Quickstart

Hi, welcome to the twelve-minute quick-start tutorial.

## Connecting to a database

First you need to import the lazyset package:

```python
import lazyset
```

To connect to a database you identify it by its
[URL](https://docs.sqlalchemy.org/en/latest/core/engines.html#engine-creation-api),
a string of the form `"dialect://user:password@host/dbname"`. Here are a few
examples for different database backends:

<!-- example: skip -->

```python
# connecting to a SQLite database
db = lazyset.connect("sqlite:///mydatabase.db")

# connecting to a MySQL database with user and password
db = lazyset.connect("mysql://user:password@localhost/mydatabase")

# connecting to a PostgreSQL database
db = lazyset.connect("postgresql://scott:tiger@localhost:5432/mydatabase")
```

It is also possible to define the URL as an environment variable called
`DATABASE_URL`, so you can connect without explicitly passing a URL:

```python
db = lazyset.connect()
```

Depending on which database you're using, you may also have to install the
bindings for it. SQLite is included in the Python core; PostgreSQL and MySQL
each need a DBAPI driver, which the matching extra installs:

```bash
pip install "lazyset[postgresql] @ git+https://github.com/medecau/lazyset@v0.1.0"
pip install "lazyset[mysql] @ git+https://github.com/medecau/lazyset@v0.1.0"
```

## Storing data

To store some data you need a reference to a table. You don't need to worry
about whether the table already exists, since lazyset creates tables and columns
on first write. (This can be turned off with `lazyset.connect(auto_create=False)`,
or per table via `db.table(name, must_exist=True)`, in which case writing to a
missing table or column raises instead of creating it.)

```python
# get a reference to the table 'user'
table = db["user"]
```

Storing data is then a single function call: pass a `dict` to `Table.insert`.
Note that you don't need to create the columns _name_ and _age_ — lazyset does
that automatically:

```python
# Insert a new record.
table.insert(dict(name="John Doe", age=46, country="China"))

# lazyset creates "missing" columns any time you insert a dict with an unknown key
table.insert(dict(name="Jane Doe", age=37, country="France", gender="female"))
```

Updating existing entries is easy, too:

```python
table.update(dict(name="John Doe", age=47), ["name"])
```

The list of filter columns given as the second argument selects which rows to
update, using the values in the same dict. If you don't want to filter on a
particular value, just use the auto-generated `id` column.

Since the same `dict` supplies both the filter columns and the new values, a
filter column's own value can never be changed by `Table.update` — it is only
ever used to find the row, not to set it.

## Using transactions

You can group a set of updates in a transaction: they are all committed at once
or, on exception, all reverted. Transactions are exposed as a context manager,
so they work through a `with` statement:

```python
with lazyset.connect() as tx:
    tx["user"].insert(dict(name="John Doe", age=46, country="China"))
```

You get the same functionality by invoking `Database.begin`, `Database.commit`
and `Database.rollback` explicitly:

<!-- example: isolated -->

```python
db = lazyset.connect()
db.begin()
try:
    db["user"].insert(dict(name="John Doe", age=46, country="China"))
    db.commit()
except Exception:
    db.rollback()
```

Nested transactions are supported too:

<!-- example: isolated -->

```python
db = lazyset.connect()
with db as tx1:
    tx1["user"].insert(dict(name="John Doe", age=46, country="China"))
    with db as tx2:
        tx2["user"].insert(dict(name="Jane Doe", age=37, country="France"))
```

## Closing connections

When you're done with a database, call `Database.close` to release all
connections back to the pool and dispose of the engine:

<!-- example: isolated -->

```python
db = lazyset.connect("sqlite:///mydb.db")
# ... do work ...
db.close()
```

This matters most in multi-threaded applications or with connection-pooled
databases (PostgreSQL, MySQL), where open connections can otherwise accumulate
and exhaust the pool.

## Inspecting databases and tables

When dealing with unknown databases we might want to check their structure
first. Let's find out which tables are stored in the database:

```pycon
>>> print(db.tables)
['user']
```

Now, list all columns in the table `user`:

```pycon
>>> print(db["user"].columns)
['id', 'name', 'age', 'country', 'gender']
```

Using `len()` we get the total number of rows in a table:

```pycon
>>> print(len(db["user"]))
2
```

## Reading data from tables

Now let's get some real data out. Calling `Table.find` with no filter returns
every row:

```python
users = db["user"].find()
```

To iterate over all rows, iterate the table directly:

```python
for user in db["user"]:
    print(user["age"])
```

We can search for specific entries using `Table.find` and `Table.find_one`:

```python
# All users from China
chinese_users = table.find(country="China")

# Get a specific user
john = table.find_one(name="John Doe")

# Find multiple at once
winners = table.find(id=[1, 3, 7])

# Find by comparison operator
elderly_users = table.find(age={">=": 70})
possible_customers = table.find(age={"between": [21, 80]})

# Use the underlying SQLAlchemy directly
elderly_users = table.find(table.table.columns.age >= 70)
```

See the **Advanced filters** guide below for details on complex filters.

Using `Table.distinct` we can grab a set of rows with unique values in one or
more columns:

```python
# Get one user per country
db["user"].distinct("country")
```

Finally, use the `row_type` parameter to choose the container rows are returned
in. It defaults to `dict`; pass any callable that accepts the row's columns as
keyword arguments and returns a mapping:

<!-- example: isolated -->

```python
from collections import OrderedDict

db = lazyset.connect("sqlite:///mydatabase.db", row_type=OrderedDict)
```

For example, a small `dict` subclass that also exposes columns as attributes
(`row.name` as well as `row['name']`) works as a `row_type`:

<!-- example: isolated -->

```python
class AttrDict(dict):
    __getattr__ = dict.__getitem__


db = lazyset.connect("sqlite:///mydatabase.db", row_type=AttrDict)
```

## Running custom SQL queries

Of course the main reason you're using a database is the full power of SQL.
Here's how you run raw queries with lazyset:

```python
result = db.query("SELECT country, COUNT(*) c FROM user GROUP BY country")
for row in result:
    print(row["country"], row["c"])
```

`Database.query` can also run
[SQLAlchemy core expressions](https://docs.sqlalchemy.org/en/latest/orm/queryguide/query.html)
for programmatic construction of more complex queries:

```python
table = db["user"].table
statement = table.select().where(table.c.name.like("%John%"))
result = db.query(statement)
```

## Limitations of lazyset

The goal of lazyset is to make basic database operations simpler by expressing
them in a Pythonic way. The downside is that as your application grows more
complex, you may need access to more advanced operations and be forced to switch
to using SQLAlchemy proper without the lazyset layer (or its ORM).

When that moment comes, take the hit. SQLAlchemy is an amazing piece of Python
code, and it gives you idiomatic access to all of SQL's functions.

Some specific aspects of SQL that are not exposed in lazyset, and are considered
out of scope for the project, include:

- Foreign key relationships between tables, and expressing one-to-many and
  many-to-many relationships in idiomatic Python.
- Python-wrapped `JOIN` queries.
- Creating databases, or managing DBMS software.

`Table.upsert` uses database-native conflict handling (`ON CONFLICT` / `ON
DUPLICATE KEY`) against a unique arbiter index. There is no separate
`insert_ignore`: an upsert whose rows carry only the key columns compiles to
the same `DO NOTHING`.

There's also functionality that might be nice to support in the future but that
requires significant engineering, such as async operations.
