// The regions of the admin panel that htmx polls every few seconds.
//
// A poll replaces its whole region, which closes whatever the operator opened inside it. So a
// region is not polled while a dialog is open in it, or a row is expanded in it (a <details>);
// the next poll after it closes catches up. A refresh that a change triggered, rather than the
// timer, still replaces a region with an expanded row, because nothing would repeat it later.
//
// The banners are polled as well, and they are announced by screen readers whenever they are
// inserted. Their region is therefore swapped only when the server's answer changed, so that a
// banner is announced once and not every five seconds.
"use strict";

(function () {
    const BANNER_REGION_ID = "banner-region";

    function isRefreshOfPolledRegion(requestConfig) {
        return Boolean(requestConfig) && requestConfig.verb === "get";
    }

    function isTimedPoll(requestConfig) {
        // htmx passes no event to the requests its "every" trigger starts.
        return !requestConfig.triggeringEvent;
    }

    function shouldKeepRegionAsItIs(region, requestConfig) {
        if (region.querySelector("dialog[open]")) {
            return true;
        }
        return isTimedPoll(requestConfig) && region.querySelector("details[open]") !== null;
    }

    document.addEventListener("htmx:beforeRequest", (event) => {
        const region = event.detail.elt;
        const requestConfig = event.detail.requestConfig;
        if (!(region instanceof Element) || !isRefreshOfPolledRegion(requestConfig)) {
            return;
        }
        if (shouldKeepRegionAsItIs(region, requestConfig)) {
            event.preventDefault();
        }
    });

    document.addEventListener("htmx:beforeSwap", (event) => {
        const target = event.detail.target;
        if (!(target instanceof HTMLElement) || target.id !== BANNER_REGION_ID) {
            return;
        }
        if (event.detail.serverResponse === target.dataset.lastBannerResponse) {
            event.detail.shouldSwap = false;
            return;
        }
        target.dataset.lastBannerResponse = event.detail.serverResponse;
    });
})();
