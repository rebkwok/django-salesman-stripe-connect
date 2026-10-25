"""The webhook, and fulfilling orders from completed checkout sessions"""
import logging
from unittest.mock import Mock, patch

import pytest
import stripe
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from salesman.core.utils import get_salesman_model

from salesman_stripe_connect import signals, views
from salesman_stripe_connect.models import ConnectedAccount
from salesman_stripe_connect.payment import StripeConnectPayment
from .utils import logged, mock_event, mock_session, post_event

pytestmark = pytest.mark.django_db

Basket = get_salesman_model("Basket")
Order = get_salesman_model("Order")
OrderPayment = get_salesman_model("OrderPayment")


def completed(basket_or_order, **params):
    kind = "basket" if isinstance(basket_or_order, Basket) else "order"
    return mock_event(
        "checkout.session.completed",
        mock_session(client_reference_id=f"{kind}_{basket_or_order.id}", **params),
    )


# The webhook


def test_webhook_url():
    assert reverse("stripe-connect-webhook") == "/stripe/webhook/"


@pytest.mark.parametrize(
    "error,message",
    [
        (ValueError("bad json"), "Invalid payload"),
        (stripe.SignatureVerificationError("bad", "sig"), "Invalid signature"),
    ],
)
def test_webhook_invalid(client, error, message):
    with patch("salesman_stripe_connect.payment.stripe.Webhook") as mock_webhook:
        mock_webhook.construct_event.side_effect = error
        resp = client.post(reverse("stripe-connect-webhook"), data={})
    assert resp.status_code == 400
    assert resp.content.decode() == message


def test_webhook_signature_checked(client, settings):
    with patch("salesman_stripe_connect.payment.stripe.Webhook") as mock_webhook:
        mock_webhook.construct_event.return_value = mock_event("other", None)
        client.post(
            reverse("stripe-connect-webhook"),
            data="{}",
            content_type="application/json",
            headers={"Stripe-Signature": "t=1,v1=sig"},
        )
    mock_webhook.construct_event.assert_called_once_with(
        b"{}", "t=1,v1=sig", settings.SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET
    )


def test_webhook_get_not_allowed(client):
    assert client.get(reverse("stripe-connect-webhook")).status_code == 405


@pytest.mark.parametrize("account", ["acct_other", None])
def test_webhook_other_account_ignored(client, checkout_basket, account, caplog):
    caplog.set_level(logging.INFO)
    # events from other connected accounts, or the platform's own events
    event = completed(checkout_basket)
    event.account = account
    resp = post_event(client, event)
    assert resp.content.decode() == "Event ignored"
    assert not Order.objects.exists()
    # e.g. another site's account if the platform is shared: not a warning
    assert logged(caplog, logging.INFO) == [
        f"Ignoring Stripe checkout.session.completed event from account {account}"
    ]
    assert logged(caplog, logging.WARNING) == []


def test_webhook_platform_event_ignored(client):
    # platform events have no account
    event = Mock(spec=["type", "data"], type="checkout.session.completed")
    assert post_event(client, event).content.decode() == "Event ignored"


def test_webhook_other_event_ignored(client):
    resp = post_event(client, mock_event("customer.created", Mock()))
    assert resp.status_code == 200
    assert resp.content.decode() == "Event ignored"


def test_webhook_account_deauthorized(client, account, caplog):
    received = []

    def receiver(sender, **kwargs):
        received.append(kwargs["account_id"])

    signals.account_deauthorized.connect(receiver)
    try:
        resp = post_event(client, mock_event("account.application.deauthorized", Mock()))
    finally:
        signals.account_deauthorized.disconnect(receiver)
    assert resp.content.decode() == "Account deauthorized"
    assert f"Stripe account {account} has disconnected from the platform" in caplog.text
    # the account is forgotten, so payments can't be made
    assert not ConnectedAccount.objects.exists()
    assert received == [account]


def test_webhook_other_account_deauthorized(client, account):
    event = mock_event("account.application.deauthorized", Mock(), account="acct_other")
    assert post_event(client, event).content.decode() == "Event ignored"
    assert ConnectedAccount.get_account_id() == account


@pytest.mark.parametrize("event_account", ["acct_test", "acct_other"])
def test_webhook_account_authorized(client, event_account, caplog):
    caplog.set_level(logging.INFO)
    event = mock_event("account.application.authorized", Mock(), account=event_account)
    assert post_event(client, event).content.decode() == "Account authorized"
    assert f"Stripe account {event_account} has connected to the platform" in caplog.text


def test_webhook_no_connected_account(client, connected_account, checkout_basket, caplog):
    connected_account.delete()
    resp = post_event(client, completed(checkout_basket))
    assert resp.content.decode() == "Event ignored"
    assert not Order.objects.exists()
    # an account can still be connected
    event = mock_event("account.application.authorized", Mock())
    assert post_event(client, event).content.decode() == "Account authorized"


def test_get_payment_method():
    # the project's payment method
    assert isinstance(views.get_payment_method(), StripeConnectPayment)
    with patch.object(views.payment_methods_pool, "get_payments", return_value=[]):
        with pytest.raises(
            ImproperlyConfigured, match="No StripeConnectPayment payment method"
        ):
            views.get_payment_method()


