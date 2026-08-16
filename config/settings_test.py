"""Settings for running tests locally.

Tests run against a local SQLite database so they do not touch any real
database.

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

# The production .env sets DEBUG=False which enables SECURE_SSL_REDIRECT and
# bounces plain-HTTP test-client requests with 301s. Force those off so the
# DRF API tests run over the test client's HTTP transport.
DEBUG = True
SECURE_SSL_REDIRECT = False
