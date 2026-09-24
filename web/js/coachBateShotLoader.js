import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// ── Theme colours for the canvas-drawn status readout ────────────────────────
// A 2D canvas can't take `var(--token)`, so read ComfyUI's own theme variables
// off the document and hand the canvas their computed values.  No hex literals:
// ComfyUI is themeable and a literal would ignore the active theme.  Cached
// with a short TTL — getComputedStyle on every repaint is not free, but the
// cache must expire so a theme switch takes effect without a reload.
const _themeCache = new Map();
const THEME_TTL_MS = 2000;
function _themeColor(...names) {
    const key = names.join("|");
    const hit = _themeCache.get(key);
    if (hit && performance.now() - hit.at < THEME_TTL_MS) return hit.value;
    let val = "";
    try {
        const cs = getComputedStyle(document.documentElement);
        for (const n of names) {
            val = (cs.getPropertyValue(n) || "").trim();
            if (val) break;
        }
    } catch (_) { /* pre-mount */ }
    if (!val) return "transparent";
    _themeCache.set(key, { value: val, at: performance.now() });
    return val;
}


// Extra pixels added to the node height to make room for the custom status box.
// LiteGraph already sizes the node for the button widgets automatically.
const STATUS_H = 60;

// ── Toast notifications from Python ──────────────────────────────────────────
// Track the last status toast so we can dismiss it when the next shot fires.
let _lastStatusToast = null;

function dismissStatusToast() {
    if (_lastStatusToast !== null) {
        try { app.extensionManager.toast.remove(_lastStatusToast); } catch (_) {}
        _lastStatusToast = null;
    }
}

api.addEventListener("coachbate.toast", ({ detail }) => {
    if (detail.kind === "status") {
        // Dismiss any previous status toast before showing the new one.
        dismissStatusToast();
        _lastStatusToast = app.extensionManager.toast.add({
            severity: detail.severity,
            summary:  detail.summary,
            detail:   detail.message,
            life:     60000,   // fallback: auto-close after 60 s if remove() doesn't work
        });
    } else {
        const msg = {
            severity: detail.severity,
            summary:  detail.summary,
            detail:   detail.message,
        };
        // Error toasts stay until the user explicitly dismisses them.
        if (detail.severity !== "error") msg.life = detail.life;
        app.extensionManager.toast.add(msg);
    }
});

// ── Windows platform detection (for browse button) ───────────────────────────
let _isWindowsServer = false;
fetch("/coachbate/platform")
    .then(r => r.json())
    .then(d => { _isWindowsServer = d.windows === true; })
    .catch(() => {});

// ── Per-node auto-queue state ─────────────────────────────────────────────────
// Stored on each node instance (this._coachbate_*) so that multiple
// CoachBateShotLoader nodes in the same graph don't interfere with each other.
//
// Module-level helpers operate on a *node* passed as an argument so there are
// no shared mutable globals for these flags.

function _cancelAutoQueueTimer(node) {
    node._coachbate_waiting_for_success = false;
    if (node._coachbate_timer !== null) {
        clearTimeout(node._coachbate_timer);
        node._coachbate_timer = null;
    }
}

function _queueNextShot(node, afterError = false) {
    node._coachbate_waiting_for_success = false;
    const graph = app.graph;
    node._coachbate_timer = setTimeout(async () => {
        node._coachbate_timer = null;
        if (!node._coachbate_autoqueue || app.graph !== graph || graph.getNodeById(node.id) !== node) return;
        if (afterError && node.properties?.continue_after_error !== true) return;
        try {
            const queued = await app.queuePrompt(0, 1);
            if (queued === false) throw new Error("Next prompt failed validation");
        } catch (err) {
            node._coachbate_autoqueue = false;
            app.extensionManager.toast.add({
                severity: "error", summary: "CoachBate - Batch stopped",
                detail: `Could not queue the next shot: ${err.message}`,
            });
            console.error("[CoachBate] Could not queue next shot:", err);
        }
    }, 150);
}

// The loader finishing is not the render finishing. Bind advancement to the
// originating prompt's success, including downstream Save Video completion.
api.addEventListener("executed", ({ detail }) => {
    const node = app.graph?.getNodeById(detail.display_node ?? detail.node);
    if (node?.type === "CoachBateShotLoader") {
        node._coachbate_prompt_id = detail.prompt_id;
    }
});

