from decimal import Decimal


class StripeBasketMixin:
    """
    For the salesman Basket model: records the basket's Stripe checkout session
    while a payment is in progress.

    The app doesn't stop the basket being changed during a payment (it only
    refuses to start a second payment); the project must. While
    payment_in_progress is True, the project's views should refuse to add,
    update or remove items and to start a checkout, and salesman's basket and
    checkout API shouldn't be exposed, as it would bypass those checks. When
    the customer returns to the basket, check_basket_payment fulfils or unlocks
    it, and cancel_basket_payment lets them cancel the payment to change it.
    Otherwise the order could be created with different items from the ones
    paid for.
    """

    # Key in extra for the checkout session while a payment is in progress
    STRIPE_SESSION_KEY = "stripe_session"

    @property
    def payment_in_progress(self):
        """
        A checkout session has been started for this basket and hasn't
        completed, expired or been cancelled
        """
        return self.STRIPE_SESSION_KEY in self.extra

    @property
    def stripe_session_id(self):
        return self.extra.get(self.STRIPE_SESSION_KEY, {}).get("id")

    @property
    def stripe_session_url(self):
        return self.extra.get(self.STRIPE_SESSION_KEY, {}).get("url")

    def start_payment(self, session_id, session_url):
        self.extra[self.STRIPE_SESSION_KEY] = {"id": session_id, "url": session_url}
        self.save()

    def end_payment(self):
        """The checkout session has expired or been cancelled"""
        self.extra.pop(self.STRIPE_SESSION_KEY, None)
        self.save()


class OrderAmountsMixin:
    """
    For the salesman Order model. Refunds are recorded as payments with
    negative amounts, so salesman's amount_paid is the amount received less any
    refunds.
    """

    @property
    def amount_received(self):
        """Total of payments received, not counting refunds"""
        return sum(
            (payment.amount for payment in self.payments.all() if payment.amount > 0),
            Decimal(0),
        )

    @property
    def amount_refunded(self):
        return -sum(
            (payment.amount for payment in self.payments.all() if payment.amount < 0),
            Decimal(0),
        )

    @property
    def amount_due(self):
        """Amount still to be paid (refunds don't make an order due again)"""
        return max(self.total - self.amount_received, Decimal(0))

    # Salesman's amount_outstanding and is_paid use amount_paid, so a refund
    # would make the order unpaid with the refund outstanding

    @property
    def amount_outstanding(self):
        return self.amount_due

    @property
    def is_paid(self):
        return self.amount_received >= self.total

    @property
    def refund_status(self):
        """'Fully refunded', 'Partially refunded', or '' if there are no refunds"""
        if not self.amount_refunded:
            return ""
        if self.amount_refunded >= self.amount_received:
            return "Fully refunded"
        return "Partially refunded"
