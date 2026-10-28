"""
Payments that complete later (e.g. bank debits): the order is created on hold
when the checkout completes, then paid when the money arrives, or failed
"""
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from salesman.core.utils import get_salesman_model

from salesman_stripe_connect.payment import StripeConnectPayment
from .utils import SESSION_URL, logged, mock_event, mock_session, post_event, session_json

pytestmark = pytest.mark.django_db

Basket = get_salesman_model("Basket")
Order = get_salesman_model("Order")


def event(event_type, basket_or_order, **params):
    kind = "basket" if isinstance(basket_or_order, Basket) else "order"
    return mock_event(
        event_type, mock_session(client_reference_id=f"{kind}_{basket_or_order.id}", **params)
    )


def completed_unpaid(basket_or_order):
    return event("checkout.session.completed", basket_or_order, payment_status="unpaid")


@pytest.fixture
def held_order(client, checkout_basket):
    post_event(client, completed_unpaid(checkout_basket))
    return Order.objects.get()


def test_completed_unpaid(client, checkout_basket):
    # the order, on hold (awaiting payment), with no payment; the basket's gone
    resp = post_event(client, completed_unpaid(checkout_basket))
    assert resp.content.decode() == "Order awaiting payment"
    assert not Basket.objects.filter(id=checkout_basket.id).exists()
    order = Order.objects.get()
    assert order.status == Order.Status.HOLD
    assert order.extra["stripe_session_id"] == "test-session"
    assert not order.payments.exists()
    # (Stripe sends it again)
    resp = post_event(client, completed_unpaid(checkout_basket))
    assert resp.content.decode() == "Order already fulfilled"
    assert Order.objects.count() == 1


def test_completed_unpaid_pending_status(client, checkout_basket, settings):
    settings.SALESMAN_STRIPE_CONNECT_PENDING_STATUS = "CREATED"
    post_event(client, completed_unpaid(checkout_basket))
    assert Order.objects.get().status == Order.Status.CREATED


def test_completed_unpaid_existing_order(client, order):
    resp = post_event(client, completed_unpaid(order))
    assert resp.content.decode() == "Order awaiting payment"
    order.refresh_from_db()
    assert order.status == Order.Status.HOLD
    assert not order.payments.exists()


@pytest.mark.parametrize("settings_status", [None, "COMPLETED"])
def test_async_payment_succeeded(client, held_order, settings, settings_status):
    # the money's arrived: paid, with the paid status
    if settings_status:
        settings.SALESMAN_STRIPE_CONNECT_PAID_STATUS = settings_status
    resp = post_event(client, event("checkout.session.async_payment_succeeded", held_order))
    assert resp.content.decode() == "Order paid"
    held_order.refresh_from_db()
    assert held_order.status == (settings_status or Order.Status.PROCESSING)
    payment = held_order.payments.get()
    assert (payment.amount, payment.transaction_id, payment.payment_method) == (20, "pi_1", "stripe")
    # (again: nothing more)
    resp = post_event(client, event("checkout.session.async_payment_succeeded", held_order))
    assert resp.content.decode() == "Order already fulfilled"
    assert held_order.payments.count() == 1


def test_async_payment_succeeded_paid_status_hook(client, held_order):
    with patch.object(StripeConnectPayment, "paid_status", return_value="COMPLETED") as hook:
        post_event(client, event("checkout.session.async_payment_succeeded", held_order))
    hook.assert_called_once_with(held_order)
    held_order.refresh_from_db()
    assert held_order.status == Order.Status.COMPLETED


def test_async_payment_succeeded_without_completed(client, checkout_basket):
    # (if the completed event hasn't been processed: the order, paid)
    resp = post_event(client, event("checkout.session.async_payment_succeeded", checkout_basket))
    assert resp.content.decode() == "Order fulfilled"
    assert Order.objects.get().status == Order.Status.PROCESSING


def test_async_payment_succeeded_amount_mismatch(client, held_order):
    post_event(client, event("checkout.session.async_payment_succeeded", held_order, amount_total=1500))
    note = held_order.notes.get()
    assert "Payment pi_1 of £15.00 doesn't match the order total of £20.00." in note.message


