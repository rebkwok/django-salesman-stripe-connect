"""
Basket payments that expire, are cancelled, or are checked when the customer
returns; and the success and cancel views
"""
import logging
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse
from model_bakery import baker
from salesman.basket.models import BASKET_ID_SESSION_KEY
from salesman.core.utils import get_salesman_model

from salesman_stripe_connect.payment import StripeConnectPayment
from .utils import SESSION_URL, mock_event, post_event, session_json

pytestmark = pytest.mark.django_db

Basket = get_salesman_model("Basket")
Order = get_salesman_model("Order")


def expired_session(basket, session_id="test-session"):
    return Mock(id=session_id, client_reference_id=f"basket_{basket.id}")


# checkout.session.expired


def test_session_expired(client, paying_basket):
    resp = post_event(client, mock_event("checkout.session.expired", expired_session(paying_basket)))
    assert resp.content.decode() == "Payment expired"
    paying_basket.refresh_from_db()
    assert not paying_basket.payment_in_progress
    # the basket keeps its items (by default)
    assert paying_basket.items.exists()


def test_session_expired_hook(paying_basket):
    with patch.object(StripeConnectPayment, "basket_payment_expired") as hook:
        basket, _ = StripeConnectPayment.expire_basket_payment(expired_session(paying_basket))
    hook.assert_called_once_with(basket)


def test_session_expired_other_session(paying_basket):
    # e.g. a session that was cancelled before a new one was started
    _, message = StripeConnectPayment.expire_basket_payment(
        expired_session(paying_basket, session_id="other-session")
    )
    assert message == "Event ignored"
    paying_basket.refresh_from_db()
    assert paying_basket.payment_in_progress


@pytest.mark.parametrize("reference", ["basket_1234", "order_1"])
def test_session_expired_ignored(reference):
    session = Mock(id="test-session", client_reference_id=reference)
    assert StripeConnectPayment.expire_basket_payment(session) == (None, "Event ignored")


# Cancelling


def test_cancel_basket_payment(paying_basket, requests_mock, account):
    expire = requests_mock.post(SESSION_URL + "/expire", json=session_json(status="expired"))
    with patch.object(StripeConnectPayment, "basket_payment_cancelled") as hook:
        assert StripeConnectPayment.cancel_basket_payment(paying_basket) is True
    assert expire.last_request.headers["Stripe-Account"] == account
    paying_basket.refresh_from_db()
    assert not paying_basket.payment_in_progress
    hook.assert_called_once()


def test_cancel_basket_payment_already_expired(paying_basket, requests_mock):
    requests_mock.post(
        SESSION_URL + "/expire",
        status_code=400,
        json={"error": {"message": "Only open sessions can be expired"}},
    )
    requests_mock.get(SESSION_URL, json=session_json(status="expired"))
    assert StripeConnectPayment.cancel_basket_payment(paying_basket) is True
    paying_basket.refresh_from_db()
    assert not paying_basket.payment_in_progress


def test_cancel_basket_payment_already_completed(paying_basket, requests_mock):
    requests_mock.post(
        SESSION_URL + "/expire",
        status_code=400,
        json={"error": {"message": "Only open sessions can be expired"}},
    )
    requests_mock.get(SESSION_URL, json=session_json())
    assert StripeConnectPayment.cancel_basket_payment(paying_basket) is False
    paying_basket.refresh_from_db()
    assert paying_basket.payment_in_progress


def test_cancel_basket_payment_not_in_progress(checkout_basket, requests_mock):
    # e.g. the payment was cancelled in another tab; nothing to do
    assert StripeConnectPayment.cancel_basket_payment(checkout_basket) is True
    assert not requests_mock.called


# Checking the payment


def test_check_basket_payment_paid(rf, paying_basket, requests_mock):
    requests_mock.get(
        SESSION_URL, json=session_json(client_reference_id=f"basket_{paying_basket.id}")
    )
    order = StripeConnectPayment.check_basket_payment(rf.get("/"), paying_basket)
    assert order == Order.objects.get()
    assert order.amount_paid == 20


def test_check_basket_payment_expired(rf, paying_basket, requests_mock):
    requests_mock.get(
        SESSION_URL,
        json=session_json(
            client_reference_id=f"basket_{paying_basket.id}",
            status="expired",
            payment_status="unpaid",
        ),
    )
    assert StripeConnectPayment.check_basket_payment(rf.get("/"), paying_basket) is None
    paying_basket.refresh_from_db()
    assert not paying_basket.payment_in_progress


def test_check_basket_payment_open(rf, paying_basket, requests_mock):
    requests_mock.get(SESSION_URL, json=session_json(status="open", payment_status="unpaid"))
    assert StripeConnectPayment.check_basket_payment(rf.get("/"), paying_basket) is None
    paying_basket.refresh_from_db()
    assert paying_basket.payment_in_progress


def test_check_basket_payment_error(rf, paying_basket, requests_mock, caplog):
    requests_mock.get(SESSION_URL, status_code=500, json={"error": {}})
    assert StripeConnectPayment.check_basket_payment(rf.get("/"), paying_basket) is None
    assert "Error retrieving Stripe session test-session" in caplog.text


# The request's basket


def test_get_request_basket(rf, basket):
    request = rf.get("/")
    request.user = AnonymousUser()
    request.session = {}
    assert StripeConnectPayment.get_request_basket(request) is None
    request.session = {BASKET_ID_SESSION_KEY: basket.id}
    assert StripeConnectPayment.get_request_basket(request) == basket

    request.user = baker.make("auth.User")
    assert StripeConnectPayment.get_request_basket(request) is None
    basket.user = request.user
    basket.save()
    assert StripeConnectPayment.get_request_basket(request) == basket


