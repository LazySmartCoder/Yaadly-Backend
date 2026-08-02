"""Settings for running tests locally.

DigitalOcean managed Postgres cannot drop its own test database (owner role
limitation), so tests run against an in-memory SQLite database instead.

Usage:  python manage.py test --settings=config.settings_test
"""

from pathlib import Path

from .settings import *  # noqa: F401,F403
from .settings import DATABASES  # noqa: F401

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(__file__).resolve().parent.parent / "test_db.sqlite3",
    }
}
