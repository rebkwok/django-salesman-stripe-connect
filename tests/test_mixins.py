from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


def test_basket_payment(basket):
    assert not basket.payment_in_progress
    assert basket.stripe_session_id is None
    assert basket.stripe_session_url is None
    basket.start_payment("cs_1", "https://stripe-session-url")
    basket.refresh_from_db()
    assert basket.payment_in_progress
    assert basket.stripe_session_id == "cs_1"
    assert basket.stripe_session_url == "https://stripe-session-url"
    basket.end_payment()
    basket.refresh_from_db()
    assert not basket.payment_in_progress


def test_order_amounts(order):
    # order total is £20
    assert (order.amount_received, order.amount_refunded, order.amount_due) == (0, 0, 20)
    order.pay(amount=Decimal("15.00"), transaction_id="pi_1")
    order.pay(amount=Decimal("-5.00"), transaction_id="re_1")
    assert order.amount_received == Decimal("15.00")
    assert order.amount_refunded == Decimal("5.00")
    assert order.amount_paid == Decimal("10.00")
    # refunds don't make an order due again
    assert order.amount_due == Decimal("5.00")


def test_order_paid_status(order):
    # order total is £20
    assert not order.is_paid
    assert order.amount_outstanding == 20
    assert order.refund_status == ""
    order.pay(amount=Decimal("20.00"), transaction_id="pi_1")
    assert order.is_paid
    assert order.amount_outstanding == 0

    # refunds don't make the order unpaid or outstanding (unlike salesman's
    # is_paid and amount_outstanding)
    order.pay(amount=Decimal("-5.00"), transaction_id="re_1")
    assert order.is_paid
    assert order.amount_outstanding == 0
    assert order.refund_status == "Partially refunded"
    order.pay(amount=Decimal("-15.00"), transaction_id="re_2")
    assert order.is_paid
    assert order.refund_status == "Fully refunded"
