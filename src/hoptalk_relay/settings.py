from pathlib import Path

from django.utils.csp import CSP

from hoptalk_relay.environment import load_environment_values
from hoptalk_relay.logging_configuration import build_logging_configuration
from hoptalk_relay.relay_settings import load_relay_settings

BASE_DIRECTORY = Path(__file__).resolve().parent.parent

# Lower-case names stay out of django.conf.settings: the values include every secret.
environment_values = load_environment_values(BASE_DIRECTORY / ".env")

RELAY_SETTINGS = load_relay_settings(environment_values)

SECRET_KEY = RELAY_SETTINGS.secret_key
DEBUG = environment_values.get("DJANGO_DEBUG", "false") == "true"

# The panel is reached by the LAN address its certificate is issued for; 127.0.0.1 serves
# the container's own health check.
server_address = environment_values.get("SERVER_ADDRESS", "").strip()
ALLOWED_HOSTS = list(dict.fromkeys(["localhost", "127.0.0.1", *([server_address] if server_address else [])]))

https_port = environment_values.get("HTTPS_PORT", "443").strip()
public_port_suffix = "" if https_port == "443" else f":{https_port}"
CSRF_TRUSTED_ORIGINS = [f"https://{allowed_host}{public_port_suffix}" for allowed_host in ALLOWED_HOSTS]

# No django.contrib.auth, contenttypes or admin: the operator is configured in src/.env, and
# "users" stays the only users table in the database.
INSTALLED_APPS = [
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "heroicons",
    "node",
    "directory",
    "messaging",
    "worker",
    "panel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "panel.middleware.OperatorAuthenticationMiddleware",
]

ROOT_URLCONF = "hoptalk_relay.urls"
WSGI_APPLICATION = "hoptalk_relay.wsgi.application"

template_loaders = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.messages.context_processors.messages",
            ],
            # Only Django's own development server empties the template cache when a file
            # changes. The development build runs gunicorn, so it reads every template afresh
            # instead, and a changed template shows on the next reload.
            "loaders": template_loaders if DEBUG else [("django.template.loaders.cached.Loader", template_loaders)],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": environment_values.get("POSTGRES_HOST", "database"),
        "PORT": environment_values.get("POSTGRES_PORT", "5432"),
        "NAME": environment_values.get("POSTGRES_DB", "hoptalk_relay"),
        "USER": environment_values.get("POSTGRES_USER", "hoptalk_relay"),
        "PASSWORD": environment_values.get("POSTGRES_PASSWORD", ""),
        # Django's built-in psycopg pool needs CONN_MAX_AGE = 0. One connection per gunicorn
        # thread; the worker's ORM work runs on a single sync_to_async thread.
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "pool": {"min_size": 1, "max_size": 4, "timeout": 10},
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
USE_I18N = False
USE_TZ = True
TIME_ZONE = RELAY_SETTINGS.time_zone

STATIC_URL = "/static/"
STATIC_ROOT = Path(environment_values.get("DJANGO_STATIC_ROOT", str(BASE_DIRECTORY / "build" / "collected-static")))
# The scripts are served as written, so they need no build step. The rest of frontend/ stays out:
# main.css is the Tailwind source, and collectstatic cannot resolve its @import "tailwindcss".
STATICFILES_DIRS = [BASE_DIRECTORY / "build" / "static", BASE_DIRECTORY / "frontend" / "scripts"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}

SESSION_ENGINE = "django.contrib.sessions.backends.db"
# Two hours of inactivity sign the operator out; OperatorAuthenticationMiddleware adds an
# absolute limit, because an open tab's HTMX polling keeps renewing the idle one.
SESSION_COOKIE_AGE = 2 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"

# nginx terminates TLS and is the only client that can reach gunicorn's Unix socket.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
}

LOGGING = build_logging_configuration()
