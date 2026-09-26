from django.db import models


class OperatorLoginAttempt(models.Model):
    """One sign-in attempt on the admin panel, the record behind the per-address throttle."""

    # From X-Forwarded-For, which nginx sets to the connecting address, replacing whatever
    # the client sent; gunicorn listens on a Unix socket, so only nginx can connect.
    client_address = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField()
    attempted_at = models.DateTimeField()

    class Meta:
        db_table = "operator_login_attempts"
        indexes = [
            models.Index(fields=["client_address", "attempted_at"], name="login_attempt_address_time"),
        ]

    def __str__(self) -> str:
        outcome = "succeeded" if self.succeeded else "failed"
        return f"sign-in from {self.client_address or 'an unknown address'} {outcome}"
