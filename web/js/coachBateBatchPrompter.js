import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// Width of the prompt-number gutter attached to the multiline_text textarea.
const GUTTER_W = 32;

// Short-lived guard, true ONLY while _queueBatch is posting its /prompt
// requests.  It serializes re-entrancy (a double Queue press, or an Auto Queue
// re-fire arriving mid-post) and is cleared synchronously in a finally the
// instant posting finishes — so unlike a long-lived "batch in flight" flag it
// can NEVER get stuck across executions.  This is what keeps the normal Run
// button working after a batch completes.
let _cbPosting = false;

// Muted / bypassed LiteGraph node modes.
const MODE_MUTED    = 2;
const MODE_BYPASSED = 4;
const _isActive = n => n && n.mode !== MODE_MUTED && n.mode !== MODE_BYPASSED;

// Resolve a node's DIRECT output consumers — the nodes its output links feed.
// Prefers LiteGraph's own getOutputNodes(slot) (it resolves a slot's links to
// target nodes for us); falls back to walking graph.links by id if that method
// isn't available on this frontend.  Deliberately one hop only: we don't try to
// trace the whole downstream chain, which proved unreliable.
function _outputConsumers(node) {
    const consumers = [];
    try {
        const outs = node.outputs ?? [];
        for (let slot = 0; slot < outs.length; slot++) {
            let targets = null;
            try { targets = node.getOutputNodes?.(slot) ?? null; } catch (_) { targets = null; }
            if (targets) {
                consumers.push(...targets.filter(Boolean));
                continue;
            }
            const graph = node.graph ?? app.graph;
            for (const linkId of (outs[slot]?.links ?? [])) {
                const link = graph?.links?.[linkId] ?? graph?.links?.get?.(linkId);
                const target = link ? graph?.getNodeById?.(link.target_id) : null;
                if (target) consumers.push(target);
            }
        }
    } catch (err) {
        console.warn("[CoachBate] Failed to resolve output consumers:", err);
    }
    return consumers;
}

// A BatchPrompter is only "wired up" if at least one of its outputs feeds a
// consumer that is itself active.  An output going nowhere — or only into
// muted/bypassed nodes — is a dead chain this run, so its prompts would have
// no effect.
const _feedsActiveConsumer = node => _outputConsumers(node).some(_isActive);

// ── Nodes 2.0 canvasOnly helper (mirrors coachBateTextPreviewEdit.js) ────────
// In Nodes 2.0 canvasOnly must be false so WidgetDOM.vue renders the element;
// in legacy mode it must be true to hide DOM widgets from the Parameters tab.
function _applyAdaptiveCanvasOnly(widget) {
    if (!widget?.options) return widget;
    try {
        Object.defineProperty(widget.options, "canvasOnly", {
            configurable: true, enumerable: true,
            get() { return !window.LiteGraph?.vueNodesMode; },
        });
    } catch (_) {
        widget.options.canvasOnly = !window.LiteGraph?.vueNodesMode;
    }
    return widget;
}

// ── Sequential-mode timer helpers ────────────────────────────────────────────
function _cancelSeqTimer(node) {
    if (node._cb_timer != null) {
        clearTimeout(node._cb_timer);
        node._cb_timer = null;
    }
}

// BOOLEAN widget values may surface as booleans OR strings ("true"/"false").
function _boolWidget(node, name) {
    const v = node.widgets?.find(w => w.name === name)?.value;
    return v === true || v === "true";
}

// 1-based prompt ordinals (blanks don't count) at/after startIndex —
// the same numbering as the gutter and Python's starting_number.
function _promptOrdinals(text, startIndex) {
    const ordinals = [];
    let ordinal = 0;
    for (const line of (text ?? "").split("\n")) {
        if (!line.trim()) continue;
        ordinal++;
        if (ordinal >= startIndex) ordinals.push(ordinal);
    }
    return ordinals;
}

// In-place Fisher–Yates shuffle.
function _shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
}

// Any end of a sequential sequence — finished, max_prompts cap, Stop button,
// interrupt, error — restores starting_number to the value the user launched
// with, so pressing Run again repeats the same thing. _cb_seq_active is set
// only by a manual sequential Run press, so bulk mode is never touched.
function _restoreSeqStart(node) {
    if (!node._cb_seq_active) return;
    node._cb_seq_active = false;
    node._cb_seq_order  = null;
    const w = node.widgets?.find(x => x.name === "starting_number");
    if (w && node._cb_seq_start != null) {
        w.value = node._cb_seq_start;
        try { w.callback?.(w.value); } catch (_) { /* ignore */ }
    }
}

// ── Disable ComfyUI's native Auto Queue (best-effort across UI versions) ────
// Mirrors coachBateShotLoader.js's disableAutoQueue(). Needed because
// CoachBateBatchPrompter.IS_CHANGED always returns time.time() (so the
// workflow is permanently "changed" from ComfyUI's point of view) — if the
// user separately has the native Auto Queue toggle on, clearing/interrupting
// the queue alone does NOT stop it: native Auto Queue sees an empty queue
// plus a changed workflow and immediately re-fires a new run on its own,
// completely bypassing our own _cb_autoqueue flag (which only gates OUR
// self-advance timer). That is what made Stop appear to do nothing in
// sequential mode when native Auto Queue was also enabled.
function _disableNativeAutoQueue() {
    try {
        const stores = window.__pinia?.state?.value;
        if (stores?.queue) {
            stores.queue.autoQueueMode = "disabled";
            return true;
        }
    } catch (_) {}

    try {
        if (app.ui?.autoQueueMode !== undefined) {
            app.ui.autoQueueMode = "disabled";
            return true;
        }
    } catch (_) {}

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

    return false;
}

