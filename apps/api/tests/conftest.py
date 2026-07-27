import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _register_sqlite_char_length(dbapi_connection, _connection_record) -> None:
    """Keep PostgreSQL CHECK expressions executable in SQLite unit tests."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function(
            "char_length",
            1,
            lambda value: None if value is None else len(value),
        )
