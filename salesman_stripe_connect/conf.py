from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class AppSettings:
    """
    Settings, read from Django settings when they're used (so tests can
    override them), apart from SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL, which is
    read when the payment method class is defined (salesman's label is a class
    attribute).
    """

    @property
    def SALESMAN_STRIPE_CONNECT_SECRET_KEY(self):
        """The platform's Stripe secret key"""
        return self._required("SALESMAN_STRIPE_CONNECT_SECRET_KEY")

    @property
    def SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET(self):
        """Signing secret of the webhook endpoint for connected accounts' events"""
        return self._required("SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET")

    @property
    def SALESMAN_STRIPE_CONNECT_CLIENT_ID(self):
        """The platform's Connect client id (ca_...), for connecting the account"""
        return self._required("SALESMAN_STRIPE_CONNECT_CLIENT_ID")

    @property
    def SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT(self):
        """Set to False to remove the page for disconnecting the account"""
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT", True)

    @property
    def SALESMAN_STRIPE_CONNECT_PAGES_ENABLED(self):
        """
        Set to False to disable the connect pages (e.g. once the account is
        connected)
        """
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_PAGES_ENABLED", True)

    @property
    def SALESMAN_STRIPE_CONNECT_CURRENCY(self):
        """
        ISO currency code for payments. Only currencies with 2 decimal places
        (e.g. gbp, eur, usd) are supported.
        """
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_CURRENCY", "gbp").lower()

    @property
    def SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL(self):
        """Payment method label shown at checkout"""
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL", "Pay with Stripe")

    @property
    def SALESMAN_STRIPE_CONNECT_PAID_STATUS(self):
        """Status of orders created when a checkout session is paid"""
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_PAID_STATUS", "PROCESSING")

    @property
    def SALESMAN_STRIPE_CONNECT_PENDING_STATUS(self):
        """
        Status of orders whose payment completes later (e.g. a bank debit): the
        order is created when the checkout completes, and paid (given the paid
        status) when the money arrives
        """
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_PENDING_STATUS", "HOLD")

    @property
    def SALESMAN_STRIPE_CONNECT_FAILED_STATUS(self):
        """Status of orders whose payment failed after the checkout completed"""
        return getattr(settings, "SALESMAN_STRIPE_CONNECT_FAILED_STATUS", "FAILED")

    def _required(self, name):
        value = getattr(settings, name, None)
        if not value:
            raise ImproperlyConfigured(f"The {name} setting is required")
        return value


app_settings = AppSettings()