def test_async_payment_failed(client, held_order, caplog):
    with patch.object(StripeConnectPayment, "order_payment_failed") as hook:
        resp = post_event(client, event("checkout.session.async_payment_failed", held_order))
    assert resp.content.decode() == "Payment failed"
    held_order.refresh_from_db()
    assert held_order.status == Order.Status.FAILED
    assert [note.message for note in held_order.notes.all()] == [
        "Payment pi_1 failed: the order hasn't been paid."
    ]
    hook.assert_called_once_with(held_order)
    assert f"Order {held_order.ref}: payment pi_1 failed" in logged(caplog, logging.WARNING)
    # (again: nothing more)
    with patch.object(StripeConnectPayment, "order_payment_failed") as hook:
        resp = post_event(client, event("checkout.session.async_payment_failed", held_order))
    assert resp.content.decode() == "Event already processed"
    hook.assert_not_called()
    assert held_order.notes.count() == 1


def test_async_payment_failed_status(client, held_order, settings):
    settings.SALESMAN_STRIPE_CONNECT_FAILED_STATUS = "CANCELLED"
    post_event(client, event("checkout.session.async_payment_failed", held_order))
    held_order.refresh_from_db()
    assert held_order.status == Order.Status.CANCELLED


def test_async_payment_failed_after_paid(client, held_order):
    # (out of order, or a paid session): a paid order isn't failed
    post_event(client, event("checkout.session.async_payment_succeeded", held_order))
    resp = post_event(client, event("checkout.session.async_payment_failed", held_order))
    assert resp.content.decode() == "Event already processed"
    held_order.refresh_from_db()
    assert held_order.status == Order.Status.PROCESSING


def test_async_payment_succeeded_after_cancelled(client, held_order):
    # cancelled while waiting: the payment's recorded, but the order stays
    # cancelled, flagged
    held_order.status = Order.Status.CANCELLED
    held_order.save()
    resp = post_event(client, event("checkout.session.async_payment_succeeded", held_order))
    assert resp.content.decode() == "Order paid, but flagged"
    held_order.refresh_from_db()
    assert held_order.status == Order.Status.CANCELLED
    assert held_order.payments.get().amount == 20
    assert held_order.notes.get().message == (
        "Payment pi_1 was received, but the order's status is Cancelled. Check it, "
        "and refund if necessary."
    )


def test_async_payment_failed_after_cancelled(client, held_order):
    held_order.status = Order.Status.CANCELLED
    held_order.save()
    with patch.object(StripeConnectPayment, "order_payment_failed") as hook:
        resp = post_event(client, event("checkout.session.async_payment_failed", held_order))
    assert resp.content.decode() == "Event already processed"
    hook.assert_not_called()


def test_async_payment_failed_no_order(client, checkout_basket, caplog):
    caplog.set_level(logging.INFO)
    resp = post_event(client, event("checkout.session.async_payment_failed", checkout_basket))
    assert resp.content.decode() == "Event ignored"
    assert "Stripe session test-session failed, with no order" in caplog.text


def test_order_payment_failed_hook():
    # (does nothing by default)
    assert StripeConnectPayment.order_payment_failed(None) is None


def test_check_basket_payment_unpaid(rf, paying_basket, requests_mock):
    # back to the basket after completing a checkout that pays later: the
    # order, on hold
    requests_mock.get(
        SESSION_URL,
        json=session_json(client_reference_id=f"basket_{paying_basket.id}", payment_status="unpaid"),
    )
    order = StripeConnectPayment.check_basket_payment(rf.get("/"), paying_basket)
    assert order.status == Order.Status.HOLD


# Orders still awaiting payment after a time (e.g. bank transfers never made)

PAYMENT_INTENT_URL = "https://api.stripe.com/v1/payment_intents/pi_1"


@pytest.fixture
def old_held_order(held_order):
    Order.objects.filter(pk=held_order.pk).update(date_created=timezone.now() - timedelta(days=8))
    held_order.refresh_from_db()
    return held_order