// ── Shared stop helper ────────────────────────────────────────────────────────
async function _stopBatch(node) {
    try {
        await fetch("/interrupt", { method: "POST" });
        await fetch("/queue", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ clear: true }),
        });
        // Stop sequential self-advance as well as bulk-queue mode.
        _cancelSeqTimer(node);
        _restoreSeqStart(node);
        node._cb_autoqueue    = false;
        const autoOff = _disableNativeAutoQueue();
        node._cb_display      = "Batch stopped by user.";
        node._cb_remaining    = 0;
        node._cb_is_last      = true;
        node._cb_total_queued = null;
        node._cb_completed    = 0;
        app.graph.setDirtyCanvas(true, true);
        app.extensionManager?.toast?.add({
            severity: "warn",
            summary:  "CoachBate Batch Prompter",
            detail:   autoOff
                ? "Queue cleared, current job interrupted, auto-queue disabled."
                : "Queue cleared and current job interrupted. If ComfyUI's native Auto Queue is on, disable it manually too.",
            life:     5000,
        });
    } catch (err) {
        console.error("[CoachBate] BatchPrompter stop failed:", err);
    }
}

// ── Queue all prompts upfront — one job per block ─────────────────────────────
//
// Parses the textarea the same way Python does (one prompt per non-blank
// line), then POSTs one /prompt request per prompt.  Each request has:
//   starting_number = that prompt's 1-based ordinal (blanks don't count)
//   max_prompts     = 1 → Python emits exactly that one prompt and returns
//                     remaining=0 / is_last=true for every individual job.
// Completion is tracked via _cb_completed / _cb_total_queued on the node so
// the status widget can show "Prompt X / Y" and "✓ DONE" at the right time.
//
async function _queueBatch(node) {
    // Serialize re-entrancy: only one posting pass at a time.  The guard is
    // cleared in the finally the moment posting ends, so a subsequent Run can
    // never be permanently suppressed the way the old long-lived flag was.
    if (_cbPosting) {
        console.debug("[CoachBate] _queueBatch ignored — already posting a batch");
        return;
    }
    _cbPosting = true;
    try {
        return await _queueBatchInner(node);
    } finally {
        _cbPosting = false;
    }
}

async function _queueBatchInner(node) {
    const twMultiline  = node.widgets?.find(w => w.name === "multiline_text");
    const twStartIndex = node.widgets?.find(w => w.name === "starting_number");
    const twMaxPrompts = node.widgets?.find(w => w.name === "max_prompts");

    if (!twMultiline || !twStartIndex || !twMaxPrompts) {
        console.error("[CoachBate] BatchPrompter: required widgets not found");
        return;
    }

    const text        = twMultiline.value   ?? "";
    const startIndex  = twStartIndex.value  ?? 0;
    const maxPrompts  = twMaxPrompts.value  ?? 1000;

    // ── Collect up to maxPrompts prompt ordinals from startIndex ─────────────
    // startIndex is a 1-based PROMPT ordinal (blank lines don't count),
    // matching both the gutter numbering and Python's interpretation of
    // starting_number. Each queued job gets its prompt's ordinal baked in.
    // With randomize on, the whole pool is shuffled BEFORE the maxPrompts cap
    // so the cap selects a random non-repeating subset, not the first N.
    const blockOrdinals = _promptOrdinals(text, startIndex);
    if (_boolWidget(node, "randomize")) _shuffle(blockOrdinals);
    if (blockOrdinals.length > maxPrompts) blockOrdinals.length = maxPrompts;

    if (blockOrdinals.length === 0) {
        app.extensionManager?.toast?.add({
            severity: "warn",
            summary:  "CoachBate Batch Prompter",
            detail:   "No prompts found in the text.",
            life:     4000,
        });
        return;
    }

    // ── Claim the batch lock BEFORE posting ───────────────────────────────────
    // Suppresses any re-entrant app.queuePrompt (Auto Queue firing after the
    // first job completes, or a fast double-press) while we post the rest.
    node._cb_total_queued = blockOrdinals.length;
    node._cb_completed    = 0;
    node._cb_is_last      = false;
    node._cb_display      = `Queuing ${blockOrdinals.length} prompt${blockOrdinals.length !== 1 ? "s" : ""}…`;
    app.graph.setDirtyCanvas(true, true);

    // ── Serialize current graph ───────────────────────────────────────────────
    let graphData;
    try {
        graphData = await app.graphToPrompt();
    } catch (err) {
        app.extensionManager?.toast?.add({
            severity: "error",
            summary:  "CoachBate Batch Prompter",
            detail:   "Could not serialize graph: " + err.message,
            life:     6000,
        });
        console.error("[CoachBate] graphToPrompt failed:", err);
        node._cb_total_queued = null;   // release the lock claimed above
        return;
    }

    const { workflow, output: promptData } = graphData;
    const nodeId   = String(node.id);
    const clientId = app.clientId ?? "";

    // Strip nodes with no class_type (group nodes, notes, reroutes that newer
    // ComfyUI includes in graphToPrompt output but the backend rejects).
    for (const id of Object.keys(promptData)) {
        if (!promptData[id]?.class_type) delete promptData[id];
    }

    if (!promptData[nodeId]) {
        console.error("[CoachBate] BatchPrompter: node id", nodeId, "not found in serialized prompt");
        app.extensionManager?.toast?.add({
            severity: "error",
            summary:  "CoachBate Batch Prompter",
            detail:   "Node not found in serialized graph — try saving the workflow first.",
            life:     6000,
        });
        node._cb_total_queued = null;   // release the lock claimed above
        return;
    }

    // ── Submit one /prompt per block ──────────────────────────────────────────
    let queued = 0;
    for (let b = 0; b < blockOrdinals.length; b++) {
        // max_prompts=1 ensures Python emits exactly this one prompt.
        const modifiedPrompt = JSON.parse(JSON.stringify(promptData));
        modifiedPrompt[nodeId].inputs.starting_number = blockOrdinals[b];   // 1-based prompt ordinal
        modifiedPrompt[nodeId].inputs.max_prompts = 1;          // exactly one prompt per job
        modifiedPrompt[nodeId].inputs.job_index   = b + 1;     // 1-based
        modifiedPrompt[nodeId].inputs.job_total   = blockOrdinals.length;

        try {
            const resp = await fetch("/prompt", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({
                    prompt:     modifiedPrompt,
                    extra_data: { extra_pnginfo: { workflow } },
                    client_id:  clientId,
                }),
            });
            if (resp.ok) {
                queued++;
            } else {
                const body = await resp.text().catch(() => "");
                console.warn("[CoachBate] /prompt returned", resp.status, "for block", b, body);
            }
        } catch (err) {
            console.error("[CoachBate] Failed to queue block", b, err);
        }
    }

    if (queued === 0) {
        app.extensionManager?.toast?.add({
            severity: "error",
            summary:  "CoachBate Batch Prompter",
            detail:   "Failed to queue any prompts — check the browser console.",
            life:     6000,
        });
        node._cb_total_queued = null;   // release the lock claimed above
        return;
    }

    node._cb_total_queued = queued;
    node._cb_completed    = 0;
    node._cb_display      = `Queued ${queued} prompt${queued !== 1 ? "s" : ""}…`;
    node._cb_is_last      = false;
    node._cb_remaining    = queued;
    app.graph.setDirtyCanvas(true, true);

    app.extensionManager?.toast?.add({
        severity: "info",
        summary:  "CoachBate Batch Prompter",
        detail:   `${queued} prompt${queued !== 1 ? "s" : ""} added to queue.`,
        life:     4000,
    });
}

