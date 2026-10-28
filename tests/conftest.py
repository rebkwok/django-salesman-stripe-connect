"""
The tests use the test project's salesman models (tests/testapp, configured in
tests/settings.py), with a connected Stripe account, and Stripe's API faked with
requests_mock.
"""
from decimal import Decimal

import pytest
from django.core import mail
from model_bakery import baker
from salesman.core.utils import get_salesman_model

from salesman_stripe_connect.models import ConnectedAccount
from salesman_stripe_connect.payment import StripeConnectPayment
from .utils import ACCOUNT, CUSTOMER_ID, CUSTOMERS_URL

Basket = get_salesman_model("Basket")
Order = get_salesman_model("Order")


@pytest.fixture(autouse=True)
def connected_account(request):
    """
    Tests that use the database have a connected Stripe account (ACCOUNT)
    """
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        yield None
        return
    transaction = marker.kwargs.get("transaction", False)
    request.getfixturevalue("transactional_db" if transaction else "db")
    account = ConnectedAccount(account_id=ACCOUNT, livemode=False)
    account.save()
    yield account


@pytest.fixture(autouse=True)
def stripe_customers(requests_mock):
    """
    Stripe's API is faked in every test (requests_mock: an unexpected request
    fails), with the customer for test@test.com: (list, create) mocks
    """
    listed = requests_mock.get(
        CUSTOMERS_URL,
        json={"object": "list", "data": [{"id": CUSTOMER_ID, "object": "customer"}], "has_more": False},
    )
    created = requests_mock.post(CUSTOMERS_URL, json={"id": "cus_new", "object": "customer"})
    yield listed, created


@pytest.fixture
def basket():
    """A basket with 2 x £10 of a product (£20)"""
    basket = baker.make(Basket, extra={"name": "Test User"})
    product = baker.make("testapp.Product", name="Test Product - Small", price=10)
    basket.add(product, quantity=2)
    basket.update(request=None)
    yield basket


@pytest.fixture
def order(basket):
    yield Order.objects.create_from_basket(basket, request=None)


@pytest.fixture
def account():
    return ACCOUNT


@pytest.fixture
def payment():
    return StripeConnectPayment()


@pytest.fixture
def checkout_basket(basket):
    # The fields added by the checkout when the basket goes to payment
    basket.extra.update(
        {
            "email": "test@test.com",
            "shipping_address": "-",
            "billing_address": "-",
            "payment_method": "stripe",
        }
    )
    basket.save()
    yield basket


@pytest.fixture
def paying_basket(checkout_basket):
    checkout_basket.start_payment("test-session", "https://stripe-session-url")
    yield checkout_basket


@pytest.fixture
def paid_order(order):
    # order total is £20
    order.status = Order.Status.PROCESSING
    order.email = "customer@test.com"
    order.save()
    order.pay(amount=Decimal("20.00"), transaction_id="pi_1", payment_method="stripe")
    mail.outbox.clear()
    yield order
