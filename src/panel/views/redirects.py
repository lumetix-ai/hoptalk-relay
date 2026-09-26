from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def redirect_to_next_page(request: HttpRequest, default_url_name: str) -> HttpResponseRedirect:
    """To the panel page a form named in its "next" field, which lets one action serve several pages."""
    next_path = request.POST.get("next", "")
    if next_path and url_has_allowed_host_and_scheme(
        next_path, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_path)
    return redirect(default_url_name)
