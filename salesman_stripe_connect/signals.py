"""
Signals sent by StripeConnectPayment, for project-specific behaviour (e.g.
emails). They're sent inside the transaction that records the change, so
receivers sending emails should use transaction.on_commit.
"""
from django.dispatch import Signal

# A refund has been recorded on an order.
# Arguments: order, refund_id, amount (Decimal), fully_refunded (bool)
refund_recorded = Signal()

# A customer has disputed a payment for an order (a chargeback); the connected
# account needs to respond in its Stripe dashboard.
# Arguments: order, dispute (the Stripe dispute), message (as noted on the order)
dispute_created = Signal()

# A dispute has been closed (won or lost).
# Arguments: order, dispute, message
dispute_closed = Signal()

# The connected account has disconnected from the platform, so payments can't be
# made until an account is connected again.
# Arguments: account_id
account_deauthorized = Signal()