api.addEventListener("execution_success", ({ detail }) => {
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateShotLoader" ||
            node._coachbate_prompt_id !== detail.prompt_id ||
            !node._coachbate_waiting_for_success || !node._coachbate_autoqueue) continue;
        _queueNextShot(node);
    }
});

// ── Dismiss status toast when execution stops for any reason ─────────────────
// Fired when the user hits ComfyUI's own interrupt button or the queue empties.
api.addEventListener("execution_interrupted", () => {
    dismissStatusToast();
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type === "CoachBateShotLoader") {
            _cancelAutoQueueTimer(node);
            node._coachbate_autoqueue = false;
        }
    }
});

// The backend advances its counter when the loader returns. Never call Skip
// here: that would skip an additional shot. Only recover our own loaded prompt.
api.addEventListener("execution_error", ({ detail }) => {
    dismissStatusToast();
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateShotLoader" || !detail?.prompt_id ||
            node._coachbate_prompt_id !== detail.prompt_id ||
            node._coachbate_terminal_prompt_id === detail.prompt_id) continue;
        node._coachbate_terminal_prompt_id = detail.prompt_id;
        const message = detail.exception_message || "Execution failed";
        // These errors leave the CUDA process unsafe for another inference.
        const fatal = /illegal memory access|device-side assert|device lost|context is destroyed/i.test(message);
        const advance = node._coachbate_waiting_for_success && node._coachbate_autoqueue &&
            node.properties?.continue_after_error === true && !fatal;
        const failure = {
            prompt_id: detail.prompt_id,
            shot: node._coachbate_display?.split("\n")[0] || `Index ${node._coachbate_array_idx + 1}`,
            array_index: node._coachbate_array_idx,
            node_id: detail.node_id, message, time: new Date().toISOString(),
        };
        node.properties ??= {};
        node.properties.shot_loader_failures = [...(node.properties.shot_loader_failures || []), failure].slice(-200);
        console.error("[CoachBate] Shot failed:", failure);
        _cancelAutoQueueTimer(node);
        node._coachbate_display = `[FAILED] ${failure.shot}\n${advance ? "Continuing to next shot" : "Batch stopped"}`;
        app.graph.setDirtyCanvas(true, true);
        app.extensionManager.toast.add({
            severity: "error", summary: "CoachBate - Shot failed",
            detail: `${failure.shot}: ${message}. ${advance ? "Continuing; this shot still needs a retry." : "Batch stopped."}`,
        });
        if (advance) _queueNextShot(node, true);
        else node._coachbate_autoqueue = false;
    }
});

// `status` fires whenever the queue changes; dismiss when nothing is running.
api.addEventListener("status", ({ detail }) => {
    if (detail?.exec_info?.queue_remaining === 0) dismissStatusToast();
});

// ── Disable ComfyUI auto-queue (best-effort across UI versions) ───────────────
function disableAutoQueue() {
    // New ComfyUI (Vue/Pinia) — queue store
    try {
        const stores = window.__pinia?.state?.value;
        if (stores?.queue) {
            stores.queue.autoQueueMode = "disabled";
            return true;
        }
    } catch (_) {}

    // Older ComfyUI — app.ui flag
    try {
        if (app.ui?.autoQueueMode !== undefined) {
            app.ui.autoQueueMode = "disabled";
            return true;
        }
    } catch (_) {}

    // DOM fallback — find the auto-queue <select> or toggle and reset it
    try {
        const sel =
            document.querySelector("select.auto-queue-mode") ??
            document.querySelector("[data-id='autoQueueMode']") ??
            document.querySelector(".comfy-settings-dialog select") ??
            [...document.querySelectorAll("select")].find(
                el => el.textContent.toLowerCase().includes("auto") ||
                      el.id.toLowerCase().includes("auto")
            );
        if (sel) {
            sel.value = "disabled";
            sel.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        }
    } catch (_) {}

    return false;  // couldn't find it — user will need to turn it off manually
}

