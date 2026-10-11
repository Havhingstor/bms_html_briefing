/* Shared screen dialogs and delegated tooltips, including iframe callers. */
(function (win) {
    "use strict";
    const doc = win.document;
    const dialogs = new WeakMap();
    let confirmationQueue = Promise.resolve();
    let sequence = 0;
    let dismissTooltip = () => {};

    function focusedElement() {
        let element = doc.activeElement;
        try {
            while (element?.contentDocument?.activeElement) element = element.contentDocument.activeElement;
        } catch { /* Cross-origin frames cannot provide their focused control. */ }
        return element;
    }

    function openDialog(dialog, { dismissible = true, focus = null } = {}) {
        if (dialog.open) return;
        dismissTooltip();
        let state = dialogs.get(dialog);
        if (!state) {
            state = {};
            dialogs.set(dialog, state);
            dialog.addEventListener("cancel", (event) => {
                event.preventDefault();
                if (state.dismissible) dialog.close("cancel");
            });
            dialog.addEventListener("click", (event) => {
                if (!state.dismissible || event.target !== dialog) return;
                const rect = dialog.getBoundingClientRect();
                if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close("cancel");
            });
            dialog.addEventListener("close", () => {
                dismissTooltip();
                if (state.previous?.isConnected && (!doc.querySelector("dialog[open]") || state.previous.closest("dialog")?.open)) {
                    state.previous.focus({ preventScroll: true });
                }
            });
        }
        Object.assign(state, { dismissible, previous: focusedElement() });
        // Escape must never reuse an accepted return value from an earlier opening.
        dialog.returnValue = "";
        dialog.showModal();
        focus?.focus({ preventScroll: true });
    }

    function confirm(options) {
        try {
            if (win.parent !== win && win.parent.InterfaceUI) return win.parent.InterfaceUI.confirm(options);
        } catch { /* A standalone or cross-origin preview owns its own dialog. */ }
        const result = confirmationQueue.then(() => new Promise((resolve) => {
            const dialog = doc.createElement("dialog");
            dialog.className = "interface-dialog interface-confirm";
            const title = doc.createElement("h2");
            title.className = "interface-dialog-title";
            title.id = `interface-confirm-title-${++sequence}`;
            title.textContent = options.title || "Confirm";
            const message = doc.createElement("p");
            message.id = `interface-confirm-message-${sequence}`;
            message.textContent = options.message;
            dialog.setAttribute("aria-labelledby", title.id);
            dialog.setAttribute("aria-describedby", message.id);
            const actions = doc.createElement("div");
            actions.className = "interface-dialog-actions";
            const cancel = doc.createElement("button");
            cancel.type = "button";
            cancel.className = "interface-button";
            cancel.textContent = options.cancelLabel || "Cancel";
            if (options.cancelTitle) cancel.title = options.cancelTitle;
            cancel.addEventListener("click", () => dialog.close("cancel"));
            const accept = doc.createElement("button");
            accept.type = "button";
            accept.className = "interface-button interface-button-primary";
            accept.textContent = options.acceptLabel || "OK";
            if (options.acceptTitle) accept.title = options.acceptTitle;
            accept.addEventListener("click", () => dialog.close("accept"));
            actions.append(cancel, accept);
            dialog.append(title, message, actions);
            doc.body.append(dialog);
            openDialog(dialog, { focus: cancel });
            dialog.addEventListener("close", () => {
                const accepted = dialog.returnValue === "accept";
                dialog.remove();
                resolve(accepted);
            }, { once: true });
        }));
        confirmationQueue = result.catch(() => {});
        return result;
    }

    function initTooltips() {
        let owner = null, text = "", timer = null, leaveTimer = null;
        const tooltip = doc.createElement("div");
        tooltip.id = "interface-tooltip";
        tooltip.className = "interface-tooltip";
        tooltip.setAttribute("role", "tooltip");
        tooltip.setAttribute("popover", "manual");
        tooltip.hidden = true;
        doc.body.append(tooltip);
        const observer = new MutationObserver(() => {
            if (!owner?.hasAttribute("title")) return;
            text = owner.getAttribute("title");
            owner.removeAttribute("title");
            if (!text) hide();
            else tooltip.textContent = text;
        });
        function hide() {
            clearTimeout(timer);
            clearTimeout(leaveTimer);
            observer.disconnect();
            if (owner) {
                if (!owner.hasAttribute("title")) owner.setAttribute("title", text);
                const ids = (owner.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== tooltip.id);
                if (ids.length) owner.setAttribute("aria-describedby", ids.join(" "));
                else owner.removeAttribute("aria-describedby");
            }
            if (tooltip.hidePopover && tooltip.matches(":popover-open")) tooltip.hidePopover();
            tooltip.hidden = true;
            owner = null;
        }
        function position() {
            if (!owner?.isConnected) { hide(); return; }
            const rect = owner.getBoundingClientRect();
            const bounds = tooltip.getBoundingClientRect();
            tooltip.style.left = `${Math.max(8, Math.min(rect.left, win.innerWidth - bounds.width - 8))}px`;
            tooltip.style.top = `${Math.max(8, rect.bottom + bounds.height + 8 <= win.innerHeight ? rect.bottom + 4 : rect.top - bounds.height - 4)}px`;
        }
        function enter(event) {
            if (owner?.contains(event.target) || tooltip.contains(event.target)) { clearTimeout(leaveTimer); return; }
            const target = event.target.closest?.("[title]");
            if (!target || target.matches("iframe, option") || !target.getAttribute("title").trim()) return;
            hide();
            owner = target;
            text = target.getAttribute("title");
            target.removeAttribute("title");
            observer.observe(target, { attributes: true, attributeFilter: ["title"] });
            timer = setTimeout(() => {
                if (!owner?.isConnected) { hide(); return; }
                tooltip.textContent = text;
                tooltip.hidden = false;
                tooltip.showPopover?.();
                const ids = (owner.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
                owner.setAttribute("aria-describedby", [...new Set([...ids, tooltip.id])].join(" "));
                position();
            }, event.type === "focusin" ? 0 : 400);
        }
        doc.addEventListener("pointerover", enter);
        doc.addEventListener("focusin", enter);
        doc.addEventListener("pointerout", (event) => {
            if (owner?.contains(event.target) && !owner.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)) leaveTimer = setTimeout(hide, 150);
        });
        tooltip.addEventListener("pointerleave", hide);
        doc.addEventListener("focusout", hide);
        doc.addEventListener("pointerdown", hide);
        doc.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
        doc.addEventListener("scroll", (event) => {
            if (tooltip.contains(event.target)) return;
            // Keyboard focus can scroll its control into view before the tooltip opens.
            if (owner?.contains(doc.activeElement)) { if (!tooltip.hidden) position(); }
            else hide();
        }, true);
        win.addEventListener("resize", hide);
        win.addEventListener("blur", hide);
        dismissTooltip = hide;
    }
    win.InterfaceUI = { openDialog, confirm };
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", initTooltips, { once: true });
    else initTooltips();
})(window);
