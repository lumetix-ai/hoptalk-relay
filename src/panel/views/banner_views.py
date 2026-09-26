from dataclasses import dataclass

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from node.worker_status import Banner, BannerKind, collect_banners


@dataclass(frozen=True, kw_only=True)
class BannerPresentation:
    tone: str
    icon_name: str
    link_url_name: str = ""
    link_label: str = ""


BANNER_PRESENTATIONS = {
    BannerKind.NODE_CONFIGURATION_INCOMPLETE: BannerPresentation(
        tone="danger", icon_name="exclamation-triangle", link_url_name="panel:setup", link_label="Open setup"
    ),
    BannerKind.WORKER_OFFLINE: BannerPresentation(tone="danger", icon_name="signal-slash"),
    BannerKind.RELAY_DISCONNECTED: BannerPresentation(tone="danger", icon_name="link-slash"),
    BannerKind.IDENTITY_MISMATCH: BannerPresentation(
        tone="danger", icon_name="finger-print", link_url_name="panel:setup", link_label="Set up this node"
    ),
    BannerKind.SETUP_REQUIRED: BannerPresentation(
        tone="warning", icon_name="wrench-screwdriver", link_url_name="panel:setup", link_label="Start setup"
    ),
    BannerKind.SETUP_IN_PROGRESS: BannerPresentation(
        tone="information", icon_name="wrench-screwdriver", link_url_name="panel:setup", link_label="Continue setup"
    ),
    BannerKind.SETTINGS_DRIFT: BannerPresentation(
        tone="warning", icon_name="adjustments-horizontal", link_url_name="panel:node_dashboard", link_label="Review"
    ),
    BannerKind.FIRMWARE_PROTOCOL_MISMATCH: BannerPresentation(tone="warning", icon_name="cpu-chip"),
    BannerKind.CONTACT_SYNC_PROBLEM: BannerPresentation(
        tone="warning", icon_name="identification", link_url_name="panel:contacts", link_label="Open contacts"
    ),
}


@dataclass(frozen=True, kw_only=True)
class DisplayedBanner:
    title: str
    detail: str
    tone: str
    icon_name: str
    link_url: str
    link_label: str


def present_banner(banner: Banner) -> DisplayedBanner:
    presentation = BANNER_PRESENTATIONS[banner.kind]
    return DisplayedBanner(
        title=banner.title,
        detail=banner.detail,
        tone=presentation.tone,
        icon_name=presentation.icon_name,
        link_url=reverse(presentation.link_url_name) if presentation.link_url_name else "",
        link_label=presentation.link_label,
    )


@require_GET
def show_banners(request: HttpRequest) -> HttpResponse:
    displayed_banners = [present_banner(banner) for banner in collect_banners(timezone.now())]
    return render(request, "panel/banners.html", {"banners": displayed_banners})
