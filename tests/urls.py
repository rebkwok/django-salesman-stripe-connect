from django.urls import include, path
from salesman.checkout.payment import payment_methods_pool

urlpatterns = [
    # salesman's payment URLs: the success and cancel views
    path("api/", include(payment_methods_pool.get_urls())),
    # the webhook and the connect pages
    path("stripe/", include("salesman_stripe_connect.urls")),
]
