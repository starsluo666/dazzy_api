import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-local-only-change-me")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "accounts",
    "providers",
    "orders",
    "activities",
    "mediafiles",
    "locations",
    "engagements",
    "home",
    "health",
    "backoffice",
    "taskcenter",
    "supportcases",
    "notifications",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {
    "default": {
        "ENGINE": os.getenv("DJANGO_DB_ENGINE", "django.contrib.gis.db.backends.postgis"),
        "HOST": os.environ["POSTGRES_HOST"],
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "NAME": os.environ.get("POSTGRES_DATABASE") or os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "CONN_MAX_AGE": int(os.getenv("POSTGRES_CONN_MAX_AGE", "60")),
        "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "prefer")},
    }
}

redis_username = os.getenv("REDIS_USERNAME", "")
redis_password = os.getenv("REDIS_PASSWORD", "")
redis_auth = ""
if redis_username or redis_password:
    redis_auth = f"{quote(redis_username, safe='')}:{quote(redis_password, safe='')}@"
redis_scheme = (
    "rediss" if os.getenv("REDIS_SSL", "false").lower() in {"1", "true", "yes"} else "redis"
)
redis_location = (
    f"{redis_scheme}://{redis_auth}{os.environ['REDIS_HOST']}:"
    f"{os.getenv('REDIS_PORT', '6379')}/{os.getenv('REDIS_CACHE_DB', '0')}"
)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": redis_location,
        "TIMEOUT": 300,
        "OPTIONS": {"socket_connect_timeout": 5, "socket_timeout": 5},
        "KEY_PREFIX": "dazzy",
    }
}

