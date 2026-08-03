import os
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "apps"))

# Prefer the deployment .env next to the code (BASE_DIR/.env); fall back to a
# fixed production path (/root/yaadly/.env) for server layouts where the
# secrets live outside the app directory.
_ENV_CANDIDATES = [Path("/root/yaadly/.env"), BASE_DIR / ".env"]
ENV_PATH = next((p for p in _ENV_CANDIDATES if p.exists()), None)

if ENV_PATH is not None:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)


def env(key, default=None, cast=str):
    value = os.environ.get(key, default)
    if value is None:
        return None
    if cast is bool:
        return str(value).lower() in {"1", "true", "yes", "on"}
    if cast is list:
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return cast(value)


SECRET_KEY = env("SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DEBUG", True, bool)
ALLOWED_HOSTS = env("ALLOWED_HOSTS", "*", list)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "apps.accounts",
    "apps.journals",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.LanAwareSSLRedirectMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "yaadly"),
        "USER": env("POSTGRES_USER", "yaadly"),
        "PASSWORD": env("POSTGRES_PASSWORD", "yaadly"),
        "HOST": env("POSTGRES_HOST", "db"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "OPTIONS": {"sslmode": env("POSTGRES_SSLMODE", "prefer")},
    }
}

if env("USE_SQLITE", False, bool):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env("ACCESS_TOKEN_MINUTES", 60, int)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env("REFRESH_TOKEN_DAYS", 14, int)),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

GOOGLE_CLIENT_ID = env(
    "GOOGLE_CLIENT_ID",
    "203246620684-ehhhpgjtd4lbo7537nruu53a7vb271q6.apps.googleusercontent.com",
)

CORS_ALLOWED_ORIGINS = env(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8080,http://localhost:5000",
    list,
)
CORS_ALLOW_CREDENTIALS = True

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    # Local dev against a plain-HTTP Django backend (e.g. a real Android device
    # hitting runserver over the LAN) must opt out of the HTTPS redirect; the
    # production default stays on.
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", True, bool)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.utils.autoreload": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
