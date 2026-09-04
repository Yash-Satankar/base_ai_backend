"""
Shared fixtures for the real-MySQL validation tests
(execution validator + schema refiner).

A live MySQL 8 is resolved from, in order:
  1. ``MYSQL_EXEC_VALIDATION_DSN`` in the environment, else
  2. an ephemeral ``testcontainers`` MySQL 8 (needs a running Docker).

If neither is available the test is skipped — unless CI has set
``REQUIRE_MYSQL_EXEC_TESTS=1``, in which case a missing backend is a hard
failure (a gate that silently skips is not a gate).
"""

import os

import pytest


def _mysql_unavailable(reason: str):
    if os.environ.get("REQUIRE_MYSQL_EXEC_TESTS") == "1":
        pytest.fail(f"REQUIRE_MYSQL_EXEC_TESTS=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def _mysql_dsn():
    dsn = os.environ.get("MYSQL_EXEC_VALIDATION_DSN")
    if dsn:
        yield dsn
        return

    try:
        try:
            from testcontainers.community.mysql import MySqlContainer  # type: ignore
        except ImportError:
            from testcontainers.mysql import MySqlContainer  # type: ignore
    except ImportError:
        _mysql_unavailable("no MYSQL_EXEC_VALIDATION_DSN and testcontainers not installed")

    try:
        container = MySqlContainer("mysql:8.0")
        container.start()
    except Exception as e:  # Docker not running, image pull blocked, …
        _mysql_unavailable(f"could not start an ephemeral MySQL 8 (Docker not running?): {e}")

    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def mysql_enabled(_mysql_dsn, monkeypatch):
    """Point the MySQL execution validator at the test server for one test."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", True)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER", False)
    monkeypatch.setenv("MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    return _mysql_dsn
