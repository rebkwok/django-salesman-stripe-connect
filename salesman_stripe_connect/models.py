from django.db import models
from django.db.models import Q
from django.utils import timezone


class ConnectedAccount(models.Model):
    """
    The connected Stripe account that payments are made to. There's at most
    one, connected through the connect pages (see connect_views), by users with
    the connect_stripe_account permission.
    """

    account_id = models.CharField(max_length=255)
    livemode = models.BooleanField(help_text="Connected with live (not test) keys")
    date_connected = models.DateTimeField(default=timezone.now)

    class Meta:
        permissions = [
            ("connect_stripe_account", "Can connect or disconnect the Stripe account")
        ]
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="single_connected_account")
        ]

    def __str__(self):
        return self.account_id

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_account_id(cls):
        """The connected account's id, or None if no account is connected"""
        account = cls.objects.filter(pk=1).first()
        return account.account_id if account else None
