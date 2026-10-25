"""
Stripe Checkout payments for salesman, as direct charges on a Stripe Connect
account: the checkout sessions, payments, refunds and disputes all belong to
the connected account, and the platform's webhook endpoint listens to events
on connected accounts.

Webhook events handled:
- checkout.session.completed: the order is paid (fulfil_session)
- checkout.session.expired: an unpaid checkout expired (expire_basket_payment)
- charge.refunded: a full or partial refund, from the orders admin or the
  connected account's Stripe dashboard (handle_charge_refunded)
- charge.refund.updated: a refund failed or was cancelled (handle_refund_updated)
- charge.dispute.created, charge.dispute.closed: a customer disputed a payment
  (handle_dispute_created, handle_dispute_closed)
- account.application.authorized, account.application.deauthorized: an account
  connected to or disconnected from the platform (handle_account_authorized,
  handle_account_deauthorized)

The connected account is saved by the connect pages (see connect_views).
"""

import logging
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache

import stripe
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from salesman.basket.models import BASKET_ID_SESSION_KEY, BaseBasket
from salesman.checkout.payment import PaymentError, PaymentMethod
from salesman.core.utils import get_salesman_model

from . import signals
from .conf import app_settings
from .models import ConnectedAccount

# The Stripe API version the code is written for; webhook endpoints should use
# the same version
API_VERSION = "2026-09-30.endive"

CURRENCY_SYMBOLS = {"gbp": "£", "eur": "€", "usd": "$"}

# Webhook response for events about payments that weren't made through the shop
# (the connected account may take other payments)
NOT_SHOP_PAYMENT = "Event ignored: not a shop payment"

Basket = get_salesman_model("Basket")
Order = get_salesman_model("Order")
OrderNote = get_salesman_model("OrderNote")
OrderPayment = get_salesman_model("OrderPayment")

logger = logging.getLogger(__name__)


class StripeAccountNotConnected(stripe.StripeError):
    """No Stripe account is connected, so payments can't be made"""


@cache
def get_client(api_key):
    return stripe.StripeClient(api_key, stripe_version=API_VERSION)