// ── Reset display on interrupt / error ────────────────────────────────────────
api.addEventListener("execution_interrupted", () => {
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        _cancelSeqTimer(node);
        _restoreSeqStart(node);
        node._cb_autoqueue    = false;
        node._cb_display      = "Interrupted.";
        node._cb_remaining    = 0;
        node._cb_is_last      = true;
        node._cb_total_queued = null;
        node._cb_completed    = 0;
        app.graph.setDirtyCanvas(true, true);
    }
});

api.addEventListener("execution_error", () => {
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        _cancelSeqTimer(node);
        _restoreSeqStart(node);
        node._cb_autoqueue    = false;
        node._cb_display      = "Error — batch stopped.";
        node._cb_remaining    = 0;
        node._cb_is_last      = true;
        node._cb_total_queued = null;
        node._cb_completed    = 0;
        app.graph.setDirtyCanvas(true, true);
    }
});

// ── Clear the progress display when the queue empties ─────────────────────────
// _cb_total_queued now only drives the on-node status readout (it no longer
// gates Run — _cbPosting does), so this just tidies the display back to idle.
// ComfyUI dispatches the status detail as { exec_info: { queue_remaining } }
// (no `.status` nesting) — matching coachBateShotLoader.js.  The extra `.status`
// level in an earlier version is what left the readout stuck mid-batch.
api.addEventListener("status", ({ detail }) => {
    const remaining = detail?.exec_info?.queue_remaining ?? -1;
    if (remaining !== 0) return;
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        if (node._cb_total_queued != null) {
            node._cb_total_queued = null;
            app.graph.setDirtyCanvas(true, true);
        }
    }
});

