# django-salesman-stripe-connect

[Stripe Checkout](https://docs.stripe.com/payments/checkout) payments for
[django-salesman](https://github.com/django-salesman/django-salesman), as
**direct charges on a Stripe Connect account**.

Your site's Stripe account is the *platform*; the business taking the payments
connects its own Stripe account to it (a *connected account*). The checkout
sessions, payments, refunds and disputes all belong to the connected account,
so the business sees them in its own Stripe dashboard, and can refund payments
there as well as from the site. The app doesn't add a platform fee (Stripe
calls it an application fee); see Platform fees to add one.

This suits a site built for someone else (e.g. a charity's shop), where the
payments should go straight to their Stripe account, without them sharing
their Stripe keys.

## What it does

- A salesman payment method, `StripeConnectPayment`, that sends the customer to
  Stripe Checkout, and creates the order when they've paid.
- A webhook for Stripe's events: completed and expired checkouts, payments
  that complete later (e.g. bank debits) succeeding or failing, refunds
  (full or partial, from the site or the Stripe dashboard), failed refunds,
  disputes, and the connected account disconnecting.
- Pages for connecting (and disconnecting) the Stripe account, with Stripe
  Connect OAuth, so the business can connect it themselves.
- Locking the basket while a payment is in progress, so the order matches what
  was paid for (the project's views enforce the lock; see Setup).
- Recording refunds as negative payments, and order amounts that take them into
  account (`OrderAmountsMixin`).
- Signals for refunds, disputes and the account disconnecting, e.g. to send
  emails.

## How a payment works

1. At checkout, salesman calls the payment method's `basket_payment`, which
   creates a Checkout session on the connected account, records it on the
   basket (locking it), and returns Stripe's payment page URL.
2. The customer pays on Stripe's page, and is sent back to the success view,
   which creates and pays the order (from the basket, which is then deleted).
   The `checkout.session.completed` webhook does the same, in case the
   customer doesn't come back; whichever comes first creates the order.
3. If the customer cancels on Stripe's page, the cancel view expires the
   session and unlocks the basket. If the session expires (after 24 hours, or
   `get_session_expiry`), the `checkout.session.expired` webhook unlocks it.
4. For a payment method that completes later (e.g. a bank debit), the order is
   created when the checkout completes, awaiting payment, and paid (or failed)
   when Stripe says so: see "Payment methods, and payments that complete later".

Problems that need someone to look at them (a payment that doesn't match the
order total, a payment for an empty basket, a failed refund, a dispute) are
added to the order as non-public notes and logged as errors.

## Requirements

- Python 3.12+, Django 5.2+, django-salesman 1.3+.
  (django-salesman 1.3.0 on PyPI requires Django < 6. For Django 6, install
  django-salesman from a fork that allows it, e.g.
  `git+https://github.com/rebkwok/django-salesman.git@12a6cdb0d36bbd544590c151aa2e0422fb72489e`.)
- The `stripe` package, version 16 (the code uses Stripe API version
  `2026-09-30.endive`).
- A Stripe account with [Connect](https://docs.stripe.com/connect) enabled
  (the platform), and the business's Stripe account to connect to it.
- A database that supports `select_for_update` (e.g. PostgreSQL) in production:
  the basket or order is locked while a payment is recorded, as the webhook and
  the success view can arrive at the same time.

## Installation

Until it's on PyPI, install it from GitHub at a fixed commit, e.g. with pip:

```
pip install "django-salesman-stripe-connect @ git+https://github.com/rebkwok/django-salesman-stripe-connect.git@<commit>"
```

or with uv, in `pyproject.toml`:

```toml
[project]
dependencies = ["django-salesman-stripe-connect"]

[tool.uv.sources]
django-salesman-stripe-connect = { git = "https://github.com/rebkwok/django-salesman-stripe-connect.git", rev = "<commit>" }
```

## Setup

1. Add the app to `INSTALLED_APPS` (after salesman's apps), and run
   `python manage.py migrate`:
   ```python
   INSTALLED_APPS = [
       # ...
       "salesman.core",
       "salesman.basket",
       "salesman.checkout",
       "salesman.orders",
       "salesman_stripe_connect",
   ]
   ```
2. Use salesman's swappable models, with the app's mixins on the basket and
   order:
   ```python
   from salesman.basket.models import BaseBasket
   from salesman.orders.models import BaseOrder
   from salesman_stripe_connect.mixins import OrderAmountsMixin, StripeBasketMixin

   class Basket(StripeBasketMixin, BaseBasket):
       ...

   class Order(OrderAmountsMixin, BaseOrder):
       ...
   ```
   (and `SALESMAN_BASKET_MODEL`, `SALESMAN_ORDER_MODEL` etc. in settings; see
   salesman's docs).
3. Add the payment method (or your subclass of it, see Customising):
   ```python
   SALESMAN_PAYMENT_METHODS = ["salesman_stripe_connect.payment.StripeConnectPayment"]
   ```
4. Add the URLs:
   ```python
   from django.urls import include, path
   from salesman.checkout.payment import payment_methods_pool

   urlpatterns = [
       # The success and cancel views, at /api/payment/stripe/success/ and
       # /api/payment/stripe/cancel/
       path("api/", include(payment_methods_pool.get_urls())),
       # The webhook (/stripe/webhook/) and the connect pages (/stripe/connect/,
       # /stripe/oauth/callback/, ...)
       path("stripe/", include("salesman_stripe_connect.urls")),
   ]
   ```
   `payment_methods_pool.get_urls()` only includes the payment methods' URLs.
   If you include all of salesman's API URLs instead, its basket and checkout
   API will be public, and it doesn't know about the basket lock (next step).
5. **Stop the basket being changed during a payment.** The app doesn't do this
   itself (it only refuses to start a second payment for a basket), as it
   doesn't provide the basket views. While `basket.payment_in_progress` is
   True, your views should refuse to add, update or remove items, or to start
   a checkout; otherwise the order could be created with different items from
   the ones paid for. When the customer comes back to their basket during a
   payment, call `StripeConnectPayment.check_basket_payment(request, basket)`,
   which creates the order if the session has been paid, or unlocks the basket
   if it has expired; and offer a way to cancel the payment
   (`StripeConnectPayment.cancel_basket_payment(basket)`) so they can change
   the basket. See [the examples](docs/examples.md#stopping-the-basket-being-changed-during-a-payment).
6. Settings (see Settings below):
   ```python
   SALESMAN_STRIPE_CONNECT_SECRET_KEY = "sk_..."
   SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET = "whsec_..."
   SALESMAN_STRIPE_CONNECT_CLIENT_ID = "ca_..."
   ```
7. Set up Stripe: the webhook, and OAuth for connecting the account (below).
8. Override the templates (see Templates), and connect the account at
   `/stripe/connect/`.

## Stripe setup

In the **platform's** Stripe dashboard (in test mode first, then again in live
mode):

- **Webhook.** Add an endpoint that listens to events on **Connected
  accounts** (not "Your account"), at `https://<your domain>/stripe/webhook/`,
  with API version `2026-09-30.endive`, for these events:
  - `checkout.session.completed`
  - `checkout.session.async_payment_succeeded`
  - `checkout.session.async_payment_failed`
  - `checkout.session.expired`
  - `charge.refunded`
  - `charge.refund.updated`
  - `charge.dispute.created`
  - `charge.dispute.closed`
  - `account.application.authorized`
  - `account.application.deauthorized`

  Its signing secret is `SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET`. Events from
  any account other than the connected one are ignored (so a platform can be
  shared between sites).

  Locally, forward events with the Stripe CLI (it prints a signing secret to
  use):
  ```
  stripe listen --forward-connect-to localhost:8000/stripe/webhook/
  ```
- **OAuth.** In the Connect settings, enable OAuth (for Standard accounts) and
  add the redirect URI `https://<your domain>/stripe/oauth/callback/`. The
  Connect client id (`ca_...`) is `SALESMAN_STRIPE_CONNECT_CLIENT_ID`.

The connected account needs the `card_payments` capability (Standard accounts
normally have it).

## Connecting the account

Go to `/stripe/connect/` and click "Connect Stripe account". On Stripe's page,
log in to the account to connect (or, in test mode, create a test account).
The account is saved (`ConnectedAccount`; there can only be one), and payments
are made to it. Until an account is connected, payments are refused with a
message for the customer.

The connect pages are for logged-in users with the
`salesman_stripe_connect.connect_stripe_account` permission ("Can connect or
disconnect the Stripe account"; superusers have it). Others are sent to
`LOGIN_URL`, or refused.

The account can be disconnected at `/stripe/connect/disconnect/` (e.g. to
connect a different one). If it's disconnected from the platform in Stripe,
the webhook removes it, and sends the `account_deauthorized` signal.

Once the account is connected, you can turn the connect pages off with
`SALESMAN_STRIPE_CONNECT_PAGES_ENABLED = False`.

## Settings

| Setting | Default | |
|---|---|---|
| `SALESMAN_STRIPE_CONNECT_SECRET_KEY` | required | The platform's secret key (`sk_test_...` or `sk_live_...`) |
| `SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET` | required | The webhook endpoint's signing secret (`whsec_...`) |
| `SALESMAN_STRIPE_CONNECT_CLIENT_ID` | required | The platform's Connect client id (`ca_...`), for connecting the account |
| `SALESMAN_STRIPE_CONNECT_PAGES_ENABLED` | `True` | `False` turns off the connect pages (they return 404) |
| `SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT` | `True` | `False` removes the disconnect page |
| `SALESMAN_STRIPE_CONNECT_CURRENCY` | `"gbp"` | The currency of payments; only currencies with 2 decimal places (e.g. gbp, eur, usd) |
| `SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL` | `"Pay with Stripe"` | The payment method's label (salesman's `label`; read when the app is loaded) |
| `SALESMAN_STRIPE_CONNECT_PAID_STATUS` | `"PROCESSING"` | The status of orders created when they're paid (one of your order statuses) |
| `SALESMAN_STRIPE_CONNECT_PENDING_STATUS` | `"HOLD"` | The status of orders whose payment completes later (e.g. a bank debit), until it does |
| `SALESMAN_STRIPE_CONNECT_FAILED_STATUS` | `"FAILED"` | The status of orders whose payment failed after the checkout completed |

Your order statuses (`SALESMAN_ORDER_STATUS`) also need a `REFUNDED` status
(salesman's default statuses have it), which fully refunded orders are set to.

The required settings are checked when they're used, so they need a value (a
dummy one will do) in tests and development too.

## Payment methods, and payments that complete later

The checkout session doesn't set `payment_method_types` (Stripe's API no longer
accepts it for Checkout), so Checkout offers the payment methods turned on in
the **connected account's** Stripe dashboard (Settings → Payment methods). To
offer only some (e.g. cards, Apple Pay and Google Pay), turn the others off
there.

Some methods (e.g. bank debits, bank transfers) take days to complete. Their
sessions complete as unpaid: the order is created then, with
`SALESMAN_STRIPE_CONNECT_PENDING_STATUS` (`HOLD`) and no payment, and the basket
is deleted (so the order holds its items). When the money arrives,
`checkout.session.async_payment_succeeded` pays the order and gives it
`SALESMAN_STRIPE_CONNECT_PAID_STATUS`; if the payment fails,
`checkout.session.async_payment_failed` gives it
`SALESMAN_STRIPE_CONNECT_FAILED_STATUS` (`FAILED`), notes it, and calls the
`order_payment_failed` hook (e.g. to put its items back in stock, and tell the
customer). Don't send an order with the pending status until it's paid. The
success template has `payment_pending` for these.

Stripe never fails a **bank transfer** the customer doesn't make: its order
would wait forever. Call `StripeConnectPayment.expire_pending_orders(older_than)`
regularly (e.g. a management command run by cron) to fail orders still awaiting
payment after `older_than` (a `timedelta`, e.g. 7 days). It checks each payment
with Stripe first: paid orders are paid, payments still being collected (bank
debits) are left, and part-paid ones are flagged; the rest are cancelled in
Stripe and failed (`order_payment_failed`). Bank transfers also can't be
changed once the customer has their bank details (Stripe shows them, and they
can't come back to pay another way), so you may prefer to leave them turned off.

Checkout sessions are given a Stripe **customer** (found by the customer's
email, or created, on the connected account): Checkout only offers bank
transfers to customers, and the customer's payments are grouped in the Stripe
dashboard. If Stripe can't find or create the customer, the session has just
the email. Override `get_customer(email)` to change this (return `None` for no
customer).

## Platform fees

The platform can take a fee from each payment: an
[application fee](https://docs.stripe.com/connect/direct-charges#collect-fees),
which Stripe moves from the payment on the connected account to the platform.
Set it (in pence or cents) in `get_session_data`:

```python
from decimal import Decimal

from salesman_stripe_connect.payment import StripeConnectPayment, to_minor_units


class PayByStripe(StripeConnectPayment):
    def get_session_data(self, obj, request):
        data = super().get_session_data(obj, request)
        # e.g. 2% of the total
        fee = to_minor_units(obj.total * Decimal("0.02"))
        data.setdefault("payment_intent_data", {})["application_fee_amount"] = fee
        return data
```

The customer still pays the order total, and the connected account still pays
Stripe's processing fees.

By default, refunds don't return the application fee: the platform keeps it,
and the connected account pays the whole refund. To return it when refunding
from the site (`refund_order`, `refund_payment`), add `refund_application_fee`
in `get_refund_data` (a full refund returns the whole fee):

```python
    @classmethod
    def get_refund_data(cls, payment):
        return {**super().get_refund_data(payment), "refund_application_fee": True}
```

Refunds made in the connected account's Stripe dashboard don't return the fee.
The platform can return it with Stripe's
[application fee refunds](https://docs.stripe.com/api/fee_refunds), e.g. from a
`refund_recorded` signal receiver; the app doesn't do this itself.

## Refunds

`StripeConnectPayment.refund_order(order)` fully refunds an order's Stripe
payments, returning `(amount refunded, [error messages])`, e.g. from an admin
view (see [the examples](docs/examples.md#refunding-an-order)). `refund_payment`
(salesman's refund interface) refunds a single payment.

Refunds made in the connected account's Stripe dashboard (full or partial) are
recorded by the `charge.refunded` webhook. Each refund is recorded once, as an
order payment with a negative amount, with a note on the order; a full refund
sets the order's status to `REFUNDED`. If a refund fails later, it's removed
and the order is flagged.

With `OrderAmountsMixin`, refunds don't make an order unpaid: `is_paid` and
`amount_outstanding` use the amount received, and `amount_refunded` and
`refund_status` ("Fully refunded", "Partially refunded" or "") describe the
refunds.

## Customising

Subclass `StripeConnectPayment`, put your subclass in
`SALESMAN_PAYMENT_METHODS`, and override its hooks:

| Hook | |
|---|---|
| `get_session_data(obj, request)` | The data for creating the Checkout session (e.g. payment methods, metadata) |
| `get_refund_data(payment)` (classmethod) | The data for refunding an order payment from the site (e.g. `refund_application_fee`, metadata) |
| `get_customer(email)` | The connected account's Stripe customer id for the email (found or created), or `None` for just the email |
| `get_session_expiry(obj)` | How long until the session expires: a `timedelta` (30 minutes to 24 hours), or `None` for Stripe's default (24 hours) |
| `session_created(obj, session, request)` | The session has been created; records it on the basket (locking it) |
| `basket_payment_expired(basket)` (classmethod) | The basket's session has expired; called before the basket is unlocked and saved (e.g. to release stock) |
| `basket_payment_cancelled(basket)` (classmethod) | The basket's payment has been cancelled, and the basket unlocked |
| `paid_status(order)` (classmethod) | The status of an order on hold once its payment arrives (default `SALESMAN_STRIPE_CONNECT_PAID_STATUS`; e.g. completed, if there's nothing to send) |
| `order_payment_failed(order)` (classmethod) | A payment that was to complete later failed, and the order has the failed status (e.g. to put its items back in stock, and tell the customer) |
| `get_request_basket(request)` (classmethod) | The request's basket for the success and cancel views, or `None` (doesn't create one) |
| `get_view_context(request)` (classmethod) | Extra context for the success and cancel templates |
| `success_template_name`, `cancel_template_name` | The success and cancel templates |

You can also override salesman's `basket_payment(basket, request)` and
`order_payment(order, request)`, e.g. to check the basket before the payment
starts (raise `salesman.checkout.payment.PaymentError` with a message for the
customer to refuse it).

Signals (`salesman_stripe_connect.signals`), e.g. for emails:

| Signal | Arguments |
|---|---|
| `refund_recorded` | `order`, `refund_id`, `amount` (Decimal), `fully_refunded` (bool) |
| `dispute_created` | `order`, `dispute` (Stripe's dispute), `message` (as noted on the order) |
| `dispute_closed` | `order`, `dispute`, `message` |
| `account_deauthorized` | `account_id` |

They're sent inside the database transaction that records the change, so
receivers that send emails should use `transaction.on_commit`.

See [docs/examples.md](docs/examples.md) for examples of all of these.

## Templates

| Template | |
|---|---|
| `salesman_stripe_connect/success.html` | After paying. Context: `order` (if it has been created), `email`, `total`, and `get_view_context`'s |
| `salesman_stripe_connect/cancel.html` | After cancelling on Stripe's page. Context: `get_view_context`'s |
| `salesman_stripe_connect/base.html` | The connect pages' base template: they fill its `title` and `content` blocks, and show results with Django's messages framework |
| `salesman_stripe_connect/connect.html`, `disconnect.html` | The connect pages (they use Bootstrap's class names) |

The success and cancel templates are minimal, standalone fragments: override
them (or set `success_template_name` and `cancel_template_name`) to use your
site's layout. Override `base.html` to put the connect pages in your site's
layout.

## Logging

The app logs to `salesman_stripe_connect.payment` and
`salesman_stripe_connect.connect_views`. Problems with orders and payments
(e.g. a dispute, or a payment that doesn't match the order) are logged as
errors; events about payments that weren't made through the site are logged
as information.

## Tests

The tests run with a minimal project (`tests/settings.py`, and salesman's
models with the app's mixins in `tests/testapp`), with Stripe's API faked:

```
uv run pytest
```

They must keep 100% coverage. CI runs them with Python 3.12 and Django 5.2,
and Python 3.13 and Django 6.

## Licence

BSD-3-Clause (see LICENSE).
