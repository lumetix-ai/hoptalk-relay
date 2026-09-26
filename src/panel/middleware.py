from collections.abc import Callable
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers

from panel.operator_authentication import has_valid_operator_session

# Everything else needs a signed-in operator.
PUBLIC_PATHS = frozenset({"/login"})
PUBLIC_PATH_PREFIXES = ("/static/",)


class OperatorAuthenticationMiddleware:
    """Sends every request without a valid operator session to the sign-in page.

    A session is valid when it was signed in less than 12 hours ago and its fingerprint matches
    the current credentials in src/.env. An HTMX request cannot follow a redirect to a full page,
    so it gets status 200 with HX-Redirect instead. Every response behind the session is marked
    uncacheable: the pages show every user's messages in clear text, and a browser must not keep
    them for the Back button after sign-out.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if is_public_path(request.path):
            return self.get_response(request)

        if has_valid_operator_session(request.session, timezone.now()):
            response = self.get_response(request)
            add_never_cache_headers(response)
            return response

        login_url = reverse("panel:login")
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(status=200, headers={"HX-Redirect": login_url})

        return HttpResponseRedirect(f"{login_url}?{urlencode({'next': request.get_full_path()})}")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)
