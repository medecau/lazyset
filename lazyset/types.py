"""Column type mapping.

`Types.guess` picks an SQLAlchemy column type from a sample value, and the
class attributes (``db.types.text``, ``db.types.integer``, …) name the types
used for ``primary_type`` and `Table.create_column`.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Unicode,
    UnicodeText,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeEngine, _Binary

MYSQL_LENGTH_TYPES = (String, _Binary)
ColumnType = TypeEngine[Any] | type[TypeEngine[Any]]


class Types:
    """A holder class for easy access to SQLAlchemy type names."""

    integer = Integer
    string = Unicode
    text = UnicodeText
    binary = LargeBinary
    float = Float
    bigint = BigInteger
    boolean = Boolean
    date = Date
    datetime = DateTime

    def __init__(self, is_postgres: bool | None = None):
        self.json = JSONB if is_postgres else JSON

    def guess(self, sample: Any) -> ColumnType:
        """Given a single sample, guess the column type for the field.

        If the sample is an instance of an SQLAlchemy type, the type will be
        used instead.
        """
        if isinstance(sample, TypeEngine):
            return sample
        if isinstance(sample, type) and issubclass(sample, TypeEngine):
            return sample()
        if isinstance(sample, bool):
            return self.boolean
        elif isinstance(sample, int):
            return self.bigint
        elif isinstance(sample, float):
            return self.float
        elif isinstance(sample, datetime):
            return self.datetime
        elif isinstance(sample, date):
            return self.date
        elif isinstance(sample, dict):
            return self.json
        elif isinstance(sample, bytes):
            # Bytes are binary, not text: a TEXT/VARCHAR column round-trips
            # them only under SQLite's loose typing — PostgreSQL and MySQL
            # reject non-UTF-8 bytes. A binary column is portable.
            return self.binary
        return self.text
