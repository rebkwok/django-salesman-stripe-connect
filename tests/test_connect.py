"""Connecting and disconnecting the Stripe account (connect_views)"""
import html
import logging
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.urls import reverse
from model_bakery import baker

from salesman_stripe_connect import signals
from salesman_stripe_connect.connect_views import STATE_SESSION_KEY
from salesman_stripe_connect.models import ConnectedAccount

pytestmark = pytest.mark.django_db

CONNECT_URL = "/stripe/connect/"
AUTHORIZE_URL = "/stripe/connect/authorize/"
DISCONNECT_URL = "/stripe/connect/disconnect/"
CALLBACK_URL = "/stripe/oauth/callback/"
TOKEN_URL = "https://connect.stripe.com/oauth/token"
DEAUTHORIZE_URL = "https://connect.stripe.com/oauth/deauthorize"


@pytest.fixture(autouse=True)
def connect_settings(settings):
    settings.SALESMAN_STRIPE_CONNECT_PAGES_ENABLED = True
    settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT = True
    settings.SALESMAN_STRIPE_CONNECT_CLIENT_ID = "ca_test"


@pytest.fixture
def connect_user():
    """A (non-superuser) user with the connect permission"""
    user = baker.make("auth.User", username="connector")
    user.user_permissions.add(Permission.objects.get(codename="connect_stripe_account"))
    return user


@pytest.fixture
def connect_client(client, connect_user):
    client.force_login(connect_user)
    return client


@pytest.fixture
def not_connected(connected_account):
    connected_account.delete()


@pytest.fixture
def deauthorized_signals():
    received = []

    def receiver(sender, **kwargs):
        received.append(kwargs["account_id"])

    signals.account_deauthorized.connect(receiver)
    yield received
    signals.account_deauthorized.disconnect(receiver)


def text(resp):
    """The page's text, unescaped and with whitespace collapsed"""
    return " ".join(html.unescape(resp.content.decode()).split())


def messages_for(resp):
    return [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]


def test_urls():
    assert reverse("stripe-connect") == CONNECT_URL
    assert reverse("stripe-connect-authorize") == AUTHORIZE_URL
    assert reverse("stripe-connect-disconnect") == DISCONNECT_URL
    assert reverse("stripe-connect-oauth-callback") == CALLBACK_URL


# Access


PAGES = [
    ("get", CONNECT_URL),
    ("post", AUTHORIZE_URL),
    ("get", CALLBACK_URL),
    ("get", DISCONNECT_URL),
    ("post", DISCONNECT_URL),
]


@pytest.mark.parametrize("method,url", PAGES)
def test_login_required(client, requests_mock, method, url):
    resp = getattr(client, method)(url)
    assert resp.status_code == 302
    assert resp.url == f"/login/?next={url}"
    assert ConnectedAccount.objects.exists()
    assert not requests_mock.called


@pytest.mark.parametrize("method,url", PAGES)
def test_permission_required(client, requests_mock, method, url):
    # e.g. staff without the permission
    client.force_login(baker.make("auth.User", is_staff=True))
    assert getattr(client, method)(url).status_code == 403
    assert ConnectedAccount.objects.exists()
    assert not requests_mock.called


def test_superuser_allowed(client):
    client.force_login(baker.make("auth.User", is_superuser=True))
    assert client.get(CONNECT_URL).status_code == 200


@pytest.mark.parametrize("method,url", PAGES)
def test_pages_disabled(connect_client, settings, method, url):
    settings.SALESMAN_STRIPE_CONNECT_PAGES_ENABLED = False
    assert getattr(connect_client, method)(url).status_code == 404


def test_methods(connect_client):
    assert connect_client.post(CONNECT_URL).status_code == 405
    assert connect_client.get(AUTHORIZE_URL).status_code == 405
    assert connect_client.post(CALLBACK_URL).status_code == 405


# The connect page


def test_connect_page_not_connected(connect_client, not_connected):
    content = text(connect_client.get(CONNECT_URL))
    assert "No Stripe account is connected, so card payments can't be taken." in content
    assert f'action="{AUTHORIZE_URL}"' in content
    assert DISCONNECT_URL not in content


def test_connect_page_connected(connect_client, account):
    content = text(connect_client.get(CONNECT_URL))
    assert (
        f"Card payments are made to the Stripe account <strong>{account}</strong> "
        "(test mode)"
    ) in content
    assert f'href="{DISCONNECT_URL}"' in content
    assert AUTHORIZE_URL not in content


