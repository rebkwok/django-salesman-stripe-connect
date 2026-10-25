"""
A minimal shop for the tests: a product, and salesman's models with the app's
mixins
"""

from django.db import models
from salesman.basket.models import BaseBasket, BaseBasketItem
from salesman.orders.models import BaseOrder, BaseOrderItem, BaseOrderNote, BaseOrderPayment

from salesman_stripe_connect.mixins import OrderAmountsMixin, StripeBasketMixin


class Product(models.Model):
    name = models.CharField(max_length=128)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return self.name

    @property
    def code(self):
        return str(self.pk)

    def get_price(self, request):
        return self.price


class Basket(StripeBasketMixin, BaseBasket):
    pass


class BasketItem(BaseBasketItem):
    pass


class Order(OrderAmountsMixin, BaseOrder):
    pass


class OrderItem(BaseOrderItem):
    pass


class OrderPayment(BaseOrderPayment):
    pass


class OrderNote(BaseOrderNote):
    pass