def expire():
    return StripeConnectPayment.expire_pending_orders(timedelta(days=7))


def mock_payment(requests_mock, held_order, payment_intent, session_status="unpaid"):
    requests_mock.get(
        SESSION_URL,
        json=session_json(client_reference_id=f"basket_{held_order.id}", payment_status=session_status),
    )
    requests_mock.get(PAYMENT_INTENT_URL, json={"id": "pi_1", "object": "payment_intent", **payment_intent})
    return requests_mock.post(f"{PAYMENT_INTENT_URL}/cancel", json={"id": "pi_1", "status": "canceled"})


def test_expire_not_yet(held_order, requests_mock):
    # (a day old)
    assert expire() == []


def test_expire_unpaid(old_held_order, requests_mock, account, caplog):
    # nothing received: cancelled in Stripe, and the order failed
    cancel = mock_payment(requests_mock, old_held_order, {"status": "requires_action", "amount_received": 0})
    with patch.object(StripeConnectPayment, "order_payment_failed") as hook:
        assert expire() == [(old_held_order, "Cancelled")]
    assert cancel.last_request.headers["Stripe-Account"] == account
    old_held_order.refresh_from_db()
    assert old_held_order.status == Order.Status.FAILED
    assert old_held_order.notes.get().message == (
        "Payment pi_1 wasn't received within 7 days, so it has been cancelled."
    )
    hook.assert_called_once_with(old_held_order)
    assert f"Order {old_held_order.ref}: payment not received in time, cancelled" in logged(caplog, logging.WARNING)
    # (and not again)
    assert expire() == []


def test_expire_paid(old_held_order, requests_mock):
    # it's arrived (and the webhook was missed): paid
    cancel = mock_payment(requests_mock, old_held_order, {}, session_status="paid")
    assert expire() == [(old_held_order, "Paid")]
    old_held_order.refresh_from_db()
    assert old_held_order.status == Order.Status.PROCESSING
    assert not cancel.called


def test_expire_processing(old_held_order, requests_mock):
    # e.g. a bank debit being collected: left for Stripe
    cancel = mock_payment(requests_mock, old_held_order, {"status": "processing", "amount_received": 0})
    assert expire() == [(old_held_order, "Still processing")]
    old_held_order.refresh_from_db()
    assert old_held_order.status == Order.Status.HOLD
    assert not cancel.called


def test_expire_partly_paid(old_held_order, requests_mock, caplog):
    # part of a bank transfer: flagged once, for someone to check
    cancel = mock_payment(requests_mock, old_held_order, {"status": "requires_action", "amount_received": 500})
    assert expire() == [(old_held_order, "Partly paid")]
    assert expire() == [(old_held_order, "Partly paid")]
    assert old_held_order.notes.get().message == (
        "Only £5.00 of payment pi_1 has been received so far. Check it with the customer."
    )
    assert logged(caplog, logging.ERROR) == [f"Order {old_held_order.ref}: payment pi_1 partly received"]
    old_held_order.refresh_from_db()
    assert old_held_order.status == Order.Status.HOLD
    assert not cancel.called


def test_expire_changed_meanwhile(old_held_order, requests_mock):
    # e.g. cancelled by the shop while checking with Stripe
    def cancelled_meanwhile(request, context):
        Order.objects.filter(pk=old_held_order.pk).update(status=Order.Status.CANCELLED)
        return {"id": "pi_1", "object": "payment_intent", "status": "requires_action", "amount_received": 0}

    cancel = mock_payment(requests_mock, old_held_order, {})
    requests_mock.get(PAYMENT_INTENT_URL, json=cancelled_meanwhile)
    assert expire() == [(old_held_order, "Changed meanwhile")]
    assert not cancel.called


def test_expire_stripe_error(old_held_order, requests_mock, caplog):
    requests_mock.get(SESSION_URL, status_code=400, json={"error": {"message": "Error"}})
    assert expire() == [(old_held_order, "Error")]
    assert f"Error checking order {old_held_order.ref}'s pending payment" in caplog.text
    old_held_order.refresh_from_db()
    assert old_held_order.status == Order.Status.HOLD
