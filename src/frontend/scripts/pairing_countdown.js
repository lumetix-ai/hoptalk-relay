// The countdown of a pairing session.
//
// data-ends-at is when the session ends and data-server-now the server's time when the panel
// was rendered, both in ISO 8601. Their difference is counted down from the moment the element
// appears, so a wrong clock in the browser does not matter. The polled panel is replaced every
// few seconds, which also corrects any drift.
"use strict";

(function () {
    const TICK_MILLISECONDS = 1000;

    function formatRemainingTime(remainingSeconds) {
        const minutes = Math.floor(remainingSeconds / 60);
        const seconds = remainingSeconds % 60;
        return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    function findSessionEndInBrowserTime(countdown) {
        if (countdown.dataset.browserEndsAt === undefined) {
            const serverMillisecondsLeft = Date.parse(countdown.dataset.endsAt) - Date.parse(countdown.dataset.serverNow);
            countdown.dataset.browserEndsAt = String(Date.now() + serverMillisecondsLeft);
        }
        return Number(countdown.dataset.browserEndsAt);
    }

    function updateCountdown(countdown) {
        const remainingSeconds = Math.max(0, Math.ceil((findSessionEndInBrowserTime(countdown) - Date.now()) / 1000));
        const text = countdown.querySelector("[data-countdown-text]");
        if (text) {
            text.textContent = remainingSeconds > 0 ? formatRemainingTime(remainingSeconds) : "ending…";
        }
        const progress = countdown.querySelector("progress");
        if (progress) {
            progress.value = remainingSeconds;
        }
    }

    function updateAllCountdowns() {
        document.querySelectorAll("[data-ends-at][data-server-now]").forEach(updateCountdown);
    }

    window.setInterval(updateAllCountdowns, TICK_MILLISECONDS);
    document.addEventListener("htmx:afterSettle", updateAllCountdowns);
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", updateAllCountdowns);
    } else {
        updateAllCountdowns();
    }
})();