redis_celery_db = os.getenv("REDIS_CELERY_DB", "1")
CELERY_BROKER_URL = (
    f"{redis_scheme}://{redis_auth}{os.environ['REDIS_HOST']}:"
    f"{os.getenv('REDIS_PORT', '6379')}/{redis_celery_db}"
)
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ("json",)
CELERY_TIMEZONE = "Asia/Shanghai"
CELERY_BEAT_SCHEDULE = {
    "process-due-scheduled-tasks": {
        "task": "taskcenter.process_due_scheduled_tasks",
        "schedule": 10.0,
    },
    "synchronize-scheduled-tasks": {
        "task": "taskcenter.synchronize_scheduled_tasks",
        "schedule": 180.0,
    },
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

COS_REGION = os.environ["COS_REGION"]
COS_BUCKET = os.environ["COS_BUCKET"]
COS_BASE_URL = os.environ["COS_BASE_URL"].rstrip("/")
TENCENT_CLOUD_SECRET_ID = os.environ["TENCENT_CLOUD_SECRET_ID"]
TENCENT_CLOUD_SECRET_KEY = os.environ["TENCENT_CLOUD_SECRET_KEY"]
COS_PUBLIC_PREFIX = os.getenv("COS_PUBLIC_PREFIX", "dazzy-test/public/")
COS_PRIVATE_PREFIX = os.getenv("COS_PRIVATE_PREFIX", "dazzy-test/private/")
COS_SIGNED_PUBLIC_URL_TTL = int(os.getenv("COS_SIGNED_PUBLIC_URL_TTL", "3600"))
COS_SIGNED_PRIVATE_URL_TTL = int(os.getenv("COS_SIGNED_PRIVATE_URL_TTL", "300"))
PROVIDER_ORDER_AUTO_CONFIRM_DAYS = int(os.getenv("PROVIDER_ORDER_AUTO_CONFIRM_DAYS", "3"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "config.authentication.VersionedJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "config.authentication.DevelopmentUserAuthentication",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "NUM_PROXIES": int(os.getenv("DRF_NUM_PROXIES", "0")),
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}
ADMIN_REFRESH_COOKIE_NAME = os.getenv("ADMIN_REFRESH_COOKIE_NAME", "dazzy_admin_refresh")
ADMIN_REFRESH_COOKIE_PATH = "/api/v1/admin/auth/"
ADMIN_REFRESH_COOKIE_DOMAIN = os.getenv("ADMIN_REFRESH_COOKIE_DOMAIN", "")
ADMIN_REFRESH_COOKIE_SECURE = False
ADMIN_REFRESH_COOKIE_SAMESITE = "Strict"
ADMIN_REFRESH_COOKIE_MAX_AGE = int(SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds())
SMS_CODE_TTL_SECONDS = int(os.getenv("SMS_CODE_TTL_SECONDS", "300"))
SMS_CODE_RESEND_SECONDS = int(os.getenv("SMS_CODE_RESEND_SECONDS", "60"))
SMS_CODE_MAX_ATTEMPTS = int(os.getenv("SMS_CODE_MAX_ATTEMPTS", "5"))
SMS_PHONE_DAILY_LIMIT = int(os.getenv("SMS_PHONE_DAILY_LIMIT", "10"))
SMS_DEVELOPMENT_CODE = os.getenv("SMS_DEVELOPMENT_CODE", "123456")
AUTH_FAILURE_LIMIT = int(os.getenv("AUTH_FAILURE_LIMIT", "5"))
AUTH_IP_FAILURE_LIMIT = int(os.getenv("AUTH_IP_FAILURE_LIMIT", "30"))
AUTH_LOCK_SECONDS = int(os.getenv("AUTH_LOCK_SECONDS", "900"))
PAYMENT_REFUND_PROCESSING_TIMEOUT_SECONDS = int(
    os.getenv("PAYMENT_REFUND_PROCESSING_TIMEOUT_SECONDS", "300")
)
HUIFU_PAYMENT_ENABLED = os.getenv("HUIFU_PAYMENT_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
}
HUIFU_ENV = os.getenv("HUIFU_ENV", "mertest")
HUIFU_SYS_ID = os.getenv("HUIFU_SYS_ID", "")
HUIFU_PRODUCT_ID = os.getenv("HUIFU_PRODUCT_ID", "")
HUIFU_MERCHANT_ID = os.getenv("HUIFU_MERCHANT_ID", "")
HUIFU_RSA_PRIVATE_KEY = os.getenv("HUIFU_RSA_PRIVATE_KEY", "")
HUIFU_RSA_PUBLIC_KEY = os.getenv("HUIFU_RSA_PUBLIC_KEY", "")
HUIFU_SKILL_SOURCE = os.getenv("HUIFU_SKILL_SOURCE", "hfps/1.3.5")
HUIFU_NOTIFY_URL = os.getenv("HUIFU_NOTIFY_URL", "")
HUIFU_FEE_FLAG = os.getenv("HUIFU_FEE_FLAG", "1")
HUIFU_CONNECT_TIMEOUT_SECONDS = int(os.getenv("HUIFU_CONNECT_TIMEOUT_SECONDS", "15"))
WECHAT_OFFICIAL_ACCOUNT_APP_ID = os.getenv("WECHAT_OFFICIAL_ACCOUNT_APP_ID", "")
WECHAT_OFFICIAL_ACCOUNT_APP_SECRET = os.getenv("WECHAT_OFFICIAL_ACCOUNT_APP_SECRET", "")
WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL = os.getenv(
    "WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL", ""
)
WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL = os.getenv(
    "WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL", ""
)
WECHAT_MOBILE_APP_ID = os.getenv("WECHAT_MOBILE_APP_ID", "")
WECHAT_OAUTH_STATE_MAX_AGE_SECONDS = int(
    os.getenv("WECHAT_OAUTH_STATE_MAX_AGE_SECONDS", "600")
)
WECHAT_OAUTH_TIMEOUT_SECONDS = int(os.getenv("WECHAT_OAUTH_TIMEOUT_SECONDS", "10"))
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "auth_sms_send": os.getenv("AUTH_SMS_SEND_RATE", "10/min"),
    "auth_sms_send_ip_daily": os.getenv("AUTH_SMS_SEND_IP_DAILY_RATE", "50/day"),
    "auth_register": os.getenv("AUTH_REGISTER_RATE", "5/min"),
    "auth_login": os.getenv("AUTH_LOGIN_RATE", "30/min"),
    "admin_auth_refresh": os.getenv("ADMIN_AUTH_REFRESH_RATE", "60/min"),
    "auth_password_reset": os.getenv("AUTH_PASSWORD_RESET_RATE", "10/min"),
    "auth_security": os.getenv("AUTH_SECURITY_RATE", "10/min"),
    "provider_order_payment_create": os.getenv(
        "PROVIDER_ORDER_PAYMENT_CREATE_RATE", "10/min"
    ),
    "provider_order_payment_status": os.getenv(
        "PROVIDER_ORDER_PAYMENT_STATUS_RATE", "30/min"
    ),
    "map_proxy_burst": os.getenv("MAP_PROXY_BURST_RATE", "30/min"),
    "map_proxy_daily": os.getenv("MAP_PROXY_DAILY_RATE", "500/day"),
}
DAZZY_DEMO_USER_PUBLIC_ID = os.getenv("DAZZY_DEMO_USER_PUBLIC_ID", "")
TENCENT_MAP_WEB_SERVICE_KEY = os.environ["TENCENT_MAP_WEB_SERVICE_KEY"]
TENCENT_MAP_WEB_SERVICE_SK = os.environ["TENCENT_MAP_WEB_SERVICE_SK"]
TENCENT_MAP_DEFAULT_REGION = os.getenv("TENCENT_MAP_DEFAULT_REGION", "邯郸市")
TENCENT_MAP_TIMEOUT_SECONDS = float(os.getenv("TENCENT_MAP_TIMEOUT_SECONDS", "5"))
SPECTACULAR_SETTINGS = {
    "TITLE": "DAZZY API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["config.permissions.DebugOnlyPermission"],
}
if os.name == 'nt':  # Windows
    GDAL_LIBRARY_PATH = r'C:\Users\13106\AppData\Local\Programs\OSGeo4W\bin\gdal313.dll'
    GEOS_LIBRARY_PATH = r'C:\Users\13106\AppData\Local\Programs\OSGeo4W\bin\geos_c.dll'
