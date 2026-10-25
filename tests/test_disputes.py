"""Disputed payments (chargebacks)"""
import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from salesman_stripe_connect import signals
from .utils import logged, mock_event, post_event

pytestmark = pytest.mark.django_db


def dispute(payment_intent="pi_1", status="needs_response", due_by=None):
    return Mock(
        id="dp_1",
        payment_intent=payment_intent,
        amount=2000,
        reason="product_not_received",
        status=status,
        evidence_details=Mock(due_by=due_by),
    )


@pytest.fixture
def dispute_signals():
    received = []

    def receiver(sender, signal, **kwargs):
        received.append((signal, kwargs))

    signals.dispute_created.connect(receiver)
    signals.dispute_closed.connect(receiver)
    yield received
    signals.dispute_created.disconnect(receiver)
    signals.dispute_closed.disconnect(receiver)


def test_dispute_created(client, paid_order, dispute_signals, caplog, settings):
    settings.TIME_ZONE = "Europe/London"
    due_by = int(datetime(2026, 10, 31, 23, 30, tzinfo=UTC).timestamp())
    event = mock_event("charge.dispute.created", dispute(due_by=due_by))
    resp = post_event(client, event)
    assert resp.content.decode() == "Dispute recorded"
    message = (
        "Payment pi_1 has been disputed by the customer (reason: product not "
        "received), for £20.00. Respond to the dispute in the Stripe dashboard by "
        "31 October 2026."
    )
    note = paid_order.notes.get()
    assert note.message == message
    assert not note.public
    assert message in caplog.text
    assert dispute_signals == [
        (
            signals.dispute_created,
            {"order": paid_order, "dispute": event.data.object, "message": message},
        )
    ]

    # a duplicate event isn't recorded again
    resp = post_event(client, event)
    assert resp.content.decode() == "Event already processed"
    assert paid_order.notes.count() == 1
    assert len(dispute_signals) == 1


def test_dispute_created_no_due_date(client, paid_order):
    post_event(client, mock_event("charge.dispute.created", dispute()))
    assert paid_order.notes.get().message.endswith(
        "Respond to the dispute in the Stripe dashboard."
    )


@pytest.mark.parametrize("event_type", ["charge.dispute.created", "charge.dispute.closed"])
def test_dispute_unknown_payment(client, event_type, caplog, dispute_signals):
    # e.g. a payment made outside the shop: logged as information, not an error
    caplog.set_level(logging.INFO)
    resp = post_event(client, mock_event(event_type, dispute("pi_unknown")))
    assert resp.content.decode() == "Event ignored: not a shop payment"
    (message,) = logged(caplog, logging.INFO)
    assert message.startswith("Dispute dp_1")
    assert message.endswith("payment pi_unknown, which isn't a shop payment")
    assert logged(caplog, logging.ERROR) == []
    assert dispute_signals == []


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("won", "won"),
        ("lost", "lost; £20.00 has been returned to the customer"),
        ("warning_closed", "warning closed"),
    ],
)
def test_dispute_closed(client, paid_order, dispute_signals, status, outcome):
    event = mock_event("charge.dispute.closed", dispute(status=status))
    resp = post_event(client, event)
    assert resp.content.decode() == "Dispute closed"
    message = f"The dispute for payment pi_1 was {outcome}."
    assert paid_order.notes.get().message == message
    assert dispute_signals == [
        (
            signals.dispute_closed,
            {"order": paid_order, "dispute": event.data.object, "message": message},
        )
    ]

    resp = post_event(client, event)
    assert resp.content.decode() == "Event already processed"
    assert len(dispute_signals) == 1