app.registerExtension({
    name: "CoachBate.ShotLoader",

    beforeRegisterNodeDef(nodeType) {
        if (nodeType.comfyClass !== "CoachBateShotLoader") return;

        // ── 1. Grow the node to fit status box + skip + stop buttons ─────────
        const origComputeSize = nodeType.prototype.computeSize;
        nodeType.prototype.computeSize = function (out) {
            const s = origComputeSize ? origComputeSize.call(this, out) : [200, 200];
            s[1] += STATUS_H;
            return s;
        };

        // ── 2. Add buttons on node creation ───────────────────────────────────
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);

            // Per-node auto-queue state so multiple nodes don't share flags.
            this._coachbate_autoqueue = true;
            this._coachbate_timer     = null;

            // ── 📂 Browse button (Windows only) ───────────────────────────────
            if (_isWindowsServer) {
                const browseBtn = this.addWidget("button", "📂  Browse for JSON...", null, () => {
                    fetch("/coachbate/browse_json")
                        .then(r => r.json())
                        .then(d => {
                            if (d.path) {
                                const w = this.widgets?.find(w => w.name === "json_path");
                                if (w) {
                                    w.value = d.path;
                                    app.graph.setDirtyCanvas(true);
                                }
                            }
                        })
                        .catch(err => console.error("[CoachBate] browse failed:", err));
                });
                // Place the browse button immediately below the json_path widget
                const idx = this.widgets.indexOf(browseBtn);
                if (idx > 1) {
                    this.widgets.splice(idx, 1);
                    this.widgets.splice(1, 0, browseBtn);
                }
            }

            // ── 🔁 Restart at index button ────────────────────────────────────
            this.addWidget("button", "🔁  Restart at index", null, async () => {
                const idxWidget = this.widgets?.find(w => w.name === "shot_number");
                const index = idxWidget ? parseInt(idxWidget.value, 10) : 0;

                const ok = await app.extensionManager.dialog.confirm({
                    title: "Restart run",
                    message: `Restart from shot ${index}?`,
                    type: "default",
                });
                if (!ok) return;

                fetch("/coachbate/restart", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ index: index - 1 }),
                })
                .then(r => r.json())
                .then(() => {
                    app.extensionManager.toast.add({
                        severity: "info",
                        summary:  "CoachBate",
                        detail:   `Restarting from index ${index} on next run.`,
                        life:     3000,
                    });
                })
                .catch(err => console.error("[CoachBate] restart failed:", err));
            });

            // ── ⏩ Skip button ──────────────────────────────────────────────────
            this.addWidget("button", "⏩  Skip this shot", null, async () => {
                // Use locally-cached display text so the confirm dialog always
                // reflects the current shot, even after a previous skip.
                const shotName = this._coachbate_display?.split("\n")[0]
                              ?? `shot ${(this._coachbate_array_idx ?? 0) + 1}`;

                const ok = await app.extensionManager.dialog.confirm({
                    title: "Skip shot",
                    message: `Skip "${shotName}" and move on to the next shot?`,
                    type: "default",
                });
                if (!ok) return;

                const currentIdx = this._coachbate_array_idx ?? 0;
                const total      = this._coachbate_total     ?? 1;

                fetch("/coachbate/skip", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ current_index: currentIdx, total }),
                })
                .then(r => {
                    if (!r.ok) throw new Error(`Server returned ${r.status}`);
                    return r.json();
                })
                .then((resp) => {
                    // Update local state immediately so subsequent skip clicks and
                    // the status box both reflect the new position without waiting
                    // for the next queue execution to update app.nodeOutputs.
                    const newIdx = resp.stored_index ?? (currentIdx + 1);
                    this._coachbate_array_idx = newIdx;
                    this._coachbate_display   = `[skipped] ⏩  next: shot ${newIdx + 1}`;

                    // Update the shot_index widget to match
                    const idxWidget = this.widgets?.find(w => w.name === "shot_number");
                    if (idxWidget) idxWidget.value = newIdx + 1;

                    app.graph.setDirtyCanvas(true, true);

                    app.extensionManager.toast.add({
                        severity: "warn",
                        summary:  "CoachBate",
                        detail:   `Skipped "${shotName}" — next run will load shot ${newIdx + 1}.`,
                        life:     4000,
                    });
                })
                .catch(err => console.error("[CoachBate] skip failed:", err));
            });

            // ── 🛑 Stop button ─────────────────────────────────────────────────
            this.addWidget("button", "🛑  Stop run", null, async () => {
                const shotName = this._coachbate_display?.split("\n")[0] ?? "the current shot";

                const ok = await app.extensionManager.dialog.confirm({
                    title: "Stop run",
                    message: `Stop "${shotName}", clear the queue, and disable auto-queue?`,
                    type: "delete",
                });
                if (!ok) return;

                this._coachbate_autoqueue = false;
                _cancelAutoQueueTimer(this);
                try {
                    // 1. Interrupt the currently executing job
                    await fetch("/interrupt", { method: "POST" });

                    // 2. Clear all pending queue items
                    await fetch("/queue", {
                        method:  "POST",
                        headers: { "Content-Type": "application/json" },
                        body:    JSON.stringify({ clear: true }),
                    });

                    // 3. Prevent self-advance from re-queuing after this run
                    this._coachbate_autoqueue = false;
                    _cancelAutoQueueTimer(this);

                    // 4. Turn off native auto-queue so it doesn't re-trigger either
                    const autoOff = disableAutoQueue();

                    // 5. Dismiss the running-shot status toast immediately
                    dismissStatusToast();

                    app.extensionManager.toast.add({
                        severity: "error",
                        summary:  "CoachBate — Run stopped",
                        detail:   autoOff
                            ? "Current job interrupted, queue cleared, auto-queue disabled."
                            : "Current job interrupted and queue cleared. Please disable auto-queue manually.",
                    });
                } catch (err) {
                    console.error("[CoachBate] stop failed:", err);
                    app.extensionManager.toast.add({
                        severity: "error",
                        summary:  "CoachBate",
                        detail:   "Stop failed — check the browser console.",
                    });
                }
            });
            // Append after legacy widgets; keep backend widget positions intact.
            this.properties ??= {};
            this.properties.continue_after_error ??= false;
            const recovery = this.addWidget("toggle", "Continue after error", false,
                value => { this.properties.continue_after_error = !!value; }, { serialize: false });
            Object.defineProperty(recovery, "value", {
                configurable: true,
                get: () => this.properties?.continue_after_error === true,
                set: value => { this.properties.continue_after_error = !!value; },
            });
        };

        // ── 3. Capture array index + total from execution outputs ─────────────
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (data) {
            onExecuted?.apply(this, arguments);

            // Re-enable self-advance on every successful execution so that
            // manually queueing a shot always restarts the run.
            this._coachbate_autoqueue = true;
            this._coachbate_terminal_prompt_id = null;

            if (data?.array_idx?.[0] != null) this._coachbate_array_idx = data.array_idx[0];
            if (data?.total?.[0]     != null) this._coachbate_total     = data.total[0];
            if (data?.text?.[0]      != null) this._coachbate_display   = data.text[0];

            // Reflect the actual running index back onto the shot_index widget so
            // the user can see which shot is currently being processed.
            const idxWidget = this.widgets?.find(w => w.name === "shot_number");
            if (idxWidget && data?.array_idx?.[0] != null) {
                idxWidget.value = data.array_idx[0] + 1;
                app.graph.setDirtyCanvas(true);
            }

            // Record the intent now; execution_success queues only after the
            // downstream render and save finish successfully.
            const isLast   = data?.is_last?.[0]  ?? false;
            const modeWidget = this.widgets?.find(w => w.name === "mode");
            const mode     = modeWidget?.value   ?? "increment";

            this._coachbate_waiting_for_success =
                !isLast && mode !== "fixed" && this._coachbate_autoqueue;
        };

        // ── 4. Paint the status box above the buttons ─────────────────────────
        const onDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            const r = onDrawForeground?.apply(this, arguments);
            if (this.flags.collapsed) return r;

            // Prefer the locally-cached display text so the box stays current
            // after a skip (app.nodeOutputs only refreshes on node execution).
            const raw = this._coachbate_display
                     ?? app.nodeOutputs?.[this.id + ""]?.text?.[0];
            if (!raw) return r;

            const lines = raw.split("\n");
            const pad   = 8;
            const lineH = 14;
            const boxY  = this.size[1] - STATUS_H + 4;
            const boxW  = this.size[0] - 8;
            const boxH  = STATUS_H - 8;

            ctx.save();

            ctx.globalAlpha = 0.55;
            ctx.fillStyle = _themeColor("--p-surface-900", "--comfy-input-bg");
            ctx.fillRect(4, boxY, boxW, boxH);
            ctx.globalAlpha = 1;

            ctx.font      = "bold 11px monospace";
            ctx.fillStyle = _themeColor("--p-primary-color", "--fg-color");
            lines.forEach((line, i) => {
                ctx.fillText(line, pad + 4, boxY + pad + 9 + i * lineH);
            });

            ctx.restore();
            return r;
        };
    },
});
