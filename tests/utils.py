"""Helpers for the tests"""
from unittest.mock import Mock, patch

from django.urls import reverse

# The connected account in the tests (see conftest)
ACCOUNT = "acct_test"

API = "https://api.stripe.com/v1"
SESSIONS_URL = f"{API}/checkout/sessions"
SESSION_URL = f"{SESSIONS_URL}/test-session"
REFUNDS_URL = f"{API}/refunds"
CUSTOMERS_URL = f"{API}/customers"
CUSTOMER_ID = "cus_test"


def session_json(**params):
    return {
        "id": "test-session",
        "object": "checkout.session",
        "client_reference_id": "basket_1234",
        "customer_details": {"email": "test@test.com"},
        "amount_total": 2000,
        "status": "complete",
        "payment_status": "paid",
        "payment_intent": "pi_1",
        "url": "https://stripe-session-url",
        **params,
    }


def mock_session(**params):
    return Mock(**{**session_json(), **params})


def mock_event(event_type, obj, account=None):
    return Mock(
        type=event_type,
        account=account or ACCOUNT,
        data=Mock(object=obj),
    )


def post_event(client, event):
    """
    Post the event to the webhook, skipping the signature check. The webhook
    uses StripeConnectPayment (rather than the project's subclass).
    """
    from salesman_stripe_connect.payment import StripeConnectPayment

    with (
        patch("salesman_stripe_connect.payment.stripe.Webhook") as mock_webhook,
        patch(
            "salesman_stripe_connect.views.get_payment_method",
            return_value=StripeConnectPayment(),
        ),
    ):
        mock_webhook.construct_event.return_value = event
        return client.post(
            reverse("stripe-connect-webhook"),
            data={},
            headers={"Stripe-Signature": "t=1,v1=dummy"},
        )


def logged(caplog, level):
    """Messages logged at the level (e.g. logging.INFO)"""
    return [record.getMessage() for record in caplog.records if record.levelno == level]
