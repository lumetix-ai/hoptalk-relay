from django.http import HttpRequest
from django_htmx.middleware import HtmxDetails


class HtmxHttpRequest(HttpRequest):
    """The request type of panel views: HtmxMiddleware adds `htmx`, which HttpRequest does not declare."""

    htmx: HtmxDetails
