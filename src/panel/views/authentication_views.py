from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from panel.forms import OperatorLoginForm
from panel.operator_authentication import (
    calculate_throttle_minutes_remaining,
    find_client_address,
    has_valid_operator_session,
    record_login_attempt,
    sign_operator_in,
    sign_operator_out,
    verify_operator_credentials,
)

WRONG_CREDENTIALS_MESSAGE = "Wrong username or password."
LOGIN_TEMPLATE = "panel/login.html"


@never_cache
@require_http_methods(["GET", "POST"])
def sign_in(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    if has_valid_operator_session(request.session, now):
        return redirect_after_sign_in(request)

    if request.method != "POST":
        return render(request, LOGIN_TEMPLATE, {"form": OperatorLoginForm()})

    login_form = OperatorLoginForm(request.POST)
    client_address = find_client_address(request)

    throttle_minutes_remaining = calculate_throttle_minutes_remaining(client_address, now)
    if throttle_minutes_remaining:
        login_form.add_error(None, f"Too many attempts, try again in {throttle_minutes_remaining} minutes.")
        return render(request, LOGIN_TEMPLATE, {"form": login_form}, status=429)

    credentials_are_valid = login_form.is_valid() and verify_operator_credentials(
        login_form.cleaned_data["username"], login_form.cleaned_data["password"]
    )
    record_login_attempt(client_address, succeeded=credentials_are_valid, now=now)

    if not credentials_are_valid:
        login_form.add_error(None, WRONG_CREDENTIALS_MESSAGE)
        return render(request, LOGIN_TEMPLATE, {"form": login_form})

    sign_operator_in(request.session, now)
    return redirect_after_sign_in(request)


@require_POST
def sign_out(request: HttpRequest) -> HttpResponse:
    sign_operator_out(request.session)
    sign_out_response = redirect("panel:login")
    # A browser may keep a page for its Back button despite no-store; this empties that cache too.
    sign_out_response["Clear-Site-Data"] = '"cache"'
    return sign_out_response


def redirect_after_sign_in(request: HttpRequest) -> HttpResponseRedirect:
    next_path = request.GET.get("next", "")
    if next_path and url_has_allowed_host_and_scheme(
        next_path,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(next_path)
    return HttpResponseRedirect("/")