def test_connect_page_no_disconnect(connect_client, settings):
    settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT = False
    assert DISCONNECT_URL not in text(connect_client.get(CONNECT_URL))


# Connecting


def test_authorize_redirects_to_stripe(connect_client, not_connected):
    resp = connect_client.post(AUTHORIZE_URL)
    assert resp.status_code == 302
    url = urlparse(resp.url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://connect.stripe.com/oauth/authorize"
    state = connect_client.session[STATE_SESSION_KEY]
    assert len(state) > 30
    assert parse_qs(url.query) == {
        "client_id": ["ca_test"],
        "response_type": ["code"],
        "scope": ["read_write"],
        "redirect_uri": ["http://testserver/stripe/oauth/callback/"],
        "state": [state],
    }


def test_authorize_already_connected(connect_client):
    # another account can't be connected
    resp = connect_client.post(AUTHORIZE_URL)
    assert resp.url == CONNECT_URL
    assert STATE_SESSION_KEY not in connect_client.session


def start_connecting(client):
    client.post(AUTHORIZE_URL)
    return client.session[STATE_SESSION_KEY]


def token_json(account="acct_new", livemode=False):
    return {
        "access_token": "sk_access",
        "refresh_token": "rt_refresh",
        "livemode": livemode,
        "scope": "read_write",
        "stripe_user_id": account,
        "token_type": "bearer",
    }


def test_callback(connect_client, not_connected, requests_mock, caplog):
    caplog.set_level(logging.INFO)
    token = requests_mock.post(TOKEN_URL, json=token_json())
    state = start_connecting(connect_client)
    resp = connect_client.get(CALLBACK_URL, {"code": "ac_123", "state": state})
    assert resp.status_code == 302
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == [
        ("success", "Your Stripe account has been connected. Card payments will now be made to it.")
    ]
    # the connect page shows the account, and the message
    content = text(connect_client.get(CONNECT_URL))
    assert "the Stripe account <strong>acct_new</strong>" in content
    assert "Your Stripe account has been connected." in content
    # the code is exchanged with the platform's key
    assert parse_qs(token.last_request.text) == {
        "grant_type": ["authorization_code"],
        "code": ["ac_123"],
    }
    assert token.last_request.headers["Authorization"] == "Bearer sk_test_dummy"
    # only the account id is saved (not the tokens)
    account = ConnectedAccount.objects.get()
    assert (account.account_id, account.livemode) == ("acct_new", False)
    assert "Stripe account acct_new connected by connector" in caplog.text
    # the state can't be used again
    assert STATE_SESSION_KEY not in connect_client.session


@pytest.mark.parametrize(
    "params",
    [
        {"code": "ac_123"},
        {"code": "ac_123", "state": "wrong"},
        {"code": "ac_123", "state": ""},
    ],
)
def test_callback_invalid_state(connect_client, not_connected, requests_mock, params):
    start_connecting(connect_client)
    resp = connect_client.get(CALLBACK_URL, params)
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == [
        ("error", "Your Stripe account couldn't be connected from this link. Please try again.")
    ]
    assert not requests_mock.called
    assert not ConnectedAccount.objects.exists()


def test_callback_not_started(connect_client, not_connected, requests_mock):
    # e.g. someone else's link: there's no state in this session
    resp = connect_client.get(CALLBACK_URL, {"code": "ac_123", "state": "their state"})
    assert messages_for(resp)[0][0] == "error"
    assert not requests_mock.called


def test_callback_cancelled(connect_client, not_connected, requests_mock):
    state = start_connecting(connect_client)
    resp = connect_client.get(
        CALLBACK_URL,
        {
            "error": "access_denied",
            "error_description": "The user denied your request",
            "state": state,
        },
    )
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == [
        ("error", "Your Stripe account wasn't connected: The user denied your request")
    ]
    # they can try again
    assert f'action="{AUTHORIZE_URL}"' in text(connect_client.get(CONNECT_URL))
    assert not requests_mock.called


def test_callback_token_error(connect_client, not_connected, requests_mock, caplog):
    requests_mock.post(
        TOKEN_URL,
        status_code=400,
        json={"error": "invalid_grant", "error_description": "Authorization code expired"},
    )
    state = start_connecting(connect_client)
    resp = connect_client.get(CALLBACK_URL, {"code": "ac_old", "state": state})
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == [
        ("error", "Sorry, there was a problem connecting your Stripe account. Please try again.")
    ]
    assert "Error connecting Stripe account" in caplog.text
    assert not ConnectedAccount.objects.exists()


@pytest.mark.parametrize("params", [{}, {"code": "ac_123", "state": "wrong"}])
def test_callback_back_button(connect_client, requests_mock, params):
    # e.g. going back to the callback page after connecting: no message, as the
    # connect page shows the connected account
    resp = connect_client.get(CALLBACK_URL, params)
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == []
    assert not requests_mock.called


def test_callback_already_connected(connect_client, not_connected, account, requests_mock):
    # e.g. connected in another tab meanwhile
    state = start_connecting(connect_client)
    ConnectedAccount(account_id=account, livemode=False).save()
    resp = connect_client.get(CALLBACK_URL, {"code": "ac_123", "state": state})
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == []
    assert not requests_mock.called
    assert ConnectedAccount.get_account_id() == account


# Disconnecting


def test_disconnect_page(connect_client, account):
    resp = connect_client.get(DISCONNECT_URL)
    assert resp.status_code == 200
    assert (
        f"Are you sure you want to disconnect the Stripe account <strong>{account}</strong>?"
    ) in text(resp)
    assert ConnectedAccount.objects.exists()


def test_disconnect(connect_client, account, requests_mock, deauthorized_signals, caplog):
    deauthorize = requests_mock.post(DEAUTHORIZE_URL, json={"stripe_user_id": account})
    resp = connect_client.post(DISCONNECT_URL)
    assert resp.url == CONNECT_URL
    assert messages_for(resp) == [
        ("success", f"The Stripe account {account} has been disconnected.")
    ]
    assert f'action="{AUTHORIZE_URL}"' in text(connect_client.get(CONNECT_URL))
    assert parse_qs(deauthorize.last_request.text) == {
        "client_id": ["ca_test"],
        "stripe_user_id": [account],
    }
    assert not ConnectedAccount.objects.exists()
    assert deauthorized_signals == [account]
    assert f"Stripe account {account} disconnected by connector" in caplog.text


def test_disconnect_already_disconnected(connect_client, account, requests_mock, caplog):
    # disconnected in Stripe, and the webhook didn't arrive
    requests_mock.post(
        DEAUTHORIZE_URL,
        status_code=401,
        json={
            "error": "invalid_client",
            "error_description": f"This application is not connected to stripe account {account}",
        },
    )
    connect_client.post(DISCONNECT_URL)
    assert not ConnectedAccount.objects.exists()
    assert f"Stripe account {account} was already disconnected" in caplog.text


def test_disconnect_error(connect_client, account, requests_mock, deauthorized_signals, caplog):
    # e.g. Stripe can't be reached
    requests_mock.post(DEAUTHORIZE_URL, exc=requests.exceptions.ConnectionError)
    resp = connect_client.post(DISCONNECT_URL)
    # back to the confirmation page, to try again
    assert resp.url == DISCONNECT_URL
    assert messages_for(resp) == [
        (
            "error",
            "Sorry, there was a problem disconnecting the Stripe account. Please try again.",
        )
    ]
    assert ConnectedAccount.objects.exists()
    assert deauthorized_signals == []
    assert f"Error disconnecting Stripe account {account}" in caplog.text


@pytest.mark.parametrize("method", ["get", "post"])
def test_disconnect_not_connected(connect_client, not_connected, requests_mock, method):
    resp = getattr(connect_client, method)(DISCONNECT_URL)
    assert resp.url == CONNECT_URL
    assert not requests_mock.called


@pytest.mark.parametrize("method", ["get", "post"])
def test_disconnect_not_allowed(connect_client, settings, requests_mock, method):
    settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT = False
    assert getattr(connect_client, method)(DISCONNECT_URL).status_code == 404
    assert ConnectedAccount.objects.exists()
    assert not requests_mock.called


def test_connected_account_single_row(not_connected):
    ConnectedAccount(account_id="acct_1", livemode=False).save()
    ConnectedAccount(account_id="acct_2", livemode=True).save()
    account = ConnectedAccount.objects.get()
    assert (account.pk, account.account_id, str(account)) == (1, "acct_2", "acct_2")
    account.delete()
    assert ConnectedAccount.get_account_id() is None
