# Examples

Examples of setting up and customising the app in a project. They assume the
project's shop app is called `shop`, with salesman's models in `shop/models.py`
(see the README's Setup).

- [A payment method subclass](#a-payment-method-subclass)
- [Cards only](#cards-only)
- [Stopping the basket being changed during a payment](#stopping-the-basket-being-changed-during-a-payment)
- [Checkout](#checkout)
- [Emails from the signals](#emails-from-the-signals)
- [Refunding an order](#refunding-an-order)
- [Templates](#templates)
- [Settings](#settings)

## A payment method subclass

```python
# shop/payment.py
from datetime import timedelta

from salesman.checkout.payment import PaymentError
from salesman_stripe_connect.payment import StripeConnectPayment


class PayByStripe(StripeConnectPayment):
    success_template_name = "shop/payment_success.html"
    cancel_template_name = "shop/payment_cancelled.html"

    def basket_payment(self, basket, request):
        # Checks before the payment starts; PaymentError's message is shown to
        # the customer
        if not basket.count:
            raise PaymentError("Your basket is empty.")
        return super().basket_payment(basket, request)

    def get_session_expiry(self, obj):
        # Expire unpaid checkouts after 30 minutes (Stripe's minimum), rather
        # than 24 hours, e.g. if items are held for the customer meanwhile
        return timedelta(minutes=30)

    @classmethod
    def basket_payment_expired(cls, basket):
        # The customer didn't pay: e.g. empty the basket, releasing any stock
        # held for it. (The basket is saved after this.)
        basket.clear()

    @classmethod
    def get_view_context(cls, request):
        # Extra context for the success and cancel templates
        return {"hide_basket_link": True}
```

```python
# settings.py
SALESMAN_PAYMENT_METHODS = ["shop.payment.PayByStripe"]
```

The success and cancel views and the webhook use the payment method in
`SALESMAN_PAYMENT_METHODS`, so they use the subclass's hooks. In your own views,
call the subclass's methods (e.g. `PayByStripe.check_basket_payment(...)`).

## Cards only

By default, Stripe Checkout offers the payment methods turned on in the
connected account's Stripe dashboard. Methods that take days to complete (e.g.
bank debits) aren't supported, as their orders would never be created (see the
README), so to be safe, offer only cards. Apple Pay and Google Pay are card
payments in Checkout, so they're still offered on devices that support them.

```python
class PayByStripe(StripeConnectPayment):
    def get_session_data(self, obj, request):
        data = super().get_session_data(obj, request)
        data["payment_method_types"] = ["card"]
        return data
```

`get_session_data` returns the parameters for
[creating the Checkout session](https://docs.stripe.com/api/checkout/sessions/create),
so the same approach works for other options, e.g. metadata:

```python
        data["metadata"] = {"site": "shop"}
```

(Don't change `client_reference_id`, `success_url` or `cancel_url`: the app uses
them to find the basket or order, and to return the customer to the site.)

## Stopping the basket being changed during a payment

While a payment is in progress (`basket.payment_in_progress`), the basket's items
mustn't change, or the order could be created with different items from the
ones paid for. The app records the payment on the basket, but your views need
to enforce the lock.

A decorator for the views that change the basket:

```python
# shop/views.py
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .payment import PayByStripe


def basket_not_locked(view):
    """Refuse to change the basket while a payment is in progress"""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        basket = PayByStripe.get_request_basket(request)
        if basket is not None and basket.payment_in_progress:
            messages.error(
                request,
                "Your basket can't be changed while your payment is in progress.",
            )
            return redirect("shop:basket")
        return view(request, *args, **kwargs)

    return wrapper


@basket_not_locked
@require_POST
def add_to_basket(request, product_id):
    ...


@basket_not_locked
@require_POST
def remove_from_basket(request, item_id):
    ...
```

The basket page: if the customer comes back during a payment, check whether
they've paid (the webhook may not have arrived yet) or the payment has expired.
If it's still in progress, offer to return to Stripe's page, or to cancel the
payment:

```python
import stripe
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST


@require_GET
def basket_view(request):
    basket = PayByStripe.get_request_basket(request)
    if basket is not None and basket.payment_in_progress:
        order = PayByStripe.check_basket_payment(request, basket)
        if order is not None:
            # Paid: the order has been created, and the basket deleted
            return redirect("shop:order", order.token)
        basket.refresh_from_db()
        if basket.payment_in_progress:
            return render(
                request,
                "shop/payment_in_progress.html",
                # A link back to Stripe's page, and a form posting to cancel_payment
                {"stripe_session_url": basket.stripe_session_url},
            )
    return render(request, "shop/basket.html", {"basket": basket})


@require_POST
def cancel_payment(request):
    basket = PayByStripe.get_request_basket(request)
    if basket is not None and basket.payment_in_progress:
        try:
            if PayByStripe.cancel_basket_payment(basket):
                messages.info(request, "Your payment has been cancelled.")
            # Otherwise it has already been paid: the basket view creates the order
        except stripe.StripeError:
            messages.error(request, "Sorry, your payment couldn't be cancelled.")
    return redirect("shop:basket")
```

Also: don't include salesman's basket and checkout API URLs (only
`payment_methods_pool.get_urls()`), as they'd bypass these checks.

## Checkout

A checkout view using salesman's checkout serializer, which saves the
customer's details on the basket (`basket.extra`, which is copied to the
order) and starts the payment:

```python
from django.http import HttpResponseRedirect
from salesman.checkout.payment import PaymentError
from salesman.checkout.serializers import CheckoutSerializer


@basket_not_locked
@require_POST
def checkout(request):
    basket = PayByStripe.get_request_basket(request)
    if basket is None or not basket.count:
        return redirect("shop:basket")
    basket.update(request)
    serializer = CheckoutSerializer(
        data={
            "email": request.POST["email"],
            "shipping_address": request.POST["address"],
            "billing_address": "",
            "payment_method": PayByStripe.identifier,  # "stripe"
            "extra": {"name": request.POST["name"]},
        },
        context={"request": request, "basket": basket},
    )
    if not serializer.is_valid():
        return render(request, "shop/checkout.html", {"errors": serializer.errors})
    try:
        serializer.save()
    except PaymentError as error:
        messages.error(request, str(error))
        return redirect("shop:basket")
    # Stripe's payment page
    return HttpResponseRedirect(serializer.data["url"])
```

The customer's email (`basket.extra["email"]`, or the user's email) is passed
to Stripe, so they don't have to enter it again.

## Emails from the signals

The signals are sent inside the database transaction that records the change,
so send emails once it's committed:

```python
# shop/receivers.py
from django.core.mail import send_mail
from django.db import transaction
from django.dispatch import receiver
from salesman_stripe_connect import signals

SHOP_EMAIL = "shop@example.com"


@receiver(signals.refund_recorded)
def email_refund(sender, order, refund_id, amount, fully_refunded, **kwargs):
    transaction.on_commit(
        lambda: send_mail(
            f"Refund for order {order.ref}",
            f"We've refunded £{amount:.2f} to your card.",
            SHOP_EMAIL,
            [order.email],
        )
    )


@receiver(signals.dispute_created)
def email_dispute(sender, order, dispute, message, **kwargs):
    # The connected account has to respond to the dispute in its Stripe dashboard
    transaction.on_commit(
        lambda: send_mail(f"Order {order.ref} disputed", message, SHOP_EMAIL, [SHOP_EMAIL])
    )


@receiver(signals.account_deauthorized)
def email_account_disconnected(sender, account_id, **kwargs):
    transaction.on_commit(
        lambda: send_mail(
            "Stripe account disconnected",
            f"The Stripe account {account_id} has been disconnected, so card "
            "payments can't be taken until an account is connected again.",
            SHOP_EMAIL,
            [SHOP_EMAIL],
        )
    )
```

Connect the receivers when the app is ready:

```python
# shop/apps.py
from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        from . import receivers  # noqa: F401
```

## Refunding an order

E.g. a staff view that fully refunds an order. The refund is recorded on the
order straight away (and the `refund_recorded` signal sent); the
`charge.refunded` webhook finds it already recorded.

```python
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404
from salesman.core.utils import get_salesman_model

Order = get_salesman_model("Order")


@staff_member_required
@require_POST
def refund_order(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    refunded, errors = PayByStripe.refund_order(order)
    if errors:
        messages.error(request, "Refund failed: " + "; ".join(errors))
    else:
        messages.success(request, f"Refunded £{refunded:.2f}.")
    return redirect("shop_admin:order", order.pk)
```

To refund part of an order, refund it in the connected account's Stripe
dashboard: the webhook records it.

## Templates

The success and cancel pages, in the site's layout:

```html
<!-- shop/templates/shop/payment_success.html -->
{% extends "base.html" %}

{% block content %}
<h1>Thank you</h1>
{% if order %}
  <p>Your order reference is <strong>{{ order.ref }}</strong>.
     We've sent a confirmation to {{ email }}.</p>
{% else %}
  {# The payment is complete, but the order hasn't been created yet (the webhook will create it) #}
  <p>Thank you for your payment. Your order is being processed.</p>
{% endif %}
{% endblock %}
```

```html
<!-- shop/templates/shop/payment_cancelled.html -->
{% extends "base.html" %}

{% block content %}
<h1>Payment cancelled</h1>
<p>Your payment was cancelled. <a href="{% url 'shop:basket' %}">Return to your basket</a>.</p>
{% endblock %}
```

The connect pages extend `salesman_stripe_connect/base.html`, filling its
`title` and `content` blocks, and show results with Django's messages. To put
them in the site's layout, add a template with that name to your project's
templates that extends your base template. If your base template has `title`
and `content` blocks and shows messages, that's all it needs:

```html
<!-- templates/salesman_stripe_connect/base.html -->
{% extends "base.html" %}
```

(Your templates directory must come before the app's templates: Django's
`DIRS` are searched before apps' templates, and apps in `INSTALLED_APPS`
order.)

## Settings

```python
# settings.py
import os

SALESMAN_PAYMENT_METHODS = ["shop.payment.PayByStripe"]

SALESMAN_STRIPE_CONNECT_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
SALESMAN_STRIPE_CONNECT_CLIENT_ID = os.environ["STRIPE_CONNECT_CLIENT_ID"]
# Turn off the connect pages once the account is connected
SALESMAN_STRIPE_CONNECT_PAGES_ENABLED = os.environ.get("STRIPE_CONNECT_PAGES", "") == "on"

SALESMAN_STRIPE_CONNECT_CURRENCY = "gbp"
SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL = "Pay by card"
SALESMAN_STRIPE_CONNECT_PAID_STATUS = "PROCESSING"
```
