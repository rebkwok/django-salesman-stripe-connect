# django-salesman-stripe-connect

Stripe Checkout payments for [django-salesman](https://github.com/django-salesman/django-salesman),
as direct charges on a Stripe Connect account: the checkout sessions, payments,
refunds and disputes belong to the connected account, so refunds can be made
from the connected account's Stripe dashboard as well as from the site.

## Setup

1. Add `"salesman_stripe_connect"` to `INSTALLED_APPS`.
2. Add the mixins to the salesman models:
   ```python
   from salesman_stripe_connect.mixins import OrderAmountsMixin, StripeBasketMixin

   class Basket(StripeBasketMixin, BaseBasket): ...
   class Order(OrderAmountsMixin, BaseOrder): ...
   ```
3. Stop the basket being changed during a payment. The app doesn't do this
   itself (it only refuses to start a second payment for a basket):
   - While `basket.payment_in_progress` is True, the project's views should
     refuse to add, update or remove items, and to start a checkout.
   - Don't expose salesman's basket and checkout API (only include
     `payment_methods_pool.get_urls()`), as it would bypass those checks.
   - When the customer returns to their basket during a payment, call
     `StripeConnectPayment.check_basket_payment(request, basket)`: it fulfils
     the order if the session has been paid, or unlocks the basket if it has
     expired. Offer a way to cancel the payment
     (`StripeConnectPayment.cancel_basket_payment(basket)`) so they can change
     the basket.
4. Add the payment method, or a subclass of it, to `SALESMAN_PAYMENT_METHODS`:
   `"salesman_stripe_connect.payment.StripeConnectPayment"`.
5. Include salesman's payment URLs (for the success and cancel views), and the
   webhook:
   ```python
   path("api/", include(payment_methods_pool.get_urls())),
   # /stripe/webhook/, and the connect pages /stripe/connect/ and /stripe/oauth/callback/
   path("stripe/", include("salesman_stripe_connect.urls")),
   ```
6. Settings:
   - `SALESMAN_STRIPE_CONNECT_SECRET_KEY`: the platform's secret key
   - `SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET`: the webhook endpoint's signing secret
   - `SALESMAN_STRIPE_CONNECT_CLIENT_ID`: the platform's Connect client id (`ca_...`)
   - `SALESMAN_STRIPE_CONNECT_PAGES_ENABLED` (default True): set to False to
     disable the connect pages, e.g. once the account is connected
   - `SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT` (default True): set to False to
     remove the page for disconnecting the account
   - `SALESMAN_STRIPE_CONNECT_CURRENCY` (default `gbp`; currencies with 2 decimal places)
   - `SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL` (default "Pay with Stripe")
   - `SALESMAN_STRIPE_CONNECT_PAID_STATUS`: status of paid orders (default `PROCESSING`)
7. In the platform's Stripe dashboard, add a webhook endpoint for events on
   **connected accounts**, using API version `2026-09-30.endive`, for these
   events: `checkout.session.completed`, `checkout.session.expired`,
   `charge.refunded`, `charge.refund.updated`, `charge.dispute.created`,
   `charge.dispute.closed`, `account.application.authorized`,
   `account.application.deauthorized`. Locally, use
   `stripe listen --forward-connect-to localhost:8000/stripe/webhook/`.
8. Connect the account that payments go to. In the platform's Connect settings,
   enable OAuth and add the redirect URI `https://<domain>/stripe/oauth/callback/`.
   Then go to `/stripe/connect/` and connect the account on Stripe's page.
   Only one account can be connected (`ConnectedAccount`); until it is, Stripe
   payments are refused. It can be disconnected at `/stripe/connect/disconnect/`
   (so another can be connected), and if it disconnects from the platform in
   Stripe, the webhook removes it.

   The connect pages are for logged-in users with the
   `salesman_stripe_connect.connect_stripe_account` permission ("Can connect or
   disconnect the Stripe account"; superusers have it). Others are sent to
   `LOGIN_URL` or refused. Override `salesman_stripe_connect/base.html` to use
   the site's layout (the pages fill its `title` and `content` blocks).

## Customising

Subclass `StripeConnectPayment` and override its hooks:
`session_created`, `get_session_data`, `get_session_expiry`,
`basket_payment_expired`, `basket_payment_cancelled`, `get_request_basket`,
`get_view_context`, and `success_template_name` / `cancel_template_name`.

Signals (`salesman_stripe_connect.signals`): `refund_recorded`,
`dispute_created`, `dispute_closed`, `account_deauthorized`, e.g. for emails.

Refunds are recorded as order payments with negative amounts.
`StripeConnectPayment.refund_order(order)` fully refunds an order; partial
refunds made in the Stripe dashboard are recorded by the webhook.

## Tests

The tests run with a minimal project (`tests/settings.py`, and salesman's
models with the app's mixins in `tests/testapp`), with Stripe's API faked:

```
uv run pytest
```

They must keep 100% coverage. CI runs them with Python 3.12 and Django 5.2,
and Python 3.13 and Django 6.