# The success and cancel views (with the default templates)


def test_view_urls():
    assert reverse("stripe-connect-success") == "/api/payment/stripe/success/"
    assert reverse("stripe-connect-cancel") == "/api/payment/stripe/cancel/"


def test_cancel_view(rf, paying_basket, requests_mock):
    requests_mock.post(SESSION_URL + "/expire", json=session_json(status="expired"))
    request = rf.get("/")
    request.user = AnonymousUser()
    request.session = {BASKET_ID_SESSION_KEY: paying_basket.id}
    resp = StripeConnectPayment.cancel_view(request)
    assert "Your payment was cancelled" in resp.content.decode()
    paying_basket.refresh_from_db()
    assert not paying_basket.payment_in_progress


def test_cancel_view_error(rf, paying_basket, requests_mock, caplog):
    requests_mock.post(SESSION_URL + "/expire", status_code=500, json={"error": {}})
    request = rf.get("/")
    request.user = AnonymousUser()
    request.session = {BASKET_ID_SESSION_KEY: paying_basket.id}
    resp = StripeConnectPayment.cancel_view(request)
    assert "Your payment was cancelled" in resp.content.decode()
    assert "Error cancelling Stripe session test-session" in caplog.text
    paying_basket.refresh_from_db()
    assert paying_basket.payment_in_progress


def test_cancel_view_no_basket(rf):
    request = rf.get("/")
    request.user = AnonymousUser()
    request.session = {}
    assert "Your payment was cancelled" in StripeConnectPayment.cancel_view(request).content.decode()


def success_request(rf, session_id="test-session"):
    request = rf.get(f"/?session_id={session_id}")
    request.user = AnonymousUser()
    return request


def test_success_view(rf, checkout_basket, requests_mock, account):
    session = requests_mock.get(
        SESSION_URL, json=session_json(client_reference_id=f"basket_{checkout_basket.id}")
    )
    resp = StripeConnectPayment.success_view(success_request(rf))
    order = Order.objects.get()
    assert f"Your order reference is <strong>{order.ref}</strong>" in resp.content.decode()
    assert session.last_request.headers["Stripe-Account"] == account


def test_success_view_context(rf, checkout_basket, requests_mock):
    requests_mock.get(
        SESSION_URL, json=session_json(client_reference_id=f"basket_{checkout_basket.id}")
    )
    with patch("salesman_stripe_connect.payment.render") as render:
        StripeConnectPayment.success_view(success_request(rf))
    _, template, context = render.call_args.args
    assert template == "salesman_stripe_connect/success.html"
    assert context == {
        "email": "test@test.com",
        "total": 20,
        "order": Order.objects.get(),
        "payment_pending": False,
    }


def test_success_view_unpaid(rf, checkout_basket, requests_mock):
    # a payment that completes later (e.g. a bank debit): the order, on hold
    requests_mock.get(
        SESSION_URL,
        json=session_json(client_reference_id=f"basket_{checkout_basket.id}", payment_status="unpaid"),
    )
    content = StripeConnectPayment.success_view(success_request(rf)).content.decode()
    order = Order.objects.get()
    assert order.status == Order.Status.HOLD
    assert f"Your order reference is <strong>{order.ref}</strong>." in content
    assert "Your order will be processed once your payment has been received." in content
    assert "Your order is being processed" not in content


def test_success_view_not_complete(rf, checkout_basket, requests_mock):
    # (e.g. the success page's address visited before paying): no order
    requests_mock.get(
        SESSION_URL,
        json=session_json(
            client_reference_id=f"basket_{checkout_basket.id}", status="open", payment_status="unpaid"
        ),
    )
    StripeConnectPayment.success_view(success_request(rf))
    assert not Order.objects.exists()


@pytest.mark.parametrize("url", ["/", "/?session_id=unknown"])
def test_success_view_no_session(rf, requests_mock, url, caplog):
    requests_mock.get(
        "https://api.stripe.com/v1/checkout/sessions/unknown",
        status_code=404,
        json={"error": {"message": "No such checkout.session"}},
    )
    resp = StripeConnectPayment.success_view(rf.get(url))
    assert resp.status_code == 200
    assert "Your order is being processed" in resp.content.decode()
    # an unknown session (the id is from the address): a warning, not an error
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    if "unknown" in url:
        assert "Unknown Stripe session unknown" in [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_success_view_stripe_error(rf, requests_mock, caplog):
    # (other errors, e.g. Stripe's down, are errors)
    requests_mock.get(SESSION_URL, status_code=500, json={"error": {"message": "Error"}})
    StripeConnectPayment.success_view(rf.get("/?session_id=test-session"))
    assert "Error retrieving Stripe session test-session" in [
        r.getMessage() for r in caplog.records if r.levelno == logging.ERROR
    ]


def test_success_view_fulfil_error(rf, checkout_basket, requests_mock, caplog):
    # An error fulfilling the order is logged and left for the webhook
    requests_mock.get(
        SESSION_URL, json=session_json(client_reference_id=f"basket_{checkout_basket.id}")
    )
    with patch.object(Order.objects, "create_from_basket", side_effect=Exception("Error")):
        resp = StripeConnectPayment.success_view(success_request(rf))
    assert "Your order is being processed" in resp.content.decode()
    assert "Error fulfilling Stripe session test-session" in caplog.text
    assert not Order.objects.exists()
    assert Basket.objects.filter(id=checkout_basket.id).exists()
