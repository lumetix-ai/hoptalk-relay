"""The Users page: every user with its devices, and deleting users and devices."""

from django.contrib import messages
from django.db.models import Count, Prefetch
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from directory.models import Contact, User
from directory.users import delete_user as delete_user_with_its_rows
from directory.users import summarize_user_deletion
from panel.forms import UserDeletionForm

USERS_TEMPLATE = "panel/users.html"


@require_GET
def show_users(request: HttpRequest) -> HttpResponse:
    users = (
        User.objects.annotate(
            device_count=Count("devices", distinct=True),
            sent_message_count=Count("sent_messages", distinct=True),
            received_message_count=Count("received_messages", distinct=True),
        )
        .prefetch_related(Prefetch("devices", queryset=Contact.objects.order_by("linked_at", "id")))
        .order_by("username_lookup")
    )
    return render(request, USERS_TEMPLATE, {"users": users, "user_deletion_form": UserDeletionForm()})


@require_GET
def show_user_deletion_summary(request: HttpRequest, user_id: int) -> HttpResponse:
    user = get_object_or_404(User, id=user_id)
    return render(
        request, f"{USERS_TEMPLATE}#user_deletion_summary", {"deletion_summary": summarize_user_deletion(user)}
    )


@require_POST
def delete_user(request: HttpRequest, user_id: int) -> HttpResponse:
    user = get_object_or_404(User, id=user_id)
    user_deletion_form = UserDeletionForm(request.POST)
    if not user_deletion_form.is_valid() or user_deletion_form.cleaned_data["typed_username"] != user.username:
        messages.error(request, f"@{user.username} was not deleted: type the username exactly as shown to confirm.")
        return redirect("panel:users")

    delete_user_with_its_rows(user)
    messages.success(request, f"@{user.username} and everything that belonged to it were deleted.")
    return redirect("panel:users")