# checkout.session.completed


def test_session_completed(client, checkout_basket):
    resp = post_event(client, completed(checkout_basket))
    assert resp.status_code == 200
    assert resp.content.decode() == "Order fulfilled"

    assert not Basket.objects.filter(id=checkout_basket.id).exists()
    order = Order.objects.get()
    assert order.status == Order.Status.PROCESSING
    assert order.total == 20
    assert order.amount_paid == 20
    assert order.extra["stripe_session_id"] == "test-session"
    payment = order.payments.get()
    assert payment.transaction_id == "pi_1"
    assert payment.payment_method == "stripe"
    assert not order.notes.exists()


def test_session_completed_paid_status(client, checkout_basket, settings):
    settings.SALESMAN_STRIPE_CONNECT_PAID_STATUS = "HOLD"
    post_event(client, completed(checkout_basket))
    assert Order.objects.get().status == Order.Status.HOLD


def test_session_completed_payment_in_progress(client, paying_basket):
    post_event(client, completed(paying_basket))
    # the session isn't copied to the order
    assert "stripe_session" not in Order.objects.get().extra


def test_session_completed_duplicate_event(client, checkout_basket):
    post_event(client, completed(checkout_basket))
    # Stripe sends the same event again
    resp = post_event(client, completed(checkout_basket))
    assert resp.content.decode() == "Order already fulfilled"
    assert Order.objects.count() == 1
    assert OrderPayment.objects.count() == 1


def test_session_completed_payment_already_recorded(client, paid_order, checkout_basket):
    # e.g. an order whose payment was recorded before it had a session id
    resp = post_event(client, completed(checkout_basket))
    assert resp.content.decode() == "Order already fulfilled"
    assert Order.objects.count() == 1


def test_session_completed_missing_basket(client, caplog):
    resp = post_event(
        client,
        mock_event("checkout.session.completed", mock_session(client_reference_id="basket_1234")),
    )
    # 200, so Stripe doesn't keep retrying
    assert resp.status_code == 200
    assert resp.content.decode() == "Missing basket"
    assert not Order.objects.exists()
    assert "Stripe payment pi_1 received for missing basket 1234" in caplog.text


@pytest.mark.parametrize("reference", ["unknown", None])
def test_session_completed_not_shop_payment(client, caplog, reference):
    # e.g. a payment link, or another integration on the connected account
    caplog.set_level(logging.INFO)
    resp = post_event(
        client,
        mock_event("checkout.session.completed", mock_session(client_reference_id=reference)),
    )
    assert resp.content.decode() == "Event ignored: not a shop payment"
    assert logged(caplog, logging.INFO) == [
        f"Stripe session test-session isn't a shop payment (reference {reference})"
    ]
    assert logged(caplog, logging.ERROR) == []
    assert not Order.objects.exists()


def test_session_completed_empty_basket(client, checkout_basket, caplog):
    # Payment completed after the basket's items expired
    checkout_basket.clear()
    resp = post_event(client, completed(checkout_basket))
    assert resp.content.decode() == "Order fulfilled"

    # Order and payment are recorded, and the order is flagged
    order = Order.objects.get()
    assert order.amount_paid == 20
    notes = list(order.notes.values_list("message", flat=True))
    assert notes == [
        "Payment pi_1 was received but the basket was empty (items may have "
        "expired). Check and refund if necessary.",
        "Payment pi_1 of £20.00 doesn't match the order total of £0.00.",
    ]
    assert "basket was empty" in caplog.text


def test_session_completed_amount_mismatch(client, checkout_basket):
    post_event(client, completed(checkout_basket, amount_total=1500))
    order = Order.objects.get()
    assert order.amount_paid == 15
    assert [note.message for note in order.notes.all()] == [
        "Payment pi_1 of £15.00 doesn't match the order total of £20.00."
    ]
    assert not order.notes.first().public


def test_session_completed_existing_order(client, order):
    resp = post_event(client, completed(order))
    assert resp.content.decode() == "Order fulfilled"
    resp = post_event(client, completed(order))
    assert resp.content.decode() == "Order already fulfilled"
    assert Order.objects.get().extra["stripe_session_id"] == "test-session"
    assert order.payments.count() == 1
    assert not order.notes.exists()


def test_session_completed_missing_order(client, caplog):
    resp = post_event(
        client,
        mock_event("checkout.session.completed", mock_session(client_reference_id="order_1234")),
    )
    assert resp.content.decode() == "Missing order"
    assert "Stripe payment pi_1 received for missing order 1234" in caplog.text


def test_fulfil_session_directly(rf, checkout_basket):
    session = mock_session(client_reference_id=f"basket_{checkout_basket.id}")
    order, message = StripeConnectPayment.fulfil_session(rf.get("/"), session)
    assert message == "Order fulfilled"
    assert StripeConnectPayment.fulfil_session(rf.get("/"), session) == (
        order,
        "Order already fulfilled",
    )
