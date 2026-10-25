"""Refunds: from the orders admin (refund_order), and recorded by the webhook"""
import logging
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest
from salesman.core.utils import get_salesman_model

from salesman_stripe_connect import signals
from salesman_stripe_connect.payment import StripeConnectPayment
from .utils import REFUNDS_URL, logged, mock_event, post_event

pytestmark = pytest.mark.django_db

Order = get_salesman_model("Order")


def refund_json(refund_id="re_1", amount=2000, status="succeeded"):
    return {
        "id": refund_id,
        "object": "refund",
        "amount": amount,
        "status": status,
        "payment_intent": "pi_1",
    }


def refunds_list(*refunds):
    return {"object": "list", "data": list(refunds), "has_more": False, "url": "/v1/refunds"}


@pytest.fixture
def refund_signals():
    received = []

    def receiver(sender, **kwargs):
        received.append(kwargs)

    signals.refund_recorded.connect(receiver)
    yield received
    signals.refund_recorded.disconnect(receiver)


# Refunding


def test_refund_order(paid_order, requests_mock, account, refund_signals):
    refund = requests_mock.post(REFUNDS_URL, json=refund_json())
    assert StripeConnectPayment.refund_order(paid_order) == (Decimal("20.00"), [])

    # refunded on the connected account (direct charges don't need reverse_transfer)
    assert refund.last_request.headers["Stripe-Account"] == account
    assert parse_qs(refund.last_request.text) == {
        "payment_intent": ["pi_1"],
        "metadata[order]": [paid_order.ref],
    }
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.REFUNDED
    assert paid_order.amount_refunded == Decimal("20.00")
    assert paid_order.amount_paid == 0
    refund_payment = paid_order.payments.get(transaction_id="re_1")
    assert refund_payment.amount == Decimal("-20.00")
    assert refund_payment.payment_method == "stripe"
    assert [note.message for note in paid_order.notes.all()] == ["Refund re_1 of £20.00"]
    assert refund_signals == [
        {
            "signal": signals.refund_recorded,
            "order": paid_order,
            "refund_id": "re_1",
            "amount": Decimal("20.00"),
            "fully_refunded": True,
        }
    ]


def test_refund_data_hook(paid_order, requests_mock):
    # e.g. a platform that takes a fee refunds it too
    class FeePayment(StripeConnectPayment):
        @classmethod
        def get_refund_data(cls, payment):
            return {**super().get_refund_data(payment), "refund_application_fee": True}

    refund = requests_mock.post(REFUNDS_URL, json=refund_json())
    assert FeePayment.refund_order(paid_order) == (Decimal("20.00"), [])
    assert parse_qs(refund.last_request.text) == {
        "payment_intent": ["pi_1"],
        "metadata[order]": [paid_order.ref],
        "refund_application_fee": ["true"],
    }


def test_refund_order_error(paid_order, requests_mock, caplog):
    requests_mock.post(
        REFUNDS_URL,
        status_code=400,
        json={"error": {"message": "Charge pi_1 has already been refunded."}},
    )
    assert StripeConnectPayment.refund_order(paid_order) == (
        0,
        ["pi_1: Charge pi_1 has already been refunded."],
    )
    assert f"Error refunding payment pi_1 for order {paid_order.ref}" in caplog.text
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.PROCESSING
    assert paid_order.amount_refunded == 0


def test_refund_order_no_stripe_payments(order, requests_mock):
    order.pay(amount=Decimal("20.00"), transaction_id="cash", payment_method="cash")
    assert StripeConnectPayment.refund_order(order) == (0, [])
    assert not requests_mock.called


@pytest.mark.parametrize("status,refunded", [(200, True), (400, False)])
def test_refund_payment(paid_order, requests_mock, status, refunded):
    # salesman's refund interface
    requests_mock.post(REFUNDS_URL, status_code=status, json=refund_json())
    assert StripeConnectPayment().refund_payment(paid_order.payments.get()) is refunded


