import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { attachNumberedGutter } from "./coachBateGutter.js";
import { attachTextToolbar } from "./coachBateTextToolbar.js";

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

// Resolve a node's DIRECT upstream providers — the nodes feeding its inputs.
// Mirror image of _outputConsumers, used to answer "is this node an ancestor of
// the nodes the user asked to run?".
function _inputProviders(node) {
    const providers = [];
    try {
        const graph = node.graph ?? app.graph;
        for (const input of (node.inputs ?? [])) {
            const linkId = input?.link;
            if (linkId == null) continue;
            const link = graph?.links?.[linkId] ?? graph?.links?.get?.(linkId);
            if (!link) continue;
            const origin = graph.getNodeById?.(link.origin_id);
            if (origin) providers.push(origin);
        }
    } catch (err) {
        console.warn("[CoachBate] Failed to resolve input providers:", err);
    }
    return providers;
}

// "Run selected nodes" (partial execution) passes the selected node ids to
// app.queuePrompt; the backend then executes those nodes plus everything
// upstream of them.  Returns the full set of node ids (as strings) that such a
// run will actually touch, so we can tell whether a Batch Prompter is one of
// them.  Null targets = a normal full run, where everything is in scope.
function _executionScopeIds(targetIds) {
    if (!targetIds?.length) return null;
    const graph = app.graph;
    const scope = new Set();
    const stack = [];
    for (const id of targetIds) {
        const n = graph?.getNodeById?.(id);
        if (n) stack.push(n);
    }
    while (stack.length) {
        const n = stack.pop();
        const key = String(n.id);
        if (scope.has(key)) continue;   // cycle/diamond guard
        scope.add(key);
        stack.push(..._inputProviders(n));
    }
    return scope;
}

// Is this node one ComfyUI will actually EXECUTE on its own — a SaveImage,
// PreviewImage, VHS_VideoCombine, our own CoachBateVideoCombine, and so on?
// ComfyUI only runs output nodes and whatever they depend on, so this is the
// question that decides whether a chain does anything at all.
//
// This mirrors the frontend's own helper, which is
//     const isOutputNode = (node) => node.constructor.nodeData?.output_node
// (in the minified bundle; internal, with no stable path to import from, hence
// the copy).  The LiteGraph registry lookup is the fallback for the window
// before a node's constructor is wired up, and both were confirmed live to
// agree: PreviewImage true, PrimitiveString false.
function _isOutputNode(node) {
    const ctorDef = node?.constructor?.nodeData;
    if (ctorDef && typeof ctorDef.output_node === "boolean") return ctorDef.output_node;
    const type = node?.type ?? node?.comfyClass;
    const def  = window.LiteGraph?.registered_node_types?.[type]?.nodeData;
    if (def && typeof def.output_node === "boolean") return def.output_node;
    return !!node?.isOutputNode;
}

// True when this frontend exposes output_node at all.  If it doesn't, the
// reachability test below can't work and would answer "nothing ever executes",
// silently disabling every fan-out — so we fall back to the old, looser rule.
function _outputFlagAvailable() {
    const reg = window.LiteGraph?.registered_node_types;
    if (!reg) return false;
    for (const key of ["SaveImage", "PreviewImage"]) {
        if (typeof reg[key]?.nodeData?.output_node === "boolean") return true;
    }
    return false;
}

// A BatchPrompter only drives a run if its prompts can reach a node that will
// actually execute — i.e. an OUTPUT node downstream of it.
//
// ⚠️ Reaching *a consumer* is not enough, which is what this used to test.  A
// Batch Prompter feeding, say, a Load Image (Path) whose own IMAGE output goes
// nowhere has an active consumer but a dead chain: ComfyUI executes output
// nodes and their ancestors only, so nothing in that branch ever runs.  Testing
// one hop made a Run press fan the whole batch out over a branch that produced
// nothing.
//
// Mute vs bypass, walking downstream:
//   • muted (mode 2)    — the chain dies here, stop.
//   • bypassed (mode 4) — a pass-through: keep walking past it, but it cannot
//                         itself be the output node that justifies the run.
function _feedsActiveConsumer(node) {
    const strict = _outputFlagAvailable();
    const seen   = new Set([node.id]);
    const stack  = _outputConsumers(node);
    while (stack.length) {
        const n = stack.pop();
        if (!n || seen.has(n.id)) continue;   // cycle/diamond guard
        seen.add(n.id);
        if (n.mode === MODE_MUTED) continue;                    // chain dies here
        if (n.mode !== MODE_BYPASSED) {
            if (!strict) return true;                           // legacy fallback
            if (_isOutputNode(n)) return true;                  // this one runs
        }
        stack.push(..._outputConsumers(n));                     // keep walking
    }
    return false;
}

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

