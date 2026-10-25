"""
Pages for connecting (and disconnecting) the Stripe account that payments are
made to, with Stripe Connect OAuth
(https://docs.stripe.com/connect/oauth-standard-accounts).

They're for logged-in users with the connect_stripe_account permission
(superusers have it). Results and errors are shown with Django's messages
framework, so they use the site's styling. The pages can be disabled entirely
(SALESMAN_STRIPE_CONNECT_PAGES_ENABLED = False), e.g. once the account is
connected, and the disconnect page can be removed
(SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT = False). Once an account is
connected, another can't be connected until it's disconnected.
"""

import hmac
import logging
import secrets
from functools import wraps

import stripe
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import signals
from .conf import app_settings
from .models import ConnectedAccount
from .payment import get_client

logger = logging.getLogger(__name__)

PERMISSION = "salesman_stripe_connect.connect_stripe_account"

# Session key for the OAuth state, which the callback checks so that it only
# accepts a code from a connection started by the connect page
STATE_SESSION_KEY = "salesman_stripe_connect_state"


def connect_page(view):
    """
    The connect pages: 404 if they're disabled, otherwise for logged-in users
    with the connect_stripe_account permission
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not app_settings.SALESMAN_STRIPE_CONNECT_PAGES_ENABLED:
            raise Http404
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def client():
    return get_client(app_settings.SALESMAN_STRIPE_CONNECT_SECRET_KEY)


@require_GET
@connect_page
def connect_view(request):
    """Shows the connected account, or a button to connect one"""
    return render(
        request,
        "salesman_stripe_connect/connect.html",
        {
            "account": ConnectedAccount.objects.first(),
            "allow_disconnect": app_settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT,
        },
    )


@require_POST
@connect_page
def authorize_view(request):
    """Sends the user to Stripe to connect their account"""
    if ConnectedAccount.objects.exists():
        return redirect("stripe-connect")
    state = secrets.token_urlsafe(32)
    request.session[STATE_SESSION_KEY] = state
    url = client().oauth.authorize_url(
        {
            "client_id": app_settings.SALESMAN_STRIPE_CONNECT_CLIENT_ID,
            "response_type": "code",
            "scope": "read_write",
            "redirect_uri": request.build_absolute_uri(
                reverse("stripe-connect-oauth-callback")
            ),
            "state": state,
        }
    )
    return redirect(url)


@require_GET
@connect_page
def oauth_callback_view(request):
    """
    Stripe redirects here after the user connects their account (or cancels).
    The code is exchanged for the account id, which is saved. Redirects to the
    connect page.
    """
    state = request.session.pop(STATE_SESSION_KEY, None)
    if ConnectedAccount.objects.exists():
        # e.g. going back to this page after connecting; the connect page shows
        # the connected account
        pass
    elif not state or not hmac.compare_digest(
        state.encode(), request.GET.get("state", "").encode()
    ):
        logger.warning("Stripe connect callback with an invalid state")
        messages.error(
            request,
            "Your Stripe account couldn't be connected from this link. Please try "
            "again.",
        )
    elif "error" in request.GET:
        description = request.GET.get("error_description", request.GET["error"])
        logger.info("Stripe account not connected: %s", description)
        messages.error(request, f"Your Stripe account wasn't connected: {description}")
    else:
        _connect(request)
    return redirect("stripe-connect")


def _connect(request):
    """Exchange the callback's code for the account id, and save it"""
    try:
        token = client().oauth.token(
            {"grant_type": "authorization_code", "code": request.GET.get("code", "")}
        )
    except stripe.StripeError:
        logger.exception("Error connecting Stripe account")
        messages.error(
            request,
            "Sorry, there was a problem connecting your Stripe account. Please try again.",
        )
        return
    ConnectedAccount(account_id=token.stripe_user_id, livemode=token.livemode).save()
    logger.info("Stripe account %s connected by %s", token.stripe_user_id, request.user)
    messages.success(
        request, "Your Stripe account has been connected. Card payments will now be made to it."
    )


@require_http_methods(["GET", "POST"])
@connect_page
def disconnect_view(request):
    """
    Asks for confirmation, then disconnects the account from the platform (so
    another can be connected). Payments can't be made until one is.
    """
    if not app_settings.SALESMAN_STRIPE_CONNECT_ALLOW_DISCONNECT:
        raise Http404
    account = ConnectedAccount.objects.first()
    if account is None:
        return redirect("stripe-connect")
    if request.method == "GET":
        return render(
            request, "salesman_stripe_connect/disconnect.html", {"account": account}
        )

    try:
        client().oauth.deauthorize(
            {
                "client_id": app_settings.SALESMAN_STRIPE_CONNECT_CLIENT_ID,
                "stripe_user_id": account.account_id,
            }
        )
    except stripe.oauth_error.InvalidClientError:
        # The account isn't connected to the platform any more (e.g. it was
        # disconnected in Stripe and the webhook didn't arrive)
        logger.warning(
            "Stripe account %s was already disconnected from the platform",
            account.account_id,
        )
    except stripe.StripeError:
        logger.exception("Error disconnecting Stripe account %s", account.account_id)
        messages.error(
            request,
            "Sorry, there was a problem disconnecting the Stripe account. Please try "
            "again.",
        )
        return redirect("stripe-connect-disconnect")
    with transaction.atomic():
        account.delete()
        signals.account_deauthorized.send(
            sender=disconnect_view, account_id=account.account_id
        )
    logger.warning(
        "Stripe account %s disconnected by %s", account.account_id, request.user
    )
    messages.success(
        request, f"The Stripe account {account.account_id} has been disconnected."
    )
    return redirect("stripe-connect")