// ── Main extension ────────────────────────────────────────────────────────────
app.registerExtension({
    name: "CoachBate.BatchPrompter",

    // ── Turn a normal Queue press into a per-prompt fan-out ───────────────────
    // We patch app.queuePrompt once at setup.  When exactly one active, connected
    // BatchPrompter is present, a Queue press fans out into one /prompt job per
    // prompt block instead of a single run.  Re-entrancy (a re-fire arriving
    // while we're still posting) is blocked by the short-lived _cbPosting guard,
    // NOT by any long-lived per-node flag — so Run can never be left dead after
    // a batch finishes.
    setup() {
        // app.queuePrompt may not exist yet on very early setup calls — retry.
        const patchOnce = () => {
            if (typeof app.queuePrompt !== "function") {
                setTimeout(patchOnce, 200);
                return;
            }
            const orig = app.queuePrompt.bind(app);
            app.queuePrompt = async function (...args) {
                // Mid-post?  This call is a re-fire (Auto Queue or double-press)
                // arriving while _queueBatch is still POSTing — suppress it so we
                // don't stack duplicates.  _cbPosting is cleared the instant the
                // posting loop ends, so this never blocks a later manual Run.
                if (_cbPosting) {
                    console.debug("[CoachBate] queuePrompt suppressed — mid-post");
                    return;
                }

                const allBatch = (app.graph?.nodes ?? []).filter(
                    n => n.type === "CoachBateBatchPrompter"
                );

                // A node only drives the batch if it's both:
                //   • active   — not muted (mode 2) and not bypassed (mode 4)
                //   • wired up — an output feeds a consumer that is itself active
                // A node whose output goes nowhere, or only into muted/bypassed
                // nodes, drives nothing this run.
                const activeBatch = allBatch.filter(_isActive);
                const driving     = activeBatch.filter(_feedsActiveConsumer);

                // An active BatchPrompter whose output leads nowhere live: do
                // nothing at all.  The node is OUTPUT_NODE=True, so a normal run
                // would still execute it and burn a pointless generation — which
                // is exactly what pressing Run here must not do.
                if (activeBatch.length > 0 && driving.length === 0) {
                    console.debug(
                        "[CoachBate] Run ignored — no Batch Prompter output feeds an active node"
                    );
                    app.extensionManager?.toast?.add({
                        severity: "warn",
                        summary:  "CoachBate Batch Prompter",
                        detail:   "Nothing queued — the Batch Prompter's output isn't connected to " +
                                  "anything active (it's unconnected, or only feeds muted/bypassed nodes).",
                        life:     5000,
                    });
                    return;
                }

                // Fan a normal Queue press out into one job per prompt block,
                // OR (when queue_all_at_once is false) fall through to a normal
                // single run so onExecuted can advance starting_number one prompt
                // at a time, exactly like ShotLoader's Auto Queue pattern.
                if (driving.length === 1) {
                    const node = driving[0];
                    const qaw = node.widgets?.find(w => w.name === "queue_all_at_once");
                    // BOOLEAN widgets may surface as boolean false OR string "false".
                    // Treat anything that isn't explicitly one of those as true (queue-all).
                    const sequential = qaw?.value === false || qaw?.value === "false";
                    if (!sequential) return _queueBatch(node);
                    // Sequential mode: a plain orig() queues one run; when it
                    // finishes, onExecuted self-advances via app.queuePrompt.
                    // A manual Run press (not a self-advance re-entry) starts a
                    // fresh sequence — reset the prompts-run counter that
                    // enforces max_prompts across the whole sequence.
                    if (node._cb_seqAdvancing) {
                        node._cb_seqAdvancing = false;
                    } else {
                        node._cb_seq_count  = 0;
                        node._cb_seq_active = true;   // enables _restoreSeqStart on any end
                        // Remember where the user started so every way the
                        // sequence can end restores it (re-Run repeats the range).
                        const sw = node.widgets?.find(w => w.name === "starting_number");
                        node._cb_seq_start = sw?.value ?? 1;

                        if (_boolWidget(node, "randomize")) {
                            // Shuffle the pool of prompts at/after the start
                            // position; the sequence steps through this order
                            // (never repeating) instead of Python's linear
                            // next_start_index. Reshuffled every Run press.
                            const text  = node.widgets?.find(w => w.name === "multiline_text")?.value ?? "";
                            const order = _shuffle(_promptOrdinals(text, node._cb_seq_start));
                            node._cb_seq_order = order.length ? order : null;
                            node._cb_seq_pos   = 0;
                            // Point this first run at the first random prompt —
                            // set BEFORE orig() so graph serialization sees it.
                            if (order.length && sw) {
                                sw.value = order[0];
                                try { sw.callback?.(sw.value); } catch (_) { /* ignore */ }
                            }
                        } else {
                            node._cb_seq_order = null;
                        }
                    }
                }
                if (driving.length > 1) {
                    console.warn(
                        "[CoachBate] Multiple active, connected BatchPrompter nodes — " +
                        "fan-out is ambiguous; running a normal single queue instead."
                    );
                    app.extensionManager?.toast?.add({
                        severity: "warn",
                        summary:  "CoachBate Batch Prompter",
                        detail:   `${driving.length} Batch Prompter nodes are active and connected — ` +
                                  "mute/bypass or disconnect all but one so Run knows which prompts to fan out.",
                        life:     6000,
                    });
                }

                return orig(...args);
            };
        };
        patchOnce();
    },

    beforeRegisterNodeDef(nodeType) {
        if (nodeType.comfyClass !== "CoachBateBatchPrompter") return;

        // ── Per-node setup ───────────────────────────────────────────────────
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);

            // Bug fixed: the Stop click handler below referenced `self`
            // without this declaration — in a browser that silently resolves
            // to `window`, so _stopBatch(window) cleared the queue but never
            // cancelled THIS node's sequential timer / autoqueue flag.
            const self = this;

            this._cb_display      = null;
            this._cb_remaining    = null;
            this._cb_is_last      = false;
            this._cb_total_queued = null;   // null = single-shot/sequential mode; N = bulk mode
            this._cb_completed    = 0;      // jobs completed in current bulk run
            this._cb_autoqueue    = true;   // sequential mode: false after stop/error
            this._cb_timer        = null;   // sequential mode: pending setTimeout id

            this._cb_attachGutter();
            this._cb_addStatusWidget();

            // ── Stop button (centred, narrow — clear of the resize corner) ──
            // The batch now starts from ComfyUI's normal Queue button: the
            // app.queuePrompt patch in setup() fans a normal run out into one
            // job per prompt.  So only Stop lives on the node face, kept narrow
            // and centred so it isn't under the bottom-right resize handle.
            // ── Stop button (DOM, centred via flexbox) ───────────────────────
            // Previously a canvas-draw widget; in Nodes 2.0 the canvas draw
            // function is not called for node-body widgets, so it rendered as an
            // unstyled left-aligned DOM element.  A real addDOMWidget with flexbox
            // centering works in both legacy and Nodes 2.0.
            const stopWrap = document.createElement("div");
            Object.assign(stopWrap.style, {
                display: "flex", justifyContent: "center", alignItems: "center",
                width: "100%", padding: "2px 0", boxSizing: "border-box",
            });
            const stopBtn = document.createElement("button");
            stopBtn.textContent = "🛑  Stop batch";
            Object.assign(stopBtn.style, {
                width: "50%", maxWidth: "150px", minWidth: "100px",
                padding: "3px 0",
                background: "#3a1010", color: "#ef9f9f",
                border: "0.8px solid #8a3a3a", borderRadius: "3px",
                font: "bold 11px sans-serif", cursor: "pointer",
            });
            stopWrap.appendChild(stopBtn);
            stopBtn.addEventListener("click", async () => {
                const ok = await app.extensionManager.dialog.confirm({
                    title: "Stop batch",
                    message: "Interrupt the current job and clear all pending queue items?",
                    type: "delete",
                });
                if (ok) _stopBatch(self);
            });
            // getMaxHeight caps this widget in the free-space split: leftover
            // vertical space from a node resize is shared among DOM widgets,
            // and without the cap this row grows along with the textarea.
            const stopDomWidget = this.addDOMWidget(
                "_cb_stop_row", "coachbate_stopbtn", stopWrap,
                {
                    serialize: false, getValue: () => null, setValue: () => {},
                    getMinHeight: () => 34, getMaxHeight: () => 34, getHeight: () => 34,
                },
            );
            _applyAdaptiveCanvasOnly(stopDomWidget);

            // ── Hide auto-managed optional inputs ────────────────────────────
            // job_index and job_total are set by _queueBatch and should never
            // be user-visible.  Collapsing them to zero height keeps them in
            // the serialised workflow (so Python receives the values) while
            // removing them from the visual widget list.
            for (const name of ["job_index", "job_total"]) {
                const w = this.widgets?.find(w => w.name === name);
                if (w) {
                    w.hidden      = true;   // Nodes 2.0 DOM renderer
                    w.computeSize = () => [0, -4];   // old canvas renderer
                }
            }
        };

        // ── DOM gutter ───────────────────────────────────────────────────────
        // Modern ComfyUI renders the multiline_text widget as a persistent HTML
        // <textarea>, so widget.draw is never called.  We attach a floating
        // gutter <div> (fixed-positioned against the viewport) and use a hidden
        // mirror <div> to measure the *visual* y-offset of each prompt block's
        // first line — accounting for text wrap, leading blank lines, and
        // multiple empty lines between paragraphs.
        nodeType.prototype._cb_attachGutter = function () {
            const tw = this.widgets?.find(w => w.name === "multiline_text");
            if (!tw) {
                console.warn("[CoachBate] BatchPrompter: multiline_text widget not found");
                return;
            }

            const self    = this;
            let   el      = null;   // the textarea
            let   gutter  = null;   // floating container we draw into
            let   mirror  = null;   // hidden div used purely for measurement
            let   hl      = null;   // active-prompt highlight band (sequential mode)

            // Which prompt to highlight: in sequential mode the widget holds
            // the ordinal of the prompt that is running (it's advanced only
            // after the run completes), so highlighting it tracks the active
            // job. -1 (queue-all mode) → no highlight.
            const activeOrdinal = () => {
                const qaw = self.widgets?.find(w => w.name === "queue_all_at_once");
                const seq = qaw?.value === false || qaw?.value === "false";
                if (!seq) return -1;
                const v = self.widgets?.find(w => w.name === "starting_number")?.value;
                return typeof v === "number" ? v : -1;
            };

            // Find the textarea — widget properties first, fall back to a
            // DOM-wide search by current value.
            const findTextarea = () => {
                const candidates = [tw.inputEl, tw.element, tw.input, tw.domElement, tw.dom];
                for (const c of candidates) {
                    if (!c) continue;
                    if (c.tagName === "TEXTAREA") return c;
                    const inner = c.querySelector?.("textarea");
                    if (inner) return inner;
                }
                const want = tw.value ?? "";
                for (const ta of document.querySelectorAll("textarea")) {
                    if (ta._cb_owned) continue;
                    if (ta.dataset?.cbNoGutter) continue;  // TextPreviewEdit + similar DOM textareas
                    if (ta.value === want) return ta;
                }
                return null;
            };

            // Sync mirror's styling to the textarea so its line wrapping
            // matches exactly.  Returns the textarea's computed style for
            // re-use by the caller (paddings, line-height, etc.).
            const syncMirror = () => {
                const cs = getComputedStyle(el);
                const padL = parseFloat(cs.paddingLeft)  || 0;
                const padR = parseFloat(cs.paddingRight) || 0;
                Object.assign(mirror.style, {
                    width:          `${el.clientWidth - padL - padR}px`,
                    font:           cs.font,
                    fontFamily:     cs.fontFamily,
                    fontSize:       cs.fontSize,
                    fontWeight:     cs.fontWeight,
                    lineHeight:     cs.lineHeight,
                    letterSpacing:  cs.letterSpacing,
                    tabSize:        cs.tabSize,
                });
                return cs;
            };

            // Measure the visual y-offset (from top of textarea content area)
            // at which line `n` of the textarea begins.  Uses the mirror to
            // render every preceding line under the same wrap rules.
            const measureLineY = (lines, n) => {
                if (n === 0) return 0;
                // Empty lines collapse to height 0 in a div — substitute a
                // single space so each empty source line still takes one
                // line-height of vertical space.
                mirror.textContent =
                    lines.slice(0, n).map(l => l === "" ? " " : l).join("\n");
                return mirror.getBoundingClientRect().height;
            };

            // Cheap escape used to compare "do I need to redraw?" payloads.
            let lastDrawKey = "";

            const redraw = () => {
                if (!el.isConnected) return;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) {
                    gutter.style.display = "none";
                    return;
                }
                gutter.style.display = "block";
                gutter.style.left    = `${rect.left}px`;
                gutter.style.top     = `${rect.top}px`;
                gutter.style.height  = `${rect.height}px`;

                // Scale: visual px per layout px — tracks ComfyUI canvas zoom.
                // Must be computed before syncMirror so paddingLeft is correct
                // when the mirror measures line widths.
                const scale = el.offsetHeight > 0 ? rect.height / el.offsetHeight : 1;

                // Keep text clear of the gutter at every zoom level.
                // paddingLeft is in layout pixels; the gutter is visual pixels,
                // so we divide by scale to get the layout-pixel equivalent.
                const needPad = `${Math.ceil((GUTTER_W + 6) / scale)}px`;
                if (el.style.paddingLeft !== needPad) el.style.paddingLeft = needPad;

                const cs      = syncMirror();
                const padTop  = parseFloat(cs.paddingTop) || 0;
                const scrollT = el.scrollTop;
                const value   = el.value ?? "";
                const act     = activeOrdinal();

                // Skip if neither content, size, scroll, scale, nor the active
                // prompt changed.
                const key = `${value.length}|${rect.width}|${rect.height}|${scrollT}|${cs.fontSize}|${act}|${value}`;
                if (key === lastDrawKey) return;
                lastDrawKey = key;

                // Clear existing numbers (keep background — that's on `gutter`).
                while (gutter.firstChild) gutter.removeChild(gutter.firstChild);

                const lines = value.split("\n");
                let n = 0;
                let hlPlaced = false;

                for (let i = 0; i < lines.length; i++) {
                    if (!lines[i].trim()) continue;   // skip blank lines

                    // Every non-blank line is its own numbered prompt.
                    n++;

                    const yContent = measureLineY(lines, i);
                    // Multiply by scale so the position matches the visually
                    // rendered (possibly zoomed/transformed) textarea.
                    const yVisible = (padTop + yContent - scrollT) * scale;

                    // Cull numbers outside the visible area (rect.height is visual).
                    if (yVisible < -16 || yVisible > rect.height) continue;

                    const isActive = n === act;

                    // Active-prompt highlight band across the textarea
                    // (sequential mode only). measureLineY(i+1) - yContent is
                    // the full visual height of this line including wraps.
                    if (isActive) {
                        const lineH = Math.max(
                            (measureLineY(lines, i + 1) - yContent) * scale, 4);
                        Object.assign(hl.style, {
                            display: "block",
                            left:    `${rect.left}px`,
                            width:   `${rect.width}px`,
                            top:     `${rect.top + yVisible}px`,
                            height:  `${lineH}px`,
                        });
                        hlPlaced = true;
                    }

                    const num = document.createElement("div");
                    num.textContent = String(n);
                    Object.assign(num.style, {
                        position:   "absolute",
                        left:       "0",
                        right:      "0",
                        top:        `${yVisible}px`,
                        textAlign:  "center",
                        color:      isActive ? "#ffd27a" : "#c89040",
                        background: isActive ? "rgba(200, 144, 64, 0.35)" : "",
                        font:       cs.font,
                        fontWeight: "bold",
                        lineHeight: cs.lineHeight,
                    });
                    gutter.appendChild(num);
                }

                if (!hlPlaced) hl.style.display = "none";
            };

            // Cheap reposition-only when neither size nor content changed
            // (used by the rAF loop for node-drag tracking).
            let lastRect = "";
            const repositionOnly = () => {
                if (!el.isConnected) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) {
                    gutter.style.display = "none";
                    hl.style.display     = "none";
                    return false;
                }
                const key = `${r.left}|${r.top}|${r.width}|${r.height}`;
                if (key === lastRect) return false;
                lastRect = key;
                gutter.style.display = "block";
                gutter.style.left    = `${r.left}px`;
                gutter.style.top     = `${r.top}px`;
                gutter.style.height  = `${r.height}px`;
                return true;
            };

            const tryAttach = (attempt = 0) => {
                el = findTextarea();
                if (!el) {
                    if (attempt < 50) {
                        setTimeout(() => tryAttach(attempt + 1), 200);
                    } else {
                        console.warn(
                            "[CoachBate] BatchPrompter: textarea not found.",
                            "widget props:", Object.keys(tw), "widget:", tw,
                        );
                    }
                    return;
                }
                if (el._cb_owned) return;
                el._cb_owned = true;

                console.debug("[CoachBate] BatchPrompter: gutter attached to", el);

                // Floating gutter container (background + child numbers).
                gutter = document.createElement("div");
                Object.assign(gutter.style, {
                    position:      "fixed",
                    pointerEvents: "none",
                    overflow:      "hidden",
                    boxSizing:     "border-box",
                    width:         `${GUTTER_W}px`,
                    background:    "rgba(18, 18, 28, 0.93)",
                    borderRight:   "1px solid rgba(200, 155, 55, 0.45)",
                    zIndex:        "1000",
                });
                document.body.appendChild(gutter);

                // Active-prompt highlight band (sequential mode). Sits under
                // the gutter numbers, spans the textarea width; pointer-events
                // off so typing/selection beneath is unaffected.
                hl = document.createElement("div");
                Object.assign(hl.style, {
                    position:      "fixed",
                    pointerEvents: "none",
                    background:    "rgba(200, 144, 64, 0.16)",
                    borderTop:     "1px solid rgba(200, 144, 64, 0.55)",
                    borderBottom:  "1px solid rgba(200, 144, 64, 0.55)",
                    boxSizing:     "border-box",
                    zIndex:        "999",
                    display:       "none",
                });
                document.body.appendChild(hl);

                // Hidden mirror div used to measure wrapped-text heights.
                mirror = document.createElement("div");
                Object.assign(mirror.style, {
                    position:      "absolute",
                    visibility:    "hidden",
                    top:           "0",
                    left:          "-99999px",
                    whiteSpace:    "pre-wrap",
                    overflowWrap:  "break-word",
                    wordWrap:      "break-word",
                    padding:       "0",
                    margin:        "0",
                    border:        "0",
                    boxSizing:     "content-box",
                });
                document.body.appendChild(mirror);

                self._cb_gutter   = gutter;
                self._cb_mirror   = mirror;
                self._cb_hl       = hl;
                self._cb_textarea = el;

                // paddingLeft is set dynamically in redraw() based on zoom scale.
                el.style.boxSizing = "border-box";

                redraw();

                el.addEventListener("input",  redraw);
                el.addEventListener("scroll", redraw);
                new ResizeObserver(() => { lastDrawKey = ""; redraw(); }).observe(el);

                // Hide the gutter when the browser goes fullscreen (e.g. ComfyUI
                // image viewer).  z-index 1000 handles most overlay cases; this
                // covers native-fullscreen where everything else is hidden.
                const _onFullscreen = () => {
                    const vis = document.fullscreenElement ? "hidden" : "visible";
                    gutter.style.visibility = vis;
                    hl.style.visibility     = vis;
                };
                document.addEventListener("fullscreenchange", _onFullscreen);
                self._cb_visibilityListener = _onFullscreen;

                // Node-drag tracking — cheap because both bail when nothing
                // changed.  redraw() also runs if the size changed (which can
                // happen when a node is resized via dragging).
                let lastAct = null;
                const tick = () => {
                    if (!el.isConnected) {
                        // Textarea was disconnected (Vue re-render replaced it).
                        // Remove the gutter and try to re-attach to the new element.
                        gutter.remove();
                        mirror.remove();
                        hl.remove();
                        el._cb_owned = false;
                        setTimeout(() => tryAttach(0), 300);
                        return;
                    }
                    // Redraw when the node moved/resized OR the active prompt
                    // advanced (sequential runs change starting_number without
                    // touching the textarea, so input/scroll events don't fire).
                    const act = activeOrdinal();
                    if (repositionOnly() || act !== lastAct) {
                        lastAct = act;
                        lastDrawKey = "";
                        redraw();
                    }
                    requestAnimationFrame(tick);
                };
                requestAnimationFrame(tick);
            };

            setTimeout(() => tryAttach(0), 200);
        };

        // Clean up the floating gutter when the node is removed.
        const origOnRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            origOnRemoved?.apply(this, arguments);
            _cancelSeqTimer(this);
            this._cb_autoqueue = false;
            this._cb_gutter?.remove();
            this._cb_mirror?.remove();
            this._cb_hl?.remove();
            if (this._cb_visibilityListener)
                document.removeEventListener("fullscreenchange", this._cb_visibilityListener);
            if (this._cb_textarea) {
                this._cb_textarea.style.paddingLeft = "";
                delete this._cb_textarea._cb_owned;
            }
        };

        // ── Status display widget (sits in natural widget flow) ──────────────
        nodeType.prototype._cb_addStatusWidget = function () {
            const self = this;

            const sw = this.addWidget("button", "_cb_status_display", "", () => {});
            sw.serialize  = false;
            sw.hidden     = true;   // Nodes 2.0: hide DOM widget; canvas draw still runs

            sw.draw = function (ctx, node, widgetWidth, y, h) {
                const val = self._cb_display;
                ctx.save();
                if (!val) {
                    ctx.fillStyle = "rgba(20, 20, 30, 0.35)";
                    ctx.fillRect(4, y + 1, widgetWidth - 8, h - 2);
                    ctx.restore();
                    return;
                }
                const lines = val.split("\n");
                ctx.fillStyle = "rgba(20, 20, 30, 0.90)";
                ctx.fillRect(4, y + 1, widgetWidth - 8, h - 2);
                ctx.fillStyle = self._cb_is_last ? "#6fcf97" : "#f5a623";
                ctx.fillRect(4, y + 1, 3, h - 2);
                ctx.font      = "bold 11px monospace";
                ctx.fillStyle = "#f0e6d3";
                ctx.fillText(lines[0] ?? "", 12, y + 15);
                if (lines[1]) ctx.fillText(lines[1], 12, y + 30);
                if (self._cb_is_last && self._cb_remaining === 0) {
                    ctx.font      = "bold 10px monospace";
                    ctx.fillStyle = "#6fcf97";
                    const badge = "✓ DONE";
                    const bw    = ctx.measureText(badge).width + 8;
                    ctx.fillText(badge, widgetWidth - 8 - bw, y + 15);
                }
                ctx.restore();
            };
            sw.computeSize = (w) => [w, 42];
            this._cb_sw = sw;
        };

        // ── onExecuted ───────────────────────────────────────────────────────
        // Bulk-queue mode (_cb_total_queued is set):
        //   Track job completions ourselves; never touch the starting_number widget
        //   because all jobs are already in the queue with baked-in inputs.
        //
        // Legacy single-shot / auto-queue mode (_cb_total_queued is null):
        //   Write next_starting_number back into the starting_number widget so
        //   ComfyUI's Auto Queue re-fires on a hash change (old behaviour).
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (data) {
            onExecuted?.apply(this, arguments);

            if (this._cb_total_queued != null) {
                // ── Bulk-queue mode ──────────────────────────────────────────
                this._cb_completed++;
                const n = this._cb_completed;
                const t = this._cb_total_queued;

                if (n >= t) {
                    this._cb_display      = `Done — ${t} prompt${t !== 1 ? "s" : ""} processed`;
                    this._cb_is_last      = true;
                    this._cb_remaining    = 0;
                    this._cb_total_queued = null;
                } else {
                    this._cb_display   = `Prompt ${n} / ${t} done\n${t - n} remaining`;
                    this._cb_is_last   = false;
                    this._cb_remaining = t - n;
                }

            } else {
                // ── Sequential mode (queue_all_at_once = false) ──────────────
                // Guard: only a manual sequential Run sets _cb_seq_active (see
                // the queuePrompt patch). If we reach here without it, this is
                // NOT a real sequential run — it's a bulk-queue job whose
                // onExecuted arrived after _cb_total_queued was cleared out
                // from under us (an execution error / interrupt mid-batch nulls
                // it, and the remaining already-queued jobs then fall into this
                // branch). Doing the self-advance below would re-enable
                // _cb_autoqueue and, in queue_all mode, re-post the ENTIRE
                // batch. Bail out and leave the stopped/error display intact.
                if (!this._cb_seq_active) return;

                // Re-enable self-advance on every successful execution so that
                // pressing Queue manually always (re)starts the sequence, same
                // as ShotLoader does with _coachbate_autoqueue.
                this._cb_autoqueue = true;

                if (data?.text?.[0]      != null) this._cb_display   = data.text[0];
                if (data?.remaining?.[0] != null) this._cb_remaining = data.remaining[0];
                if (data?.is_last?.[0]   != null) this._cb_is_last   = data.is_last[0];

                const done    = data?.done?.[0] === true;
                const isLast  = data?.is_last?.[0] === true;
                const nextIdx = data?.next_start_index?.[0];
                // has_more: prompts exist beyond this one in the WHOLE text
                // (is_last only says the per-run max_prompts window ran out).
                // Missing on a pre-restart server — fall back to !is_last.
                const hasMore = data?.has_more?.[0] ?? !isLast;

                // max_prompts caps the whole sequence: each self-advanced run
                // opens a fresh window in Python, so the cap must be counted
                // here across runs. _cb_seq_count is reset by a manual Run
                // press in the queuePrompt patch.
                this._cb_seq_count = (this._cb_seq_count ?? 0) + 1;
                const maxP = this.widgets?.find(x => x.name === "max_prompts")?.value ?? Infinity;
                const capReached = this._cb_seq_count >= maxP;

                const w = this.widgets?.find(x => x.name === "starting_number");
                const setStart = (v) => {
                    if (!w) return;
                    w.value = v;
                    try { w.callback?.(w.value); } catch (_) { /* ignore */ }
                };

                // Randomize mode: the shuffled order built at Run time replaces
                // Python's linear next_start_index / has_more.
                const rand   = Array.isArray(this._cb_seq_order);
                let nextP    = nextIdx;
                let moreLeft = hasMore;
                if (rand) {
                    this._cb_seq_pos = (this._cb_seq_pos ?? 0) + 1;
                    moreLeft = this._cb_seq_pos < this._cb_seq_order.length;
                    nextP    = moreLeft ? this._cb_seq_order[this._cb_seq_pos] : null;
                }

                if (!done && moreLeft && !capReached && nextP != null) {
                    // Show the current position; a manual restart lands right.
                    setStart(nextP);

                    // Self-advance: queue the next single run directly, exactly
                    // as ShotLoader does — no reliance on Auto Queue mode.
                    if (this._cb_autoqueue) {
                        this._cb_timer = setTimeout(() => {
                            this._cb_timer = null;
                            if (this._cb_autoqueue) {
                                this._cb_seqAdvancing = true;   // don't reset _cb_seq_count
                                app.queuePrompt(0, 1);
                            }
                        }, 150);
                    }
                } else if (!done && moreLeft && capReached) {
                    // max_prompts reached mid-text: stop and restore the
                    // starting_number the user launched with — the rule is
                    // that ANY end of a sequence puts the widget back where
                    // the user set it, so re-Run repeats the same thing.
                    _restoreSeqStart(this);
                    this._cb_display =
                        `Ran ${this._cb_seq_count}${rand ? " random" : ""} ` +
                        `prompt${this._cb_seq_count !== 1 ? "s" : ""} (max_prompts)`;
                } else {
                    // Finished — this run WAS the last prompt in the text (or
                    // Python reported done). Crucially, we must NOT queue one
                    // more run just to hit Python's "batch finished" branch:
                    // that run outputs an empty string that errors downstream.
                    if (!done) {
                        this._cb_display = "Done — all prompts processed";
                        app.extensionManager?.toast?.add({
                            severity: "success",
                            summary:  "CoachBate Batch Prompter",
                            detail:   "All prompts have been processed.",
                            life:     5000,
                        });
                    }
                    // Restore the number the user started this sequence with
                    // (leaving it past the end would make every subsequent Run
                    // hit "done" immediately).
                    _restoreSeqStart(this);
                }
            }

            app.graph.setDirtyCanvas(true, true);
        };
    },
});
