"""Creating checkout sessions"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.conf import settings
from model_bakery import baker
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from salesman.checkout.payment import PaymentError

from salesman_stripe_connect.conf import app_settings
from salesman_stripe_connect.payment import StripeAccountNotConnected, StripeConnectPayment, get_client
from .utils import ACCOUNT, SESSIONS_URL, session_json

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_():
    return RequestFactory().post("/")


@pytest.fixture
def create_session(requests_mock):
    yield requests_mock.post(
        SESSIONS_URL, json=session_json(status="open", payment_status="unpaid")
    )


def test_basket_payment(payment, checkout_basket, request_, create_session, account):
    assert payment.basket_payment(checkout_basket, request_) == "https://stripe-session-url"
    # the session is created on the connected account
    request = create_session.last_request
    assert request.headers["Stripe-Account"] == account
    assert request.headers["Stripe-Version"] == "2026-09-30.endive"
    # the session is recorded on the basket, which locks it
    checkout_basket.refresh_from_db()
    assert checkout_basket.payment_in_progress
    assert checkout_basket.stripe_session_id == "test-session"
    assert checkout_basket.stripe_session_url == "https://stripe-session-url"


def test_basket_payment_already_in_progress(payment, paying_basket, request_, requests_mock):
    with pytest.raises(PaymentError, match="already in progress"):
        payment.basket_payment(paying_basket, request_)
    assert not requests_mock.called


def test_order_payment(payment, order, request_, create_session):
    assert payment.order_payment(order, request_) == "https://stripe-session-url"
    assert f"client_reference_id=order_{order.id}" in create_session.last_request.text


def test_payment_stripe_error(payment, checkout_basket, request_, requests_mock, caplog):
    requests_mock.post(
        SESSIONS_URL, status_code=400, json={"error": {"message": "Invalid request"}}
    )
    with pytest.raises(PaymentError, match="there was a problem starting your payment"):
        payment.basket_payment(checkout_basket, request_)
    assert f"Error creating Stripe checkout session for basket_{checkout_basket.id}" in caplog.text
    checkout_basket.refresh_from_db()
    assert not checkout_basket.payment_in_progress


def test_session_data(payment, checkout_basket, request_):
    assert payment.get_session_data(checkout_basket, request_) == {
        "mode": "payment",
        "success_url": "http://testserver/api/payment/stripe/success/"
        "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": "http://testserver/api/payment/stripe/cancel/",
        "client_reference_id": f"basket_{checkout_basket.id}",
        "customer_email": "test@test.com",
        "line_items": [
            {
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": 1000,
                    "product_data": {"name": "Test Product - Small"},
                },
                "quantity": 2,
            }
        ],
    }


def test_session_expiry(checkout_basket, request_, freezer):
    freezer.move_to("2024-01-01 10:00")

    class Payment(StripeConnectPayment):
        def get_session_expiry(self, obj):
            return timedelta(minutes=45)

    data = Payment().get_session_data(checkout_basket, request_)
    assert data["expires_at"] == int(datetime(2024, 1, 1, 10, 45, tzinfo=UTC).timestamp())


def test_customer_email(payment, basket, order):
    # basket: from the checkout; order: its email
    basket.extra["email"] = "basket@test.com"
    assert payment.get_customer_email(basket) == "basket@test.com"
    order.email = "order@test.com"
    assert payment.get_customer_email(order) == "order@test.com"
    # otherwise, the user's email, or none
    order.email = ""
    assert payment.get_customer_email(order) is None
    order.user = baker.make(settings.AUTH_USER_MODEL, email="user@test.com")
    assert payment.get_customer_email(order) == "user@test.com"


def test_session_data_no_email(payment, basket, request_):
    assert "customer_email" not in payment.get_session_data(basket, request_)


# Line items


def item(name, quantity, total, rows=()):
    return SimpleNamespace(
        name=name,
        quantity=quantity,
        total=Decimal(total),
        extra_rows=[{"label": label, "amount": amount} for label, amount in rows],
    )


def order_like(items, total, rows=()):
    return SimpleNamespace(
        get_items=lambda: items,
        total=Decimal(total),
        extra_rows=[{"label": label, "amount": amount} for label, amount in rows],
    )


def line_item(name, unit_amount, quantity, description=None):
    product_data = {"name": name}
    if description:
        product_data["description"] = description
    return {
        "price_data": {
            "currency": "gbp",
            "unit_amount": unit_amount,
            "product_data": product_data,
        },
        "quantity": quantity,
    }


def test_line_items(payment):
    obj = order_like(
        [
            item("Mug", 3, "24.00", rows=[("Sale: 20% off", "-6.00")]),
            item("Calendar", 1, "12.00"),
        ],
        "40.99",
        rows=[("Shipping", "4.99")],
    )
    assert payment.get_line_items(obj, None) == [
        line_item("Mug", 800, 3, description="Sale: 20% off"),
        line_item("Calendar", 1200, 1),
        line_item("Shipping", 499, 1),
    ]


def test_line_items_basket_rows(payment):
    # basket rows are serializers
    obj = order_like([item("Mug", 1, "10.00")], "14.99")
    obj.extra_rows = {"shipping": Mock(data={"label": "Shipping", "amount": "4.99"})}
    assert payment.get_line_items(obj, None) == [
        line_item("Mug", 1000, 1),
        line_item("Shipping", 499, 1),
    ]


def test_line_items_uneven_quantity(payment):
    # the item total doesn't divide equally by the quantity
    obj = order_like([item("Mug", 3, "10.00")], "10.00")
    assert payment.get_line_items(obj, None) == [line_item("3 x Mug", 1000, 1)]


def test_line_items_zero_row_skipped(payment):
    obj = order_like([item("Mug", 1, "10.00")], "10.00", rows=[("Shipping", "0.00")])
    assert payment.get_line_items(obj, None) == [line_item("Mug", 1000, 1)]


@pytest.mark.parametrize(
    "rows,total",
    [
        # a discount on the whole order can't be a line item
        ([("Discount", "-5.00")], "15.00"),
        # the line items don't add up to the total
        ([], "19.00"),
    ],
)
def test_line_items_total_only(payment, rows, total):
    obj = order_like([item("Mug", 2, "20.00"), item("Card", 1, "0.00")], total, rows)
    assert payment.get_line_items(obj, None) == [
        line_item(
            "Order total",
            int(Decimal(total) * 100),
            1,
            description="2 x Mug, 1 x Card",
        )
    ]


# References


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("basket_12", ("basket", 12)),
        ("order_3", ("order", 3)),
        ("order_x", (None, None)),
        ("other_3", (None, None)),
        ("unknown", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_reference(reference, expected):
    assert StripeConnectPayment.parse_reference(reference) == expected


# Settings


@pytest.mark.parametrize(
    "currency,expected", [("GBP", "£1.50"), ("usd", "$1.50"), ("chf", "1.50 CHF")]
)
def test_format_amount(settings, currency, expected):
    settings.SALESMAN_STRIPE_CONNECT_CURRENCY = currency
    assert StripeConnectPayment.format_amount(Decimal("1.5")) == expected


def test_settings_defaults(settings):
    for name in ["CURRENCY", "PAYMENT_LABEL", "PAID_STATUS", "PAGES_ENABLED"]:
        delattr(settings, f"SALESMAN_STRIPE_CONNECT_{name}")
    assert app_settings.SALESMAN_STRIPE_CONNECT_CURRENCY == "gbp"
    assert app_settings.SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL == "Pay with Stripe"
    assert app_settings.SALESMAN_STRIPE_CONNECT_PAID_STATUS == "PROCESSING"
    assert app_settings.SALESMAN_STRIPE_CONNECT_PAGES_ENABLED is True
    assert app_settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT is True


def test_required_setting(settings):
    settings.SALESMAN_STRIPE_CONNECT_CLIENT_ID = ""
    with pytest.raises(
        ImproperlyConfigured, match="The SALESMAN_STRIPE_CONNECT_CLIENT_ID setting is required"
    ):
        app_settings.SALESMAN_STRIPE_CONNECT_CLIENT_ID


# No connected account


def test_request_options_no_account(connected_account):
    assert StripeConnectPayment.request_options() == {"stripe_account": ACCOUNT}
    connected_account.delete()
    with pytest.raises(StripeAccountNotConnected):
        StripeConnectPayment.request_options()


def test_payment_no_account(
    payment, checkout_basket, request_, connected_account, requests_mock, caplog
):
    connected_account.delete()
    with pytest.raises(PaymentError, match="card payments aren't available"):
        payment.basket_payment(checkout_basket, request_)
    assert not requests_mock.called
    assert "Stripe payment refused: no Stripe account is connected" in caplog.text
    checkout_basket.refresh_from_db()
    assert not checkout_basket.payment_in_progress


def test_client():
    client = get_client("sk_test_1")
    assert client is get_client("sk_test_1")
    assert client is not get_client("sk_test_2")
