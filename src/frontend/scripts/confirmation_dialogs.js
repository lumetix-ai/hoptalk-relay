// Confirmation dialogs and submit buttons of the admin panel.
//
// Dialogs open and close through the browser's own invoker commands (commandfor and command
// attributes on buttons); older browsers get the same behaviour from the fallback below. A
// dialog with a typed confirmation enables its confirm button only once the text matches, and
// a plain form disables its submit buttons while it is being submitted. The server checks the
// typed text again, so all of this only saves the operator a round trip.
"use strict";

(function () {
    const browserSupportsInvokerCommands = "command" in HTMLButtonElement.prototype;

    function runDialogCommand(button) {
        const dialog = document.getElementById(button.getAttribute("commandfor"));
        if (!(dialog instanceof HTMLDialogElement)) {
            return;
        }
        const command = button.getAttribute("command");
        if (command === "show-modal" && !dialog.open) {
            dialog.showModal();
        } else if (command === "close" && dialog.open) {
            dialog.close();
        }
    }

    function updateTypedConfirmation(input) {
        const form = input.form;
        if (!form) {
            return;
        }
        const matches = input.value.trim() === input.dataset.confirmationText;
        form.querySelectorAll("[data-confirmation-submit]").forEach((button) => {
            button.disabled = !matches;
        });
    }

    function prepareTypedConfirmations(root) {
        root.querySelectorAll("input[data-confirmation-text]").forEach(updateTypedConfirmation);
    }

    function resetTypedConfirmationsOfDialog(dialog) {
        dialog.querySelectorAll("input[data-confirmation-text]").forEach((input) => {
            input.value = "";
            updateTypedConfirmation(input);
        });
    }

    document.addEventListener("click", (event) => {
        if (browserSupportsInvokerCommands || !(event.target instanceof Element)) {
            return;
        }
        const button = event.target.closest("button[commandfor]");
        if (button) {
            event.preventDefault();
            runDialogCommand(button);
        }
    });

    document.addEventListener("input", (event) => {
        if (event.target instanceof HTMLInputElement && event.target.dataset.confirmationText !== undefined) {
            updateTypedConfirmation(event.target);
        }
    });

    document.addEventListener(
        "close",
        (event) => {
            if (event.target instanceof HTMLDialogElement) {
                resetTypedConfirmationsOfDialog(event.target);
            }
        },
        true,
    );

    // htmx forms disable their buttons through hx-disabled-elt; this covers the plain ones.
    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || form.hasAttribute("hx-post") || form.hasAttribute("hx-get")) {
            return;
        }
        // Disabled in the next task: a button disabled now would leave its name and value out
        // of the submitted form.
        window.setTimeout(() => {
            form.querySelectorAll("button[type=submit]:not([disabled]), button:not([type]):not([disabled])").forEach(
                (button) => {
                    button.disabled = true;
                    button.dataset.disabledWhileSubmitting = "true";
                },
            );
        }, 0);
    });

    document.addEventListener("htmx:afterSettle", (event) => {
        if (event.target instanceof Element) {
            prepareTypedConfirmations(event.target);
        }
    });

    // A page restored from the back-forward cache keeps the buttons it disabled on submit.
    window.addEventListener("pageshow", () => {
        document.querySelectorAll("button[data-disabled-while-submitting]").forEach((button) => {
            button.disabled = false;
            delete button.dataset.disabledWhileSubmitting;
        });
        prepareTypedConfirmations(document);
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => prepareTypedConfirmations(document));
    } else {
        prepareTypedConfirmations(document);
    }
})();