// Hide an internal widget in BOTH renderers.
//
// Legacy canvas honours `widget.hidden` (LGraphNode.isWidgetVisible /
// getLayoutWidgets) plus the zero-height computeSize the callers set.
//
// Nodes 2.0 reads neither of those.  Its Vue node body snapshots each widget
// through extractWidgetDisplayOptions(), which only looks at
// `widget.options.hidden` / `.advanced` / `.canvasOnly`, and isWidgetVisible()
// then drops anything with options.hidden set (frontend 1.52.x; the core
// Painter / CreateBoundingBoxes nodes hide their widgets the same way).
// The earlier `type = "hidden"` hack did nothing there — shouldRenderAsVue()
// only excludes canvasOnly widgets, so a type:"hidden" widget still rendered.
// `options` is the same object the widget-value store holds (BaseWidget copies
// the reference into _state), so mutating it in place is seen on the next
// render without any re-registration.  Value still serializes: serialization
// gates on `serialize !== false`, so job_index/job_total still reach Python.
function _hideInternalWidget(widget) {
    if (!widget) return;
    widget.hidden = true;
    widget.options ??= {};
    widget.options.hidden = true;
}

// Force job_index / job_total to real integers so they can never serialize as
// null.  ComfyUI's prompt validator runs int(value) on every widget input
// before execute; a null (from an old saved workflow whose widget values
// shifted) throws "Failed to convert an input value to a INT value".  Both are
// auto-managed internals (set by _queueBatch during fan-out, defaulted to 0
// otherwise), so clamping a bad value to 0 is always safe.
function _sanitizeJobWidgets(node) {
    for (const name of ["job_index", "job_total"]) {
        const w = node.widgets?.find(w => w.name === name);
        if (!w) continue;
        const n = Number(w.value);
        if (!Number.isFinite(n)) {
            w.value = 0;
        } else if (n !== w.value) {
            w.value = n;   // normalize e.g. "3" → 3
        }
    }
    // Same hazard for newline_delimiter: it was appended after workflows were
    // already saved, so an older workflow can restore it as undefined/null.
    // Coerce to its default (true = one prompt per line) rather than letting a
    // null reach the BOOLEAN validator.
    const nd = node.widgets?.find(w => w.name === "newline_delimiter");
    if (nd && nd.value !== true && nd.value !== false) {
        nd.value = !(nd.value === "false");
    }
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

// Like _boolWidget but falls back to `dflt` when the widget is missing or its
// value isn't a recognizable boolean.  Needed for widgets appended after
// workflows were already saved: ComfyUI restores widget values positionally,
// so an older workflow simply has nothing for the new slot — and defaulting
// such a node to the non-default mode would silently change its behaviour.
function _boolWidgetOr(node, name, dflt) {
    const w = node.widgets?.find(w => w.name === name);
    if (!w) return dflt;
    if (w.value === true  || w.value === "true")  return true;
    if (w.value === false || w.value === "false") return false;
    return dflt;
}

// True when prompts are delimited by newlines (default); false = "||".
const _newlineMode = node => _boolWidgetOr(node, "newline_delimiter", true);

// The prompt to highlight in the gutter: in sequential mode the widget holds
// the ordinal of the prompt that is RUNNING (it's advanced only after the run
// completes), so highlighting it tracks the active job.  Queue-all-at-once has
// no single active prompt -- -1 means "highlight nothing".
function _activeOrdinal(node) {
    if (_boolWidget(node, "queue_all_at_once")) return -1;
    const v = node.widgets?.find(w => w.name === "starting_number")?.value;
    return typeof v === "number" ? v : -1;
}

// The double-pipe prompt delimiter used when newline_delimiter is OFF.
const PIPE_DELIM = "||";

// Split `text` into numbered prompt segments.  Mirrors the split Python does
// in CoachBateBatchPrompter.execute() — keep the two in sync.
//
// Each entry is { ordinal, line, endLine }:
//   ordinal — 1-based prompt number (whitespace-only segments don't count),
//             the number shown in the gutter and held by starting_number
//   line    — index of the textarea line the segment's first non-blank
//             character sits on (where the gutter number is drawn)
//   endLine — index of the line its last non-blank character sits on (equal
//             to `line` in newline mode; used to size the active highlight)
function _promptSegments(text, newlineMode) {
    const src  = text ?? "";
    const segs = [];
    let ordinal = 0;

    if (newlineMode) {
        const lines = src.split("\n");
        for (let i = 0; i < lines.length; i++) {
            if (!lines[i].trim()) continue;
            segs.push({ ordinal: ++ordinal, line: i, endLine: i });
        }
        return segs;
    }

    // Double-pipe mode: a segment can span any number of lines, so map its
    // trimmed start/end character offsets back to line indices.  Counting
    // newlines in the text before an offset gives that offset's line index.
    const lineOf = off => {
        let n = 0;
        for (let i = 0; i < off && i < src.length; i++) if (src[i] === "\n") n++;
        return n;
    };

    let pos = 0;
    for (const part of src.split(PIPE_DELIM)) {
        if (part.trim()) {
            const lead  = part.length - part.trimStart().length;
            const trail = part.length - part.trimEnd().length;
            segs.push({
                ordinal: ++ordinal,
                line:    lineOf(pos + lead),
                // -1: land on the last non-blank character itself, not the
                // position just past it (which may already be a newline).
                endLine: lineOf(pos + part.length - trail - 1),
            });
        }
        pos += part.length + PIPE_DELIM.length;
    }
    return segs;
}

// 1-based prompt ordinals at/after startIndex — the same numbering as the
// gutter and Python's starting_number.
function _promptOrdinals(text, startIndex, newlineMode = true) {
    return _promptSegments(text, newlineMode)
        .map(s => s.ordinal)
        .filter(o => o >= startIndex);
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

// ── Theme colours for the canvas-drawn status readout ────────────────────────
// A 2D canvas can't take `var(--token)`, so resolve the same ComfyUI theme
// variables the DOM widgets use and hand the canvas the computed value.  No hex
// literals: ComfyUI is themeable and a literal would be the one thing on the
// node that ignores the active theme.  Cached briefly — getComputedStyle on
// every canvas repaint is not free, but the cache has to expire or switching
// theme would leave the readout in the old palette until a reload.
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

// ── Ownership of queued jobs ─────────────────────────────────────────────────
// Stop must cancel ONLY what this node put on the queue — clearing the whole
// queue also killed jobs the user had queued from elsewhere.  Every prompt we
// submit is recorded here by its server-assigned prompt_id: the bulk path reads
// it out of its own /prompt response, and the sequential path (which goes
// through ComfyUI's own queue machinery) gets it via the api.queuePrompt patch
// below.  Ids are dropped again as their jobs finish, so the set only ever
// holds jobs that are still pending or running.
function _ownedIds(node) {
    if (!node._cb_queued_ids) node._cb_queued_ids = new Set();
    return node._cb_queued_ids;
}

function _forgetPromptId(promptId) {
    if (!promptId) return;
    for (const node of (app.graph?.nodes ?? [])) {
        node._cb_queued_ids?.delete(promptId);
    }
}

// Set for the duration of the one app.queuePrompt call a sequential run makes,
// so the prompt_id the API hands back can be attributed to that node.
let _cbAttributeTo = null;

(function patchApiQueuePrompt() {
    if (typeof api.queuePrompt !== "function" || api._cb_queue_patched) return;
    api._cb_queue_patched = true;
    const orig = api.queuePrompt.bind(api);
    api.queuePrompt = async function (...args) {
        const target = _cbAttributeTo;
        const res = await orig(...args);
        if (target && res?.prompt_id) _ownedIds(target).add(res.prompt_id);
        return res;
    };
})();

// ── Shared stop helper ────────────────────────────────────────────────────────
async function _stopBatch(node) {
    try {
        const ids = [..._ownedIds(node)];

        if (ids.length) {
            // Dequeue OUR pending jobs first, so nothing of ours can start in
            // the gap, then interrupt — the /interrupt prompt_id form is a
            // no-op unless that exact prompt is the one currently running, so
            // someone else's job on the same queue is never touched.
            await fetch("/queue", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ delete: ids }),
            });
            for (const id of ids) {
                await fetch("/interrupt", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ prompt_id: id }),
                });
            }
            _ownedIds(node).clear();
        }

        // Stop sequential self-advance as well as bulk-queue mode.
        _cancelSeqTimer(node);
        _cancelPendingAdvance(node);
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
            detail:   (ids.length
                ? `Cancelled ${ids.length} job${ids.length !== 1 ? "s" : ""} queued by this node.`
                : "Nothing queued by this node was still pending — batch stopped locally.")
                + (autoOff ? "" : " If ComfyUI's native Auto Queue is on, disable it manually too."),
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
async function _queueBatch(node, targetIds = null) {
    // Serialize re-entrancy: only one posting pass at a time.  The guard is
    // cleared in the finally the moment posting ends, so a subsequent Run can
    // never be permanently suppressed the way the old long-lived flag was.
    if (_cbPosting) {
        console.debug("[CoachBate] _queueBatch ignored — already posting a batch");
        return;
    }
    _cbPosting = true;
    try {
        return await _queueBatchInner(node, targetIds);
    } finally {
        _cbPosting = false;
    }
}

