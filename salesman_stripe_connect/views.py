from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from salesman.checkout.payment import payment_methods_pool

from .payment import StripeConnectPayment


def get_payment_method():
    """The StripeConnectPayment (or subclass) in SALESMAN_PAYMENT_METHODS"""
    for payment in payment_methods_pool.get_payments():
        if isinstance(payment, StripeConnectPayment):
            return payment
    raise ImproperlyConfigured(
        "No StripeConnectPayment payment method in SALESMAN_PAYMENT_METHODS"
    )


@csrf_exempt
@require_POST
def webhook_view(request):
    return get_payment_method().webhook(request)
