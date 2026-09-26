// "Copy" buttons: data-copy-target names the element whose value or text is copied.
//
// The Clipboard API needs a secure context and permission; when it is missing or refuses, the
// text is selected so that the operator can copy it by hand.
"use strict";

(function () {
    const FEEDBACK_MILLISECONDS = 2000;

    function readCopiedText(target) {
        if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
            return target.value;
        }
        return target.textContent.trim();
    }

    function selectText(target) {
        if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
            target.focus();
            target.select();
            return;
        }
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }

    function showFeedback(button, feedbackText) {
        const label = button.querySelector("[data-copy-label]");
        if (!label) {
            return;
        }
        if (label.dataset.originalText === undefined) {
            label.dataset.originalText = label.textContent;
        }
        label.textContent = feedbackText;
        window.setTimeout(() => {
            label.textContent = label.dataset.originalText;
        }, FEEDBACK_MILLISECONDS);
    }

    async function copyFromTarget(button) {
        const target = document.getElementById(button.dataset.copyTarget);
        if (!target) {
            return;
        }
        try {
            await navigator.clipboard.writeText(readCopiedText(target));
            showFeedback(button, "Copied");
        } catch {
            selectText(target);
            showFeedback(button, "Press Ctrl+C or ⌘C");
        }
    }

    document.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) {
            return;
        }
        const button = event.target.closest("[data-copy-target]");
        if (button) {
            event.preventDefault();
            copyFromTarget(button);
        }
    });
})();