async function _queueBatchInner(node, targetIds = null) {
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
    const blockOrdinals = _promptOrdinals(text, startIndex, _newlineMode(node));
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
                    // Preserve a "Run selected nodes" press: the backend runs
                    // only these nodes and their ancestors, exactly as it would
                    // for the un-fanned-out run this replaced.
                    ...(targetIds?.length && { partial_execution_targets: targetIds }),
                }),
            });
            if (resp.ok) {
                queued++;
                // Remember the id so Stop can cancel exactly these jobs.
                try {
                    const body = await resp.json();
                    if (body?.prompt_id) _ownedIds(node).add(body.prompt_id);
                } catch (_) { /* older server, or a non-JSON body */ }
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

// ── End of a prompt's execution: run any deferred sequential advance ─────────
// onExecuted fires when the Batch Prompter NODE runs, which is near the START
// of a long graph — far too early to advance to the next prompt (see the
// comment in onExecuted).  The advance is parked on the node and fired here,
// when the whole prompt has actually finished.  ComfyUI signals that with
// "execution_success" on modern frontends and with an "executing" event whose
// node is null on older ones; whichever arrives first wins, and the callback is
// cleared before being invoked so it can never fire twice.
function _runPendingAdvances() {
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        const pending = node._cb_pendingAdvance;
        if (!pending) continue;
        node._cb_pendingAdvance = null;
        try {
            pending();
        } catch (err) {
            console.error("[CoachBate] BatchPrompter: sequential advance failed:", err);
        }
    }
}

