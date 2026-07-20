import { app } from "../../../scripts/app.js";

// Display strategy (frontend 1.45.x): update the EXISTING "text" widget in
// place. The old ShowText-style destroy-and-recreate trick is actively harmful
// in Nodes 2.0 mode — the Vue textarea renders the value from the frontend's
// registered widget-state entry (widget._state), not from `widget.value`, and
// a widget recreated via ComfyWidgets["STRING"] is a legacy DOM widget whose
// value setter bypasses that store entirely, leaving the textarea blank until
// the user interacts with it (interaction is what finally writes the store).
// The widget the frontend builds from INPUT_TYPES is _state-backed, so setting
// its value IS reactive; we also sync _state and inputEl directly as
// belt-and-braces for legacy canvas mode / older frontends.

function markNoGutter(w) {
    // Tells the BatchPrompter gutter's value-based fallback search to skip
    // this textarea (both start empty, so it used to steal the gutter).
    try { if (w?.inputEl) w.inputEl.dataset.cbNoGutter = "1"; } catch (_) {}
    return w;
}

app.registerExtension({
    name: "CoachBate.TextPreviewEdit",

    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "CoachBateTextPreviewEdit") return;

        function populate(text) {
            const w = this.widgets?.find((w) => w.name === "text");
            if (!w) return;
            const val = text == null ? "" : String(text);

            w.value = val;
            // Nodes 2.0: the Vue textarea binds to the registered state entry
            // (⚠️ minified-internal surface — re-verify on frontend upgrades).
            // DOM-legacy widgets' value setter doesn't touch it, so force it.
            try {
                if (w._state && w._state.value !== val) w._state.value = val;
            } catch (_) {}
            // Legacy canvas mode: the textarea element itself.
            try {
                if (w.inputEl && w.inputEl.value !== val) w.inputEl.value = val;
            } catch (_) {}

            markNoGutter(w);
            app.graph.setDirtyCanvas(true, false);
        }

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);

            markNoGutter(this.widgets?.find((w) => w.name === "text"));

            // ── Copy Text button ─────────────────────────────────────────
            this.addWidget("button", "📋  Copy Text", null, async () => {
                const text = this.widgets?.find((w) => w.name === "text")?.value ?? "";
                const toast = (severity, detail, life) =>
                    app.extensionManager?.toast?.add({
                        severity, summary: "CoachBate Text Preview and Edit", detail, life,
                    });
                try {
                    await navigator.clipboard.writeText(text);
                    toast("success", "Text copied to clipboard.", 2500);
                } catch {
                    try {
                        const tmp = document.createElement("textarea");
                        tmp.value = text;
                        Object.assign(tmp.style, { position: "fixed", left: "-99999px" });
                        document.body.appendChild(tmp);
                        tmp.select();
                        document.execCommand("copy");
                        document.body.removeChild(tmp);
                        toast("success", "Text copied to clipboard.", 2500);
                    } catch (err2) {
                        console.error("[CoachBate] Copy Text failed:", err2);
                        toast("error", "Could not copy text to clipboard.", 4000);
                    }
                }
            });
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (output) {
            onExecuted?.apply(this, arguments);
            const text = output?.text?.[0];
            if (text != null) populate.call(this, text);
        };
    },
});
