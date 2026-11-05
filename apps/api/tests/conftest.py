import sqlite3
import sys

import pytest

from sqlalchemy import event
from sqlalchemy.engine import Engine


@pytest.fixture(autouse=True)
def _isolate_user_session_generation(tmp_path_factory, monkeypatch):
    from app.core.config import settings
    from app.core.user_sessions import user_session_generation
    session_root = tmp_path_factory.mktemp("auth-session-state")
    monkeypatch.setattr(settings, "maintenance_control_root", str(session_root))
    user_session_generation(create=True)
    for module in tuple(sys.modules.values()):
        if getattr(module, "__file__", "") and str(module.__file__).endswith("update-control-plane/control_plane.py"):
            monkeypatch.setattr(module, "MAINTENANCE_CONTROL_ROOT", session_root)


@pytest.fixture(autouse=True)
def _isolate_login_throttle():
    # Each test is a separate synthetic client history. Production keeps its
    # real rolling budget; there is no APP_ENV/test bypass in the login route.
    module = sys.modules.get("app.core.login_throttle")
    if module is not None:
        module.login_throttle.clear()
    yield
    module = sys.modules.get("app.core.login_throttle")
    if module is not None:
        module.login_throttle.clear()


@event.listens_for(Engine, "connect")
def _register_sqlite_char_length(dbapi_connection, _connection_record) -> None:
    """Keep PostgreSQL CHECK expressions executable in SQLite unit tests."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function(
            "char_length",
            1,
            lambda value: None if value is None else len(value),
        )
