# Changelog

## 0.1.0 (unreleased)

- Payments that complete later (e.g. bank debits): the order is created on hold
  when the checkout completes, and paid or failed by the
  `checkout.session.async_payment_succeeded` and `..._failed` webhooks (new
  settings `SALESMAN_STRIPE_CONNECT_PENDING_STATUS` and `..._FAILED_STATUS`,
  and the `order_payment_failed` hook). Before, every completed checkout was
  recorded as paid.
- `expire_pending_orders(older_than)`: fails orders still awaiting payment
  (e.g. bank transfers never made, which Stripe doesn't fail), after checking
  each payment with Stripe.
- Checkout sessions have a Stripe customer, found by email or created (the
  `get_customer` hook), so Checkout can offer bank transfers.

First version, moved from the project it was written for: Stripe Checkout
payments for django-salesman as direct charges on a Stripe Connect account,
with the webhook (completed and expired checkouts, refunds, disputes, the
account disconnecting), the connect pages, refunds, and signals.