def to_minor_units(amount):
    """Amount in pence (or cents)"""
    return int((Decimal(amount) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor_units(amount):
    return Decimal(amount) / 100


def _extra_rows(obj):
    """
    The label and amount of each extra row (added by basket modifiers) on a
    basket, order, or one of their items. Baskets and basket items have a dict
    of serializers; orders and order items have a list of dicts.
    """
    rows = obj.extra_rows
    if isinstance(rows, dict):
        rows = [row.data for row in rows.values()]
    return [(row["label"], Decimal(str(row["amount"]))) for row in rows]


class StripeConnectPayment(PaymentMethod):
    """
    Stripe Checkout payment method. Subclass it to add project-specific
    behaviour; the methods marked as hooks are there to be overridden.

    The Basket model should use mixins.StripeBasketMixin, and the Order model
    mixins.OrderAmountsMixin.
    """

    identifier = "stripe"
    label = app_settings.SALESMAN_STRIPE_CONNECT_PAYMENT_LABEL
    success_template_name = "salesman_stripe_connect/success.html"
    cancel_template_name = "salesman_stripe_connect/cancel.html"

    def get_urls(self):
        # Included by salesman under /payment/<identifier>/
        return [
            path("success/", self.success_view, name="stripe-connect-success"),
            path("cancel/", self.cancel_view, name="stripe-connect-cancel"),
        ]

    # Stripe API

    @classmethod
    def client(cls):
        return get_client(app_settings.SALESMAN_STRIPE_CONNECT_SECRET_KEY)

    @classmethod
    def get_connected_account(cls):
        """Id of the connected account that payments are made to, or None"""
        return ConnectedAccount.get_account_id()

    @classmethod
    def request_options(cls):
        """
        Options for Stripe API requests, which are made on the connected account.
        Raises StripeAccountNotConnected (a StripeError) if no account is
        connected.
        """
        account = cls.get_connected_account()
        if account is None:
            raise StripeAccountNotConnected("No Stripe account is connected")
        return {"stripe_account": account}

    @classmethod
    def retrieve_session(cls, session_id):
        return cls.client().v1.checkout.sessions.retrieve(
            session_id, options=cls.request_options()
        )

    # Amounts

    @classmethod
    def get_currency(cls):
        return app_settings.SALESMAN_STRIPE_CONNECT_CURRENCY

    @classmethod
    def format_amount(cls, amount):
        currency = cls.get_currency()
        symbol = CURRENCY_SYMBOLS.get(currency)
        if symbol:
            return f"{symbol}{amount:.2f}"
        return f"{amount:.2f} {currency.upper()}"

    # Starting a payment

    def basket_payment(self, basket, request):
        if basket.payment_in_progress:
            raise PaymentError("A payment is already in progress for this basket")
        return self.process_payment(basket, request)

    def order_payment(self, order, request):
        return self.process_payment(order, request)

    def process_payment(self, obj, request):
        """
        Create a checkout session; returns its URL. Raises PaymentError, with a
        message for the customer, if it can't be created.
        """
        try:
            session = self.client().v1.checkout.sessions.create(
                self.get_session_data(obj, request), options=self.request_options()
            )
        except StripeAccountNotConnected:
            logger.error("Stripe payment refused: no Stripe account is connected")
            raise PaymentError(
                "Sorry, card payments aren't available at the moment. Please try "
                "again later."
            )
        except stripe.StripeError:
            logger.exception(
                "Error creating Stripe checkout session for %s", self.get_reference(obj)
            )
            raise PaymentError(
                "Sorry, there was a problem starting your payment. Please try again."
            )
        self.session_created(obj, session, request)
        return session.url

    def session_created(self, obj, session, request):
        """
        Hook: the checkout session has been created. Records it on the basket,
        which locks the basket until the session completes, expires or is
        cancelled.
        """
        if isinstance(obj, BaseBasket):
            obj.start_payment(session.id, session.url)

    def get_session_data(self, obj, request):
        """
        Data for creating the checkout session, see
        https://docs.stripe.com/api/checkout/sessions/create
        """
        data = {
            "mode": "payment",
            "success_url": request.build_absolute_uri(reverse("stripe-connect-success"))
            + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": request.build_absolute_uri(reverse("stripe-connect-cancel")),
            "client_reference_id": self.get_reference(obj),
            "line_items": self.get_line_items(obj, request),
        }
        email = self.get_customer_email(obj)
        if email:
            data["customer_email"] = email
        expiry = self.get_session_expiry(obj)
        if expiry is not None:
            data["expires_at"] = int((timezone.now() + expiry).timestamp())
        return data

    def get_session_expiry(self, obj):
        """
        Hook: how long until the checkout session expires (a timedelta between
        30 minutes and 24 hours), or None for Stripe's default of 24 hours
        """
        return None

    def get_customer_email(self, obj):
        email = obj.extra.get("email") if isinstance(obj, BaseBasket) else obj.email
        if not email and obj.user is not None:
            email = obj.user.email
        return email or None

    def get_line_items(self, obj, request):
        """
        A line item for each item, and for each extra row on the whole basket
        or order (e.g. shipping).
        Stripe line items can't be negative, so if there's a negative row (e.g.
        a discount on the whole order), or the line items don't add up to the
        total, a single line item for the total is used instead.
        """
        currency = self.get_currency()
        line_items = []
        for item in obj.get_items():
            total, quantity = to_minor_units(item.total), item.quantity
            name = item.name
            if total % quantity:
                # e.g. a discount that doesn't divide equally between the items
                name, unit_amount, quantity = f"{quantity} x {name}", total, 1
            else:
                unit_amount = total // quantity
            product_data = {"name": name}
            labels = [label for label, _ in _extra_rows(item)]
            if labels:
                product_data["description"] = ", ".join(labels)
            line_items.append(self._line_item(currency, product_data, unit_amount, quantity))
        for label, amount in _extra_rows(obj):
            if amount:
                line_items.append(
                    self._line_item(currency, {"name": label}, to_minor_units(amount), 1)
                )

        amounts = [
            item["price_data"]["unit_amount"] * item["quantity"] for item in line_items
        ]
        if any(amount < 0 for amount in amounts) or sum(amounts) != to_minor_units(
            obj.total
        ):
            description = ", ".join(
                f"{item.quantity} x {item.name}" for item in obj.get_items()
            )
            return [
                self._line_item(
                    currency,
                    {"name": "Order total", "description": description},
                    to_minor_units(obj.total),
                    1,
                )
            ]
        return line_items

    @staticmethod
    def _line_item(currency, product_data, unit_amount, quantity):
        return {
            "price_data": {
                "currency": currency,
                "unit_amount": unit_amount,
                "product_data": product_data,
            },
            "quantity": quantity,
        }

    @classmethod
    def get_reference(cls, obj):
        """The checkout session's client_reference_id, identifying the basket or order"""
        if isinstance(obj, BaseBasket):
            return f"basket_{obj.id}"
        return f"order_{obj.id}"

    @classmethod
    def parse_reference(cls, reference):
        """Returns (kind, id), where kind is basket or order; or (None, None)"""
        kind, _, id = (reference or "").partition("_")
        if kind in ("basket", "order") and id.isdigit():
            return kind, int(id)
        return None, None

    # Completed payments

    @classmethod
    def fulfil_session(cls, request, session):
        """
        Create and pay the order for a completed checkout session.

        Called from the webhook, the success view and check_basket_payment, so
        it's safe to call more than once, including concurrently: the order
        records the session id, and whichever call comes first creates the
        order and payment; later calls return the existing order.

        A payment for an empty basket (e.g. its items expired) or a payment that
        doesn't match the order total is recorded on the order and flagged, so
        the shop can resolve it.

        Returns (order, message); order is None if it couldn't be fulfilled.
        """
        kind, id = cls.parse_reference(session.client_reference_id)
        if kind is None:
            # e.g. a payment link, or another integration on the same account
            logger.info(
                "Stripe session %s isn't a shop payment (reference %s)",
                session.id,
                session.client_reference_id,
            )
            return None, NOT_SHOP_PAYMENT

        with transaction.atomic():
            # Lock the basket or order so that concurrent calls for the same
            # session are processed one at a time
            if kind == "basket":
                basket = Basket.objects.select_for_update().filter(id=id).first()
            else:
                order = Order.objects.select_for_update().filter(id=id).first()

            existing_order = Order.objects.filter(
                _extra__stripe_session_id=session.id
            ).first()
            if existing_order is None:
                existing_order = cls.get_order_for_payment(session.payment_intent)
            if existing_order is not None:
                logger.info("Stripe session %s already fulfilled", session.id)
                return existing_order, "Order already fulfilled"

            if kind == "basket":
                if basket is None:
                    logger.error(
                        "Stripe payment %s received for missing basket %s",
                        session.payment_intent,
                        id,
                    )
                    return None, "Missing basket"
                basket_was_empty = not basket.items.exists()
                # basket.extra is copied to order.extra
                basket.extra.pop(basket.STRIPE_SESSION_KEY, None)
                basket.extra["stripe_session_id"] = session.id
                order = Order.objects.create_from_basket(
                    basket,
                    request,
                    status=app_settings.SALESMAN_STRIPE_CONNECT_PAID_STATUS,
                )
                basket.delete()
                if basket_was_empty:
                    cls.flag_order(
                        order,
                        f"Payment {session.payment_intent} was received but the "
                        "basket was empty (items may have expired). Check and refund "
                        "if necessary.",
                    )
            else:
                if order is None:
                    logger.error(
                        "Stripe payment %s received for missing order %s",
                        session.payment_intent,
                        id,
                    )
                    return None, "Missing order"
                order.extra["stripe_session_id"] = session.id
                order.save()

            amount = from_minor_units(session.amount_total)
            if amount != order.total:
                cls.flag_order(
                    order,
                    f"Payment {session.payment_intent} of {cls.format_amount(amount)} "
                    "doesn't match the order total of "
                    f"{cls.format_amount(order.total)}.",
                )
            order.pay(
                amount=amount,
                transaction_id=session.payment_intent,
                payment_method=cls.identifier,
            )

        logger.info("Order fulfilled: %s", order.ref)
        return order, "Order fulfilled"

    @classmethod
    def get_order_for_payment(cls, payment_intent):
        """The order paid by the payment intent, or None"""
        payment = (
            OrderPayment.objects.filter(transaction_id=payment_intent, amount__gt=0)
            .select_related("order")
            .first()
        )
        return payment.order if payment else None

    # Basket payments that expire or are cancelled

    @classmethod
    def expire_basket_payment(cls, session):
        """
        The checkout session has expired: if its basket is still waiting for it,
        unlock the basket. Returns (basket or None, message).
        """
        kind, id = cls.parse_reference(session.client_reference_id)
        if kind != "basket":
            return None, "Event ignored"
        with transaction.atomic():
            basket = Basket.objects.select_for_update().filter(id=id).first()
            if basket is None or basket.stripe_session_id != session.id:
                # The basket has been fulfilled or deleted, or its payment cancelled
                return None, "Event ignored"
            cls.basket_payment_expired(basket)
            basket.end_payment()
        logger.info("Stripe session %s expired for basket %s", session.id, id)
        return basket, "Payment expired"

    @classmethod
    def basket_payment_expired(cls, basket):
        """
        Hook: the basket's checkout session has expired; called before the
        basket is unlocked (and saved)
        """

    @classmethod
    def cancel_basket_payment(cls, basket):
        """
        Expire the basket's checkout session (e.g. the customer wants to change
        their basket) and unlock the basket.
        Returns False if the session has already completed, so can't be cancelled.
        Raises StripeError if the session can't be expired or retrieved.
        """
        with transaction.atomic():
            # Lock the basket, so the expired webhook waits until it's unlocked
            basket = Basket.objects.select_for_update().get(id=basket.id)
            if not basket.payment_in_progress:
                return True
            try:
                cls.client().v1.checkout.sessions.expire(
                    basket.stripe_session_id, options=cls.request_options()
                )
            except stripe.InvalidRequestError:
                # The session isn't open; it has either expired or completed
                session = cls.retrieve_session(basket.stripe_session_id)
                if session.status == "complete":
                    return False
            basket.end_payment()
            cls.basket_payment_cancelled(basket)
        return True

    @classmethod
    def basket_payment_cancelled(cls, basket):
        """Hook: the basket's payment has been cancelled, and the basket unlocked"""

    @classmethod
    def check_basket_payment(cls, request, basket):
        """
        Check the status of the basket's checkout session (e.g. when the
        customer returns to their basket) rather than waiting for a webhook.
        Fulfils the order if the session has been paid, or unlocks the basket if
        it has expired.
        Returns the order if the session has been paid, otherwise None.
        """
        try:
            session = cls.retrieve_session(basket.stripe_session_id)
        except stripe.StripeError:
            logger.exception(
                "Error retrieving Stripe session %s", basket.stripe_session_id
            )
            return None
        if session.status == "complete" and session.payment_status == "paid":
            order, _ = cls.fulfil_session(request, session)
            return order
        if session.status == "expired":
            cls.expire_basket_payment(session)
        return None

    # Refunds

    def refund_payment(self, payment):
        """Fully refund an order payment (salesman's refund interface)"""
        _, error = self._refund(payment)
        return error is None

    @classmethod
    def refund_order(cls, order):
        """
        Fully refund the order's Stripe payments. To refund part of an order,
        refund it in the connected account's Stripe dashboard; the refund is
        recorded by the charge.refunded webhook.
        Returns (amount refunded, list of error messages).
        """
        refunded, errors = Decimal(0), []
        payments = order.payments.filter(payment_method=cls.identifier, amount__gt=0)
        for payment in payments:
            amount, error = cls._refund(payment)
            if error is not None:
                errors.append(error)
            else:
                refunded += amount
        return refunded, errors

    @classmethod
    def _refund(cls, payment):
        """
        Refund the payment in Stripe and record the refund.
        Returns (amount refunded, None) or (None, error message).
        """
        order = payment.order
        try:
            refund = cls.client().v1.refunds.create(
                {"payment_intent": payment.transaction_id, "metadata": {"order": order.ref}},
                options=cls.request_options(),
            )
        except stripe.StripeError as e:
            logger.exception(
                "Error refunding payment %s for order %s",
                payment.transaction_id,
                order.ref,
            )
            return None, f"{payment.transaction_id}: {e.user_message or e}"
        amount = from_minor_units(refund.amount)
        cls.record_refund(order, refund.id, amount)
        return amount, None

    @classmethod
    def record_refund(cls, order, refund_id, amount):
        """
        Record a refund on the order (once; a refund made from the orders admin
        is also received by the webhook), as a payment with a negative amount.
        A full refund sets the order to Refunded.
        Returns True if the refund was recorded, False if it already had been.
        """
        with transaction.atomic():
            # Lock the order so concurrent webhooks record each refund once
            order = Order.objects.select_for_update().get(pk=order.pk)
            if order.payments.filter(transaction_id=refund_id).exists():
                return False
            OrderPayment.objects.create(
                order=order,
                amount=-amount,
                transaction_id=refund_id,
                payment_method=cls.identifier,
            )
            fully_refunded = order.amount_refunded >= order.amount_received
            OrderNote.objects.create(
                order=order,
                message=f"Refund {refund_id} of {cls.format_amount(amount)}"
                + ("" if fully_refunded else " (partial refund)"),
                public=False,
            )
            if fully_refunded and order.status != order.Status.REFUNDED:
                order.status = order.Status.REFUNDED
                order.save()
            signals.refund_recorded.send(
                sender=cls,
                order=order,
                refund_id=refund_id,
                amount=amount,
                fully_refunded=fully_refunded,
            )
        logger.info("Order %s: refund %s of %s recorded", order.ref, refund_id, amount)
        return True

    @classmethod
    def handle_charge_refunded(cls, charge):
        """
        A charge has been (fully or partly) refunded: record any refunds that
        aren't on the order yet.
        """
        order = cls.get_order_for_payment(charge.payment_intent)
        if order is None:
            logger.info(
                "Refund received for payment %s, which isn't a shop payment",
                charge.payment_intent,
            )
            return NOT_SHOP_PAYMENT
        # The charge's refunds aren't included in the event, so fetch them
        refunds = cls.client().v1.refunds.list(
            {"payment_intent": charge.payment_intent, "limit": 100},
            options=cls.request_options(),
        )
        recorded = sum(
            cls.record_refund(order, refund.id, from_minor_units(refund.amount))
            for refund in refunds.auto_paging_iter()
            if refund.status in ("succeeded", "pending")
        )
        return f"{recorded} refund(s) recorded"

    @classmethod
    def handle_refund_updated(cls, refund):
        """
        A refund has failed or been cancelled after it was created: remove it
        from the order, and flag the order.
        """
        if refund.status not in ("failed", "canceled"):
            return "Event ignored"
        refund_payment = (
            OrderPayment.objects.filter(transaction_id=refund.id)
            .select_related("order")
            .first()
        )
        if refund_payment is None:
            return "Event ignored"
        order = refund_payment.order
        refund_payment.delete()
        cls.flag_order(
            order,
            f"Refund {refund.id} of {cls.format_amount(from_minor_units(refund.amount))} "
            f"{refund.status}; it has been removed from the order's payments. "
            "Check the order status.",
        )
        return "Refund removed"

    # Disputes

    @classmethod
    def handle_dispute_created(cls, dispute):
        """
        A customer has disputed a payment: flag the order. The connected account
        has to respond in its Stripe dashboard.
        """
        order = cls.get_order_for_payment(dispute.payment_intent)
        if order is None:
            logger.info(
                "Dispute %s received for payment %s, which isn't a shop payment",
                dispute.id,
                dispute.payment_intent,
            )
            return NOT_SHOP_PAYMENT
        message = (
            f"Payment {dispute.payment_intent} has been disputed by the customer "
            f"(reason: {dispute.reason.replace('_', ' ')}), for "
            f"{cls.format_amount(from_minor_units(dispute.amount))}. "
            "Respond to the dispute in the Stripe dashboard"
        )
        due_by = dispute.evidence_details.due_by
        if due_by:
            due_date = timezone.localtime(datetime.fromtimestamp(due_by, UTC))
            message += f" by {due_date:%-d %B %Y}"
        message += "."
        if not cls.add_note_once(order, message):
            return "Event already processed"
        logger.error("Order %s: %s", order.ref, message)
        signals.dispute_created.send(
            sender=cls, order=order, dispute=dispute, message=message
        )
        return "Dispute recorded"

    @classmethod
    def handle_dispute_closed(cls, dispute):
        """A dispute has been closed: note the outcome on the order"""
        order = cls.get_order_for_payment(dispute.payment_intent)
        if order is None:
            logger.info(
                "Dispute %s closed for payment %s, which isn't a shop payment",
                dispute.id,
                dispute.payment_intent,
            )
            return NOT_SHOP_PAYMENT
        outcome = {
            "won": "won",
            "lost": (
                f"lost; {cls.format_amount(from_minor_units(dispute.amount))} has "
                "been returned to the customer"
            ),
        }.get(dispute.status, dispute.status.replace("_", " "))
        message = f"The dispute for payment {dispute.payment_intent} was {outcome}."
        if not cls.add_note_once(order, message):
            return "Event already processed"
        logger.info("Order %s: %s", order.ref, message)
        signals.dispute_closed.send(
            sender=cls, order=order, dispute=dispute, message=message
        )
        return "Dispute closed"

    # The connected account

    @classmethod
    def handle_account_authorized(cls, account):
        """An account has connected to the platform (e.g. by the connect pages)"""
        logger.info("Stripe account %s has connected to the platform", account)
        return "Account authorized"

    @classmethod
    def handle_account_deauthorized(cls, account):
        """
        An account has disconnected from the platform. If it's the connected
        account, forget it; payments can't be made until an account is
        connected again.
        """
        with transaction.atomic():
            deleted, _ = ConnectedAccount.objects.filter(account_id=account).delete()
            if not deleted:
                return "Event ignored"
            signals.account_deauthorized.send(sender=cls, account_id=account)
        logger.error(
            "Stripe account %s has disconnected from the platform; Stripe payments "
            "can't be made until an account is connected again",
            account,
        )
        return "Account deauthorized"

    # Orders

    @classmethod
    def flag_order(cls, order, message):
        """Add a (non-public) note to the order about a problem, and log it"""
        OrderNote.objects.create(order=order, message=message, public=False)
        logger.error("Order %s: %s", order.ref, message)

    @classmethod
    def add_note_once(cls, order, message):
        """
        Add a (non-public) note to the order, unless it already has it (e.g. a
        duplicate webhook event). Returns True if it was added.
        """
        _, created = OrderNote.objects.get_or_create(
            order=order, message=message, defaults={"public": False}
        )
        return created

    # Webhook

    @classmethod
    def webhook(cls, request):
        """Handle a webhook request, after checking its signature"""
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                request.headers.get("Stripe-Signature"),
                app_settings.SALESMAN_STRIPE_CONNECT_WEBHOOK_SECRET,
            )
        except ValueError:
            logger.exception("Invalid Stripe webhook payload")
            return HttpResponseBadRequest("Invalid payload")
        except stripe.SignatureVerificationError:
            logger.exception("Invalid Stripe webhook signature")
            return HttpResponseBadRequest("Invalid signature")
        return HttpResponse(cls.handle_webhook_event(request, event))

    @classmethod
    def handle_webhook_event(cls, request, event):
        """
        Handle a webhook event; returns a message for the response.
        Events from any account other than the connected account (including the
        platform's own events) are ignored.
        Unrecoverable problems (e.g. a missing basket) are logged, and the
        response is still a 200, so Stripe doesn't keep retrying.
        """
        account = getattr(event, "account", None)
        # Events about connecting accounts can be for accounts other than the
        # connected one (or there may be no connected account)
        if event.type == "account.application.authorized":
            return cls.handle_account_authorized(account)
        if event.type == "account.application.deauthorized":
            return cls.handle_account_deauthorized(account)
        if account is None or account != cls.get_connected_account():
            # e.g. another site's connected account, if the platform is shared
            logger.info("Ignoring Stripe %s event from account %s", event.type, account)
            return "Event ignored"
        obj = event.data.object
        match event.type:
            case "checkout.session.completed":
                _, message = cls.fulfil_session(request, obj)
            case "checkout.session.expired":
                _, message = cls.expire_basket_payment(obj)
            case "charge.refunded":
                message = cls.handle_charge_refunded(obj)
            case "charge.refund.updated":
                message = cls.handle_refund_updated(obj)
            case "charge.dispute.created":
                message = cls.handle_dispute_created(obj)
            case "charge.dispute.closed":
                message = cls.handle_dispute_closed(obj)
            case _:
                message = "Event ignored"
        return message

    # Views

    @classmethod
    def get_request_basket(cls, request):
        """
        Hook: the request's basket, or None. Unlike salesman's
        Basket.objects.get_or_create_from_request, this doesn't create one.
        """
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return Basket.objects.filter(user=user).first()
        basket_id = request.session.get(BASKET_ID_SESSION_KEY)
        if basket_id is None:
            return None
        return Basket.objects.filter(id=basket_id, user=None).first()

    @classmethod
    def get_view_context(cls, request):
        """Hook: extra context for the success and cancel templates"""
        return {}

    @classmethod
    def cancel_view(cls, request):
        """
        The customer cancelled the payment on Stripe; unlock the basket so they
        can change it.
        """
        basket = cls.get_request_basket(request)
        if basket is not None and basket.payment_in_progress:
            try:
                cls.cancel_basket_payment(basket)
            except stripe.StripeError:
                logger.exception(
                    "Error cancelling Stripe session %s", basket.stripe_session_id
                )
        return render(request, cls.cancel_template_name, cls.get_view_context(request))

    @classmethod
    def success_view(cls, request):
        """
        The customer has paid. Fulfil the order here as well as in the webhook,
        so the customer sees their order (and an empty basket) without waiting
        for the webhook. If anything goes wrong, the webhook will still fulfil
        the order.
        """
        context = cls.get_view_context(request)
        session_id = request.GET.get("session_id")
        session = None
        if session_id:
            try:
                session = cls.retrieve_session(session_id)
            except stripe.StripeError:
                logger.exception("Error retrieving Stripe session %s", session_id)

        if session is not None:
            context["email"] = session.customer_details.email
            context["total"] = from_minor_units(session.amount_total)
            if session.payment_status == "paid":
                try:
                    context["order"], _ = cls.fulfil_session(request, session)
                except Exception:
                    logger.exception(
                        "Error fulfilling Stripe session %s from success view",
                        session.id,
                    )
        return render(request, cls.success_template_name, context)
