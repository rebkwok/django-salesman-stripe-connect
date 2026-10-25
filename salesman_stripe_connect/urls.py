from django.urls import path

from .connect_views import (
    authorize_view,
    connect_view,
    disconnect_view,
    oauth_callback_view,
)
from .views import webhook_view

urlpatterns = [
    path("webhook/", webhook_view, name="stripe-connect-webhook"),
    path("connect/", connect_view, name="stripe-connect"),
    path("connect/authorize/", authorize_view, name="stripe-connect-authorize"),
    path("connect/disconnect/", disconnect_view, name="stripe-connect-disconnect"),
    path("oauth/callback/", oauth_callback_view, name="stripe-connect-oauth-callback"),
]