def test_record_refund_partial(paid_order, refund_signals):
    assert StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    paid_order.refresh_from_db()
    # a partial refund doesn't change the status
    assert paid_order.status == Order.Status.PROCESSING
    assert paid_order.amount_received == Decimal("20.00")
    assert paid_order.amount_refunded == Decimal("5.00")
    assert paid_order.amount_due == 0
    assert [note.message for note in paid_order.notes.all()] == [
        "Refund re_1 of £5.00 (partial refund)"
    ]
    assert refund_signals[0]["fully_refunded"] is False


def test_record_refund_once(paid_order, refund_signals):
    assert StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    assert not StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    assert paid_order.amount_refunded == Decimal("5.00")
    assert len(refund_signals) == 1


def test_record_refunds_to_full(paid_order):
    StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    StripeConnectPayment.record_refund(paid_order, "re_2", Decimal("15.00"))
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.REFUNDED
    assert paid_order.notes.last().message == "Refund re_2 of £15.00"


# charge.refunded


def charge_refunded(payment_intent="pi_1"):
    return mock_event("charge.refunded", Mock(payment_intent=payment_intent))


def test_charge_refunded(client, paid_order, requests_mock, account):
    refunds = requests_mock.get(REFUNDS_URL, json=refunds_list(refund_json(amount=500)))
    resp = post_event(client, charge_refunded())
    assert resp.content.decode() == "1 refund(s) recorded"
    assert refunds.last_request.qs == {"payment_intent": ["pi_1"], "limit": ["100"]}
    assert refunds.last_request.headers["Stripe-Account"] == account
    assert paid_order.amount_refunded == Decimal("5.00")


def test_charge_refunded_twice(client, paid_order, requests_mock):
    # e.g. a duplicate event, or a refund made from the orders admin
    requests_mock.get(REFUNDS_URL, json=refunds_list(refund_json(amount=500)))
    post_event(client, charge_refunded())
    resp = post_event(client, charge_refunded())
    assert resp.content.decode() == "0 refund(s) recorded"
    assert paid_order.payments.filter(amount__lt=0).count() == 1


def test_charge_refunded_ignores_failed_refunds(client, paid_order, requests_mock):
    requests_mock.get(
        REFUNDS_URL,
        json=refunds_list(
            refund_json("re_1", status="failed"),
            refund_json("re_2", status="canceled"),
            refund_json("re_3", amount=300, status="pending"),
        ),
    )
    post_event(client, charge_refunded())
    assert [p.transaction_id for p in paid_order.payments.filter(amount__lt=0)] == ["re_3"]


def test_charge_refunded_unknown_payment(client, requests_mock, caplog):
    # e.g. a payment made outside the shop: logged as information, not an error
    caplog.set_level(logging.INFO)
    resp = post_event(client, charge_refunded("pi_unknown"))
    assert resp.content.decode() == "Event ignored: not a shop payment"
    assert logged(caplog, logging.INFO) == [
        "Refund received for payment pi_unknown, which isn't a shop payment"
    ]
    assert logged(caplog, logging.ERROR) == []
    assert not requests_mock.called


# charge.refund.updated


def refund_updated(status, refund_id="re_1"):
    return mock_event("charge.refund.updated", Mock(id=refund_id, status=status, amount=500))


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_refund_failed(client, paid_order, status, caplog):
    StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    resp = post_event(client, refund_updated(status))
    assert resp.content.decode() == "Refund removed"
    assert not paid_order.payments.filter(transaction_id="re_1").exists()
    assert paid_order.amount_refunded == 0
    message = (
        f"Refund re_1 of £5.00 {status}; it has been removed from the order's "
        "payments. Check the order status."
    )
    assert paid_order.notes.last().message == message
    assert message in caplog.text


@pytest.mark.parametrize("status,refund_id", [("succeeded", "re_1"), ("failed", "re_unknown")])
def test_refund_updated_ignored(client, paid_order, status, refund_id):
    StripeConnectPayment.record_refund(paid_order, "re_1", Decimal("5.00"))
    resp = post_event(client, refund_updated(status, refund_id))
    assert resp.content.decode() == "Event ignored"
    assert paid_order.payments.filter(transaction_id="re_1").exists()
