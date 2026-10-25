"""Settings for the tests: a minimal project with salesman and the app"""

SECRET_KEY = "tests"
DEBUG = False
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    "salesman.core",
    "salesman.basket",
    "salesman.checkout",
    "salesman.orders",
    "salesman_stripe_connect",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ROOT_URLCONF = "tests.urls"
LOGIN_URL = "/login/"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

# Salesman, with the app's mixins on the basket and order models
SALESMAN_BASKET_MODEL = "testapp.Basket"
SALESMAN_BASKET_ITEM_MODEL = "testapp.BasketItem"
SALESMAN_ORDER_MODEL = "testapp.Order"
SALESMAN_ORDER_ITEM_MODEL = "testapp.OrderItem"
SALESMAN_ORDER_PAYMENT_MODEL = "testapp.OrderPayment"
SALESMAN_ORDER_NOTE_MODEL = "testapp.OrderNote"
SALESMAN_PRODUCT_TYPES = {"testapp.Product": "tests.testapp.serializers.ProductSerializer"}
SALESMAN_PAYMENT_METHODS = ["salesman_stripe_connect.payment.StripeConnectPayment"]

SALESMAN_STRIPE_CONNECT_SECRET_KEY = "sk_test_dummy"
SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET = "whsec_dummy"
SALESMAN_STRIPE_CONNECT_CLIENT_ID = "ca_dummy"