api.addEventListener("execution_success", ({ detail }) => {
    _forgetPromptId(detail?.prompt_id);
    _runPendingAdvances();
});
api.addEventListener("executing", ({ detail }) => {
    // detail is the executing node id (or, on some versions, an object with
    // one); null/undefined means "nothing left executing" = prompt finished.
    const nodeId = (detail && typeof detail === "object") ? detail.node : detail;
    if (nodeId == null) _runPendingAdvances();
});

// Drop a parked advance without running it — the sequence is over.  Called on
// stop / interrupt / error, all of which can land between the node executing
// and the prompt finishing.
function _cancelPendingAdvance(node) {
    node._cb_pendingAdvance = null;
}

// ── Reset display on interrupt / error ────────────────────────────────────────
api.addEventListener("execution_interrupted", ({ detail }) => {
    _forgetPromptId(detail?.prompt_id);
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        _cancelSeqTimer(node);
        _cancelPendingAdvance(node);
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

api.addEventListener("execution_error", ({ detail }) => {
    _forgetPromptId(detail?.prompt_id);
    for (const node of (app.graph?.nodes ?? [])) {
        if (node.type !== "CoachBateBatchPrompter") continue;
        _cancelSeqTimer(node);
        _cancelPendingAdvance(node);
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
    // Backstop for the deferred sequential advance, in case a frontend emits
    // neither "execution_success" nor a null "executing".  An empty queue can
    // only mean the prompt finished, and _runPendingAdvances clears each
    // callback before invoking it, so this can never double-fire.
    _runPendingAdvances();
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

                // "Run selected nodes": app.queuePrompt(number, batchCount,
                // opts) carries the selected node ids, either as a bare array
                // or as opts.queueNodeIds (both shapes exist across frontend
                // versions — the store does `Array.isArray(n) ? {queueNodeIds:
                // n} : n`).  Only the selection and everything upstream of it
                // actually executes, so a Batch Prompter outside that scope
                // must NOT fan the run out into one job per prompt — it isn't
                // running at all.  Without this check, selecting three
                // unrelated nodes and pressing Run fired the whole batch.
                const qOpts = args[2];
                const targetIds = Array.isArray(qOpts)
                    ? qOpts
                    : (qOpts?.queueNodeIds ?? null);
                const scope = _executionScopeIds(targetIds);

                const allBatch = (app.graph?.nodes ?? []).filter(
                    n => n.type === "CoachBateBatchPrompter"
                        && (!scope || scope.has(String(n.id)))
                );

                // A node only DRIVES a fan-out if it's both:
                //   • active   — not muted (mode 2) and not bypassed (mode 4)
                //   • wired up — an output feeds a consumer that is itself active
                // A node whose output goes nowhere, or only into muted/bypassed
                // nodes, drives no iteration.  It does NOT suppress the run:
                // when nothing drives, we fall through to orig() so the workflow
                // executes exactly as if this node's fan-out logic weren't here.
                // Suppressing the queue would break every other output branch.
                const activeBatch = allBatch.filter(_isActive);
                // With "Run selected nodes", being inside the execution scope
                // already proves the node runs, so the output-node reachability
                // rule (which assumes a normal full run) must not apply.
                const driving     = scope
                    ? activeBatch
                    : activeBatch.filter(_feedsActiveConsumer);

                if (activeBatch.length > 0 && driving.length === 0) {
                    console.debug(
                        "[CoachBate] No Batch Prompter output reaches an output node — " +
                        "skipping fan-out, running the workflow normally."
                    );
                }

                // Fan a normal Queue press out into one job per prompt block,
                // OR (when queue_all_at_once is false) fall through to a normal
                // single run so onExecuted can advance starting_number one prompt
                // at a time, exactly like ShotLoader's Auto Queue pattern.
                if (driving.length === 1) {
                    const node = driving[0];
                    // Use _boolWidget (checks for true/"true" only) so an unset/null
                    // value doesn't silently default to queue-all — it defaults to sequential.
                    if (_boolWidget(node, "queue_all_at_once")) return _queueBatch(node, targetIds);
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
                            const order = _shuffle(
                                _promptOrdinals(text, node._cb_seq_start, _newlineMode(node)));
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

                // Attribute the prompt_id of a sequential run to its node, so
                // Stop can cancel just that job (see _ownedIds).
                if (driving.length === 1 && !_boolWidget(driving[0], "queue_all_at_once")) {
                    _cbAttributeTo = driving[0];
                    try {
                        return await orig(...args);
                    } finally {
                        _cbAttributeTo = null;
                    }
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

            // ── Numbered gutter ──────────────────────────────────────────
            // Shared with the Numbered Text node (coachBateGutter.js).  This
            // node supplies its own segments: a prompt can span several lines
            // in "||" mode, and the one that's running is marked active so the
            // gutter highlights it.
            // ⚠️ Each attach is isolated: an exception in one of these overlays
            // used to abort the whole of onNodeCreated, so the node came up with
            // no gutter, no toolbar AND job_index/job_total unhidden — one bug
            // presenting as three. Never let a decoration take the node down.
            try {
            this._cb_gutter_cleanup = attachNumberedGutter(this, "multiline_text", {
                segments: text => {
                    const act = _activeOrdinal(self);
                    return _promptSegments(text, _newlineMode(self))
                        .map(seg => (seg.ordinal === act ? { ...seg, active: true } : seg));
                },
                // starting_number advances between sequential runs and the
                // delimiter toggle renumbers everything -- neither fires a
                // textarea event, so the gutter has to be told to re-check.
                stateKey: () => `${_activeOrdinal(self)}|${_newlineMode(self)}`,
            });
            } catch (err) {
                console.error("[CoachBate] BatchPrompter: gutter attach failed:", err);
            }

            // Find/replace toolbar for multiline_text.  It renders at the BOTTOM
            // of the node, not under the textarea: a non-serializing widget
            // anywhere but the tail of node.widgets corrupts every value after
            // it on save/load — see the warning in coachBateTextToolbar.js.
            try {
            this._cb_toolbar_cleanup = attachTextToolbar(this, "multiline_text", {
                title: "CoachBate Batch Prompter",
                // Stop lives IN the toolbar rather than in a row of its own.
                // It used to be a full-width red slab in its own DOM widget,
                // which cost a whole extra row and matched nothing else on the
                // node; as a tinted member of the toolbar's button family it
                // reads as the same kind of control as Copy/Find/Undo.
                extraButtons: [{
                    label:  "🛑 Stop",
                    title:  "Cancel the jobs this Batch Prompter queued",
                    onClick: async () => {
                        const owned = self._cb_queued_ids?.size ?? 0;
                        const ok = await app.extensionManager.dialog.confirm({
                            title:   "Stop batch",
                            message: owned
                                ? `Cancel the ${owned} job${owned !== 1 ? "s" : ""} queued by this Batch Prompter? Jobs queued elsewhere are left alone.`
                                : "Nothing queued by this node is still pending. Stop the batch locally anyway?",
                            type:    "delete",
                        });
                        if (ok) _stopBatch(self);
                    },
                }],
            });
            } catch (err) {
                console.error("[CoachBate] BatchPrompter: toolbar attach failed:", err);
            }

            this._cb_addStatusWidget();


            // ── Hide auto-managed optional inputs ────────────────────────────
            // job_index and job_total are set by _queueBatch and should never
            // be user-visible.  Collapsing them to zero height keeps them in
            // the serialised workflow (so Python receives the values) while
            // removing them from the visual widget list.
            for (const name of ["job_index", "job_total"]) {
                const w = this.widgets?.find(w => w.name === name);
                if (w) {
                    _hideInternalWidget(w);           // hidden + options.hidden
                    w.computeSize = () => [0, -4];    // legacy canvas renderer
                }
            }

            _sanitizeJobWidgets(this);
        };

        // ── Repair job_index / job_total after a saved workflow restores ──────
        // ComfyUI restores widget values positionally.  A workflow saved under
        // an older widget order (e.g. before `randomize` was appended) can shift
        // values so job_index / job_total land on null — and ComfyUI's prompt
        // validator does int(null) on the way to execute, which throws
        // "Failed to convert an input value to a INT value: job_total, None".
        // onConfigure runs AFTER values are restored, so coerce them here.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            _sanitizeJobWidgets(this);
        };

        // Tear down the gutter and toolbar when the node is removed.
        const origOnRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            origOnRemoved?.apply(this, arguments);
            _cancelSeqTimer(this);
            _cancelPendingAdvance(this);
            this._cb_autoqueue = false;
            this._cb_toolbar_cleanup?.();
            this._cb_gutter_cleanup?.();
        };

        // ── Status display widget (sits in natural widget flow) ──────────────
        nodeType.prototype._cb_addStatusWidget = function () {
            const self = this;

            const sw = this.addWidget("button", "_cb_status_display", "", () => {});
            sw.serialize  = false;

            // Nodes 2.0 never calls the canvas `draw` below, so the status
            // readout can't render there — and the raw "button" widget was
            // leaking into the UI as an empty "_cb_status_display" field.  Hide
            // it in both renderers (progress still surfaces via toasts).
            _hideInternalWidget(sw);

            sw.draw = function (ctx, node, widgetWidth, y, h) {
                const val  = self._cb_display;
                // Same token chains as the toolbar/gutter stylesheets.
                const fill = _themeColor("--p-surface-900", "--comfy-input-bg");
                const text = _themeColor("--p-text-color", "--input-text", "--fg-color");
                const done = _themeColor("--p-green-400", "--p-primary-color");
                const busy = _themeColor("--p-amber-400", "--p-primary-color");
                ctx.save();
                if (!val) {
                    ctx.globalAlpha = 0.35;
                    ctx.fillStyle = fill;
                    ctx.fillRect(4, y + 1, widgetWidth - 8, h - 2);
                    ctx.restore();
                    return;
                }
                const lines = val.split("\n");
                ctx.fillStyle = fill;
                ctx.fillRect(4, y + 1, widgetWidth - 8, h - 2);
                ctx.fillStyle = self._cb_is_last ? done : busy;
                ctx.fillRect(4, y + 1, 3, h - 2);
                ctx.font      = "bold 11px monospace";
                ctx.fillStyle = text;
                ctx.fillText(lines[0] ?? "", 12, y + 15);
                if (lines[1]) ctx.fillText(lines[1], 12, y + 30);
                if (self._cb_is_last && self._cb_remaining === 0) {
                    ctx.font      = "bold 10px monospace";
                    ctx.fillStyle = done;
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

                // ⚠️ Everything below is DEFERRED to the end of this prompt's
                // execution (see _runPendingAdvances) instead of running here.
                // onExecuted fires when THIS NODE runs — for a Batch Prompter
                // feeding a long graph that's seconds-to-minutes before the job
                // it belongs to actually finishes. Advancing here moved
                // starting_number to the NEXT prompt while the current one was
                // still sampling, so the node (and the gutter highlight) read
                // "prompt 2" for the whole of prompt 1's run — it looked like
                // prompt 1 had been skipped — and it queued the next job early,
                // contradicting what sequential mode promises: that each job
                // fully finishes before the next fires, so you can tweak the
                // workflow in between.
                this._cb_pendingAdvance = () => {
                    // The sequence may have been stopped/interrupted/errored
                    // between the node running and the prompt finishing.
                    if (!this._cb_seq_active) return;

                    // max_prompts caps the whole sequence: each self-advanced
                    // run opens a fresh window in Python, so the cap must be
                    // counted here across runs. _cb_seq_count is reset by a
                    // manual Run press in the queuePrompt patch.
                    this._cb_seq_count = (this._cb_seq_count ?? 0) + 1;
                    const maxP = this.widgets?.find(x => x.name === "max_prompts")?.value ?? Infinity;
                    const capReached = this._cb_seq_count >= maxP;

                    const w = this.widgets?.find(x => x.name === "starting_number");
                    const setStart = (v) => {
                        if (!w) return;
                        w.value = v;
                        try { w.callback?.(w.value); } catch (_) { /* ignore */ }
                    };

                    // Randomize mode: the shuffled order built at Run time
                    // replaces Python's linear next_start_index / has_more.
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

                        // Self-advance: queue the next single run directly,
                        // exactly as ShotLoader does — no reliance on Auto
                        // Queue mode.
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
                        // Finished — this run WAS the last prompt in the text
                        // (or Python reported done). Crucially, we must NOT
                        // queue one more run just to hit Python's "batch
                        // finished" branch: that run outputs an empty string
                        // that errors downstream.
                        if (!done) {
                            this._cb_display = "Done — all prompts processed";
                            app.extensionManager?.toast?.add({
                                severity: "success",
                                summary:  "CoachBate Batch Prompter",
                                detail:   "All prompts have been processed.",
                                life:     5000,
                            });
                        }
                        // Restore the number the user started this sequence
                        // with (leaving it past the end would make every
                        // subsequent Run hit "done" immediately).
                        _restoreSeqStart(this);
                    }
                    app.graph.setDirtyCanvas(true, true);
                };
            }

            app.graph.setDirtyCanvas(true, true);
        };
    },
});
