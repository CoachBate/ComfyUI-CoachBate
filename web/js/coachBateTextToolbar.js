// A find/replace toolbar that attaches under a node's multiline textarea.
//
//   ┌────────────────────────────────────────────────────┐
//   │ 📋 Copy  🔍 Find          ↶ Undo  ↷ Redo  [extras] │  always
//   │ [find…] ‹ › 3/17 Aa ab| [replace…] Replace All ✕ │  only while Find is on
//   └────────────────────────────────────────────────────┘
//
// Built for the workflow of editing long prompts in place instead of pasting
// them into a text editor to run a global search-and-replace.
//
// Undo/redo deliberately piggybacks on the BROWSER's own textarea undo history
// rather than a private snapshot stack: every edit is applied through
// document.execCommand("insertText"), which the browser records exactly as if
// the user had typed it. That means one unified history — Ctrl+Z walks back
// through typing and Replace All alike, in the real order they happened — and
// the native input event it fires is what keeps the Nodes 2.0 Vue model in
// sync. A private stack would fork into two histories that disagree.
// execCommand is formally deprecated but is still the only API that writes to a
// text field's native undo stack; setTextRangeText/`.value =` both wipe it.
// There's a direct-assignment fallback below for the day it stops working.

import { app } from "../../../scripts/app.js";
import {
    findWidgetTextarea,
    applyAdaptiveCanvasOnly,
    installNativeTextMenu,
    installCanvasZoomPassthrough,
    scrollIndexIntoView,
} from "./coachBateNodeDom.js";
import {
    findMatches,
    matchIndexAfter,
    matchIndexBefore,
    matchIndexAt,
    replaceAllIn,
} from "./text_tools/findReplace.js";

const CSS_ID = "cb-text-toolbar-css";
const REBIND_MS = 500;      // how often to look for a remounted textarea
const ROW_H = 25;           // fallback row height before the rows are measurable
const GAP = 3;              // .cbtt-root row gap, keep in sync with the CSS
const PAD_V = 11;           // .cbtt-root vertical padding - FALLBACK only; measure()
                            // reads the real value back out of the computed style
const MARGIN_FALLBACK = 10;  // widget.margin default; only used pre-mount
const RING = 2;             // slack for the 1px focus ring an input paints outside
                            // its border box, top and bottom

const CSS = `
/* Palette — one place to retune.

   The surface the buttons sit on is NOT a global theme token: it is the colour
   of the node they are mounted in, pushed in as --cbtt-surface by syncSurface()
   below.  A node the user has coloured green must not carry a grey toolbar, and
   a global token can't know what colour this particular node is.  Everything
   else (text, accent) still resolves through ComfyUI's theme tokens.

   Scoped to .cbtt-root, not :root — --cbtt-surface is PER NODE, so a global
   definition would make every toolbar take the colour of whichever node
   happened to write it last.

   ⚠️ No hex literals anywhere in these chains.  ComfyUI is themeable, so a
   literal fallback would survive a theme switch and be the one control painted
   in the wrong palette.  Every chain ends on a legacy --comfy-* / --fg-color
   variable that ComfyUI's own stylesheet always defines; tints are color-mix()
   over the same tokens, so they track too.
   ⚠️ NEVER use a backtick in this block: it is inside a template literal, and
   a stray one closes it early — the CSS after it then parses as code, which is
   how "--cbtt-x" once became a predecrement and the whole module failed to
   load in the browser while node --check still passed. */
.cbtt-root {
    /* --cbtt-surface is set per node by syncSurface(); the fallback only
       applies before the node's own colour has been read. */
    --cbtt-control-bg:       var(--cbtt-surface, var(--comfy-menu-bg));
    --cbtt-control-bg-hover: color-mix(in srgb, var(--cbtt-control-bg) 82%, var(--cbtt-text));
    --cbtt-field-bg:         color-mix(in srgb, var(--cbtt-control-bg) 70%,
                                       var(--comfy-input-bg, var(--p-surface-900)));
    --cbtt-line:             color-mix(in srgb, var(--cbtt-control-bg) 72%, var(--cbtt-text));
    --cbtt-text:             var(--p-text-color, var(--input-text, var(--fg-color)));
    --cbtt-muted:            var(--p-text-muted-color, var(--descrip-text, var(--fg-color)));
    --cbtt-accent:           var(--p-primary-color, var(--fg-color));

    display: flex; flex-direction: column; gap: 3px;
    align-content: flex-start;
    /* Extra bottom padding: without it the find row sits flush against the
       node's bottom edge and visibly bleeds through the rounded border. */
    width: 100%; box-sizing: border-box; padding: 3px 4px 8px;
    font: 11px sans-serif; color: var(--cbtt-text);
}
/* flex: 0 0 auto keeps each row at its natural height even when ComfyUI
   stretches .cbtt-root with an h-full class — that is what makes measure()
   below able to read a stable content height. */
.cbtt-row { display: flex; align-items: center; gap: 4px; width: 100%; flex: 0 0 auto; }
.cbtt-row.cbtt-hidden { display: none; }
.cbtt-hidden { display: none !important; }
/* Read-only: the node's text widget has been wired as an INPUT, so the text
   comes from the link and cannot be typed into.  Paint the whole toolbar the
   way ComfyUI paints any disabled control, so it says "not editable" at a
   glance instead of looking live but doing nothing. */
.cbtt-root.cbtt-readonly { opacity: 0.55; }
.cbtt-root.cbtt-readonly .cbtt-btn { cursor: default; }
.cbtt-spacer { flex: 1 1 auto; }

/* The node's prompt textarea is deliberately NOT restyled: it keeps ComfyUI's
   own default field look, which is what every other node's multiline widget
   has.  The .cbtt-field class is still applied in bindTo() as a marker (and a
   hook for future work) but carries no paint of its own. */

.cbtt-input {
    flex: 1 1 auto; min-width: 30px;
    background: var(--cbtt-field-bg); color: var(--cbtt-text);
    border: 1px solid var(--cbtt-line); border-radius: 4px;
    padding: 2px 6px; font: 11px sans-serif; box-sizing: border-box;
}
.cbtt-input::placeholder { color: var(--cbtt-muted); opacity: 1; }
.cbtt-input:focus {
    outline: none;
    border-color: var(--cbtt-accent);
    box-shadow: 0 0 0 1px var(--cbtt-accent);
}
.cbtt-btn {
    flex: 0 0 auto;
    /* Same colour as the node body, so the toolbar reads as part of the node
       rather than a grey strip pasted onto it.  That means the button needs a
       border to exist at all — it is mixed from the surface toward the text
       colour, so it stays visible on a dark grey node and on a green one. */
    background: var(--cbtt-control-bg); color: var(--cbtt-text);
    border: 1px solid var(--cbtt-line); border-radius: 4px;
    padding: 3px 8px; font: 11px sans-serif; line-height: 1.2;
    cursor: pointer; white-space: nowrap;
}
.cbtt-btn:hover:not(:disabled) { background: var(--cbtt-control-bg-hover); }
.cbtt-btn:active:not(:disabled) { filter: brightness(0.9); }
.cbtt-btn:disabled { opacity: 0.45; cursor: default; }
.cbtt-btn.cbtt-toggle-on {
    background: var(--cbtt-accent);
    border-color: var(--cbtt-accent);
    color: var(--p-primary-contrast-color, var(--cbtt-text));
}
.cbtt-nav { padding: 3px 7px; font-weight: bold; }
.cbtt-count {
    flex: 0 0 auto; min-width: 42px; text-align: center;
    color: var(--cbtt-muted); font-variant-numeric: tabular-nums;
}
.cbtt-count.cbtt-none { color: var(--p-red-400, var(--error-text, var(--cbtt-muted))); }
.cbtt-close { padding: 3px 7px; }
`;

function injectCss() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement("style");
    style.id = CSS_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
}

function mkBtn(label, title, className = "") {
    const b = document.createElement("button");
    b.className = `cbtt-btn ${className}`.trim();
    b.textContent = label;
    b.title = title;
    // Keep the caret and the highlighted match where they are: without this the
    // button takes focus on press and the textarea's selection stops rendering.
    b.addEventListener("mousedown", (e) => e.preventDefault());
    return b;
}

function mkInput(placeholder, title) {
    const i = document.createElement("input");
    i.type = "text";
    i.className = "cbtt-input";
    i.placeholder = placeholder;
    i.title = title;
    i.spellcheck = false;
    return i;
}

/**
 * Attach the toolbar to `node`'s multiline widget.
 *
 * @param {object} node                 the LiteGraph node
 * @param {string} widgetName           name of the multiline STRING widget
 * @param {object}  [opts]
 * @param {string}  [opts.title]        title used in toast notifications
 * @param {Array}   [opts.extraButtons] node-specific buttons appended to the
 *        right of row 1, each `{label, title, onClick}`.  They join the
 *        existing button family (same size/radius/font) instead of each node
 *        inventing its own stray control row — which is what the Batch
 *        Prompter's old full-width red Stop slab was.
 * @returns {() => void}                cleanup, for onRemoved
 */
export function attachTextToolbar(node, widgetName, opts = {}) {
    injectCss();
    const title = opts.title || node.title || "CoachBate";

    const toast = (severity, detail, life = 2500) => {
        try {
            app.extensionManager?.toast?.add({ severity, summary: title, detail, life });
        } catch (_) { /* toast service is optional */ }
    };

    // ── State ──────────────────────────────────────────────────────────────
    let caseSensitive = false;
    let wholeWord = false;
    let el = null;                  // the live <textarea>, or null until mounted
    let cache = null;               // memoised match list, see getMatches()

    const invalidate = () => { cache = null; };

    // ── DOM ────────────────────────────────────────────────────────────────
    const root = document.createElement("div");
    root.className = "cbtt-root";

    // Row 1 — always visible.
    const row1 = document.createElement("div");
    row1.className = "cbtt-row";
    const copyBtn = mkBtn("📋 Copy", "Copy the whole text to the clipboard");
    const findBtn = mkBtn("🔍 Find", "Show the find/replace bar (Ctrl+F)");
    const undoBtn = mkBtn("↶ Undo", "Undo the last edit (same history as Ctrl+Z in the box)");
    const redoBtn = mkBtn("↷ Redo", "Redo the last undone edit");
    const spacer1 = document.createElement("div");
    spacer1.className = "cbtt-spacer";
    row1.append(copyBtn, findBtn, spacer1, undoBtn, redoBtn);

    // Node-specific buttons (e.g. the Batch Prompter's Stop) go last, after the
    // spacer, so they sit at the right edge away from the text-editing controls.
    for (const spec of (opts.extraButtons ?? [])) {
        if (!spec?.label) continue;
        // No special colour: a button that shouts is a button that no longer
        // looks like it belongs to the node.  The label's own icon carries the
        // meaning (see the Batch Prompter's Stop).
        const b = mkBtn(spec.label, spec.title ?? spec.label);
        b.addEventListener("click", () => {
            try { spec.onClick?.(); } catch (err) {
                console.error("[CoachBate] toolbar button failed:", err);
            }
        });
        row1.append(b);
    }

    // Row 2 — the find/replace bar, hidden until 🔍 Find is clicked. Find and
    // replace share one row so the toolbar never costs more than two.
    const row2 = document.createElement("div");
    row2.className = "cbtt-row cbtt-hidden";
    const findInput = mkInput("find…", "Text to search for (literal, not a pattern)");
    const prevBtn = mkBtn("‹", "Previous match (Shift+Enter)", "cbtt-nav");
    const nextBtn = mkBtn("›", "Next match (Enter)", "cbtt-nav");
    const countEl = document.createElement("span");
    countEl.className = "cbtt-count";
    countEl.textContent = "0/0";
    const caseBtn = mkBtn("Aa", "Match case");
    const wordBtn = mkBtn("ab|", "Match whole word only");
    const replInput = mkInput("replace with…", "Replacement text — leave blank to delete each match");
    const replBtn = mkBtn("Replace", "Replace the highlighted match, then find the next");
    const replAllBtn = mkBtn("All", "Replace every match in one undoable step");
    const closeBtn = mkBtn("✕", "Hide the find/replace bar (Esc)", "cbtt-close");
    row2.append(findInput, prevBtn, nextBtn, countEl, caseBtn, wordBtn,
                replInput, replBtn, replAllBtn, closeBtn);

    root.append(row1, row2);

    const uninstallMenu = installNativeTextMenu(root);
    const uninstallWheel = installCanvasZoomPassthrough(root);

    // ── Textarea binding ───────────────────────────────────────────────────
    // Nodes 2.0 unmounts and remounts the textarea (node collapse/expand, tab
    // switches), so the element reference goes stale. Resolve lazily on every
    // action and re-poll slowly in the background to keep the counter live.

    const onTextareaInput = () => { invalidate(); refreshCount(); };

    const onTextareaKeydown = (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
            e.preventDefault();
            e.stopPropagation();
            setFindOpen(true);
        }
    };

    function bindTo(ta) {
        if (el === ta) return;
        if (el) {
            el.removeEventListener("input", onTextareaInput);
            el.removeEventListener("keydown", onTextareaKeydown);
            el.classList.remove("cbtt-field");
        }
        el = ta;
        if (el) {
            el.addEventListener("input", onTextareaInput);
            el.addEventListener("keydown", onTextareaKeydown);
            // Restyle onto the control surface - see textarea.cbtt-field above.
            el.classList.add("cbtt-field");
        }
    }

    function resolveTextarea() {
        if (el?.isConnected) return el;
        bindTo(findWidgetTextarea(node, widgetName));
        return el;
    }

    // ── Node colour ────────────────────────────────────────────────────────
    // The toolbar sits INSIDE the node body, so its buttons must be the colour
    // of that node — which is a per-node LiteGraph property (set from the
    // canvas Colors menu), not a theme token.  A node the user has turned green
    // carried a grey toolbar before this.
    //
    // In legacy canvas mode the node is painted on the canvas, so there is no
    // DOM ancestor to inherit from and node.bgcolor is the only source.  In
    // Nodes 2.0 the .lg-node element does carry it, but node.bgcolor is still
    // correct and works in both, so we read the graph, not the DOM.
    let lastSurface = null;
    function syncSurface() {
        const bg = node.bgcolor
            || window.LiteGraph?.NODE_DEFAULT_BGCOLOR
            || "";
        if (bg === lastSurface) return;
        lastSurface = bg;
        if (bg) root.style.setProperty("--cbtt-surface", bg);
        else root.style.removeProperty("--cbtt-surface");
    }
    syncSurface();

    // ── Read-only state ────────────────────────────────────────────────────
    // Connecting a link to the node's text widget converts it to an INPUT: the
    // textarea goes away entirely (the value now comes down the wire), so every
    // control here is inert.  Rather than leave live-looking buttons that do
    // nothing, mark the toolbar disabled and hide the controls that only make
    // sense when you can type — Replace and Replace All.
    let readOnly = null;   // null = not yet determined
    function isEditable() {
        // Decide from the GRAPH, not the DOM.  A converted widget shows up in
        // node.inputs carrying a `widget` descriptor; modern frontends keep that
        // socket around permanently, so it is the LINK that means "the value now
        // comes down a wire", not the socket's existence.
        const wired = (node.inputs ?? []).some(
            i => i?.widget?.name === widgetName && i.link != null);
        if (wired) return false;
        // A missing textarea means "not mounted yet" (Nodes 2.0 remounts it on
        // collapse, tab switch, first paint), NOT "not editable" — treating it
        // as read-only left the toolbar stuck disabled after a disconnect.
        const ta = resolveTextarea();
        if (ta && (ta.readOnly || ta.disabled)) return false;
        return true;
    }
    function syncReadOnly() {
        const editable = isEditable();
        if (readOnly === !editable) return;
        readOnly = !editable;
        root.classList.toggle("cbtt-readonly", readOnly);
        // Replace is meaningless without an editable field — hide it outright.
        for (const elm of [replInput, replBtn, replAllBtn]) {
            elm.classList.toggle("cbtt-hidden", readOnly);
        }
        for (const b of [copyBtn, findBtn, undoBtn, redoBtn]) b.disabled = readOnly;
        if (readOnly && findOpen) setFindOpen(false);
        refreshCount();
        // The row's content changed width/'height; re-ask for our height.
        try { node.setSize?.([node.size[0], node.size[1]]); } catch (_) {}
    }

    const rebindTimer = setInterval(() => {
        // Recolouring a node fires no event we can hook, so ride the poll that
        // already exists for textarea remounts.
        // Wiring the text widget as an input fires no event either, hence
        // syncReadOnly here too.
        syncSurface();
        syncReadOnly();
        if (el?.isConnected) return;
        if (resolveTextarea()) { invalidate(); refreshCount(); }
    }, REBIND_MS);

    // ── Widget value sync ──────────────────────────────────────────────────
    // execCommand fires a real input event, so the Nodes 2.0 Vue model updates
    // itself — but mirror the value onto the widget (and its state entry) the
    // same defensive way populate() does in coachBateTextPreviewEdit.js, so a
    // legacy canvas widget or an older frontend stays correct too.
    function syncWidget() {
        const w = node.widgets?.find(x => x.name === widgetName);
        if (!w || !el) return;
        const v = el.value;
        if (w.value !== v) w.value = v;
        try { if (w._state && w._state.value !== v) w._state.value = v; } catch (_) {}
    }

    // ── Matching ───────────────────────────────────────────────────────────
    function getMatches() {
        const text = el?.value ?? "";
        const query = findInput.value;
        if (cache
            && cache.text === text && cache.query === query
            && cache.caseSensitive === caseSensitive && cache.wholeWord === wholeWord) {
            return cache.matches;
        }
        cache = {
            text, query, caseSensitive, wholeWord,
            matches: findMatches(text, query, { caseSensitive, wholeWord }),
        };
        return cache.matches;
    }

    function refreshCount() {
        const hasQuery = findInput.value.length > 0;
        for (const b of [prevBtn, nextBtn, replBtn, replAllBtn]) {
            b.disabled = !hasQuery || readOnly === true;
        }

        if (!hasQuery) {
            countEl.textContent = "0/0";
            countEl.classList.remove("cbtt-none");
            return;
        }
        resolveTextarea();
        const ms = getMatches();
        // Derive "which one am I on" from the live selection rather than a
        // stored index, so it stays right after an edit or a manual click.
        const cur = el ? matchIndexAt(ms, el.selectionStart, el.selectionEnd) : -1;
        countEl.textContent = `${cur >= 0 ? cur + 1 : 0}/${ms.length}`;
        countEl.classList.toggle("cbtt-none", ms.length === 0);
    }

    function selectMatch(m) {
        if (!el) return;
        el.focus();
        el.setSelectionRange(m.start, m.end);
        scrollIndexIntoView(el, m.start);
        refreshCount();
    }

    /** Move to the next (or previous) match, wrapping at either end. */
    function step(backwards) {
        if (!resolveTextarea()) return;
        const ms = getMatches();
        if (!ms.length) { refreshCount(); return; }
        const selS = el.selectionStart ?? 0;
        const selE = el.selectionEnd ?? 0;
        const cur = matchIndexAt(ms, selS, selE);
        const idx = backwards
            ? matchIndexBefore(ms, cur >= 0 ? ms[cur].start : selS)
            : matchIndexAfter(ms, cur >= 0 ? ms[cur].end : selS);
        if (idx >= 0) selectMatch(ms[idx]);
    }

    // ── Editing ────────────────────────────────────────────────────────────
    /**
     * Replace [start,end) with `text` through the browser's own edit pipeline so
     * the change lands on the native undo stack. Falls back to direct assignment
     * (which does not) only if execCommand refuses.
     */
    function applyEdit(start, end, text) {
        el.focus();
        el.setSelectionRange(start, end);
        let ok = false;
        try { ok = document.execCommand("insertText", false, text); } catch (_) { ok = false; }
        if (!ok) {
            const v = el.value;
            el.value = v.slice(0, start) + text + v.slice(end);
            const caret = start + text.length;
            el.setSelectionRange(caret, caret);
            el.dispatchEvent(new Event("input", { bubbles: true }));
        }
        syncWidget();
        invalidate();
        return ok;
    }

    function replaceOne() {
        if (!resolveTextarea()) return;
        const ms = getMatches();
        if (!ms.length) { refreshCount(); return; }
        const cur = matchIndexAt(ms, el.selectionStart, el.selectionEnd);
        if (cur < 0) { step(false); return; }   // nothing highlighted yet → just find
        applyEdit(ms[cur].start, ms[cur].end, replInput.value);
        step(false);                            // advance from the new caret position
        refreshCount();
    }

    function replaceAll() {
        if (!resolveTextarea()) return;
        const query = findInput.value;
        if (!query) return;
        const { text, count } = replaceAllIn(
            el.value, query, replInput.value, { caseSensitive, wholeWord },
        );
        if (!count) { toast("info", `No matches for “${query}”.`); refreshCount(); return; }
        // One select-all + insert = one undo step for the whole operation.
        const scrollTop = el.scrollTop;
        applyEdit(0, el.value.length, text);
        el.scrollTop = scrollTop;
        refreshCount();
        toast("success", `Replaced ${count} occurrence${count === 1 ? "" : "s"}.`);
    }

    function nativeHistory(command) {
        if (!resolveTextarea()) return;
        el.focus();
        try { document.execCommand(command); } catch (_) { /* nothing to undo */ }
        syncWidget();
        invalidate();
        refreshCount();
    }

    async function copyAll() {
        const text = resolveTextarea()
            ? el.value
            : (node.widgets?.find(w => w.name === widgetName)?.value ?? "");
        try {
            await navigator.clipboard.writeText(text);
            toast("success", "Text copied to clipboard.");
        } catch {
            // Clipboard API needs a secure context; the execCommand path works
            // over plain http, which is how ComfyUI is usually reached on a LAN.
            try {
                const tmp = document.createElement("textarea");
                tmp.value = text;
                Object.assign(tmp.style, { position: "fixed", left: "-99999px" });
                document.body.appendChild(tmp);
                tmp.select();
                document.execCommand("copy");
                document.body.removeChild(tmp);
                toast("success", "Text copied to clipboard.");
            } catch (err) {
                console.error("[CoachBate] Copy Text failed:", err);
                toast("error", "Could not copy text to clipboard.", 4000);
            }
        }
    }

    // ── Wiring ─────────────────────────────────────────────────────────────
    copyBtn.addEventListener("click", copyAll);
    undoBtn.addEventListener("click", () => nativeHistory("undo"));
    redoBtn.addEventListener("click", () => nativeHistory("redo"));
    prevBtn.addEventListener("click", () => step(true));
    nextBtn.addEventListener("click", () => step(false));
    replBtn.addEventListener("click", replaceOne);
    replAllBtn.addEventListener("click", replaceAll);

    caseBtn.addEventListener("click", () => {
        caseSensitive = !caseSensitive;
        caseBtn.classList.toggle("cbtt-toggle-on", caseSensitive);
        invalidate();
        refreshCount();
    });
    wordBtn.addEventListener("click", () => {
        wholeWord = !wholeWord;
        wordBtn.classList.toggle("cbtt-toggle-on", wholeWord);
        invalidate();
        refreshCount();
    });

    findInput.addEventListener("input", () => { invalidate(); refreshCount(); });
    findInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); step(e.shiftKey); }
        else if (e.key === "Escape") { e.preventDefault(); setFindOpen(false); }
    });
    replInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); replaceOne(); }
        else if (e.key === "Escape") { e.preventDefault(); setFindOpen(false); }
    });
    findBtn.addEventListener("click", () => setFindOpen(!findOpen));
    closeBtn.addEventListener("click", () => setFindOpen(false));

    // Typing in our inputs must not reach the canvas, which treats bare letters
    // as shortcuts (and Delete/Backspace as "delete the selected node").
    for (const input of [findInput, replInput]) {
        input.addEventListener("keydown", (e) => e.stopPropagation());
        input.addEventListener("keyup", (e) => e.stopPropagation());
    }

    // ── Height ──────────────────────────────────────────────────────
    //
    // Sum the ROWS - never measure `root`.  ComfyUI stretches the root with an
    // h-full class, so root.scrollHeight reports whatever space it was just
    // handed; returning that as the requested height ratchets the toolbar up
    // until it has swallowed everything the textarea should have had.  That is
    // the "textarea shrinks to two lines while the node stays tall" bug.  The
    // rows are flex: 0 0 auto, so their own heights stay at content size no
    // matter how tall the root gets.
    const measure = () => {
        let h = 0;
        let shown = 0;
        for (const r of [row1, row2]) {
            if (r.classList.contains("cbtt-hidden")) continue;
            h += r.offsetHeight || ROW_H;
            shown++;
        }
        h += GAP * Math.max(0, shown - 1);
        // Read the padding back out of the computed style rather than trusting
        // the constant: the CSS is the source of truth, and a hardcoded copy
        // silently under-reports the moment someone retunes the padding - which
        // shows up as the bottom row bleeding through the node's border.
        let padV = PAD_V;
        try {
            const cs = getComputedStyle(root);
            const top = parseFloat(cs.paddingTop);
            const bottom = parseFloat(cs.paddingBottom);
            if (Number.isFinite(top) && Number.isFinite(bottom) && top + bottom > 0) {
                padV = top + bottom;
            }
        } catch (_) { /* unmounted - the constant is the right answer anyway */ }

        // ⚠️ ComfyUI does NOT hand the element the height we ask for.  From the
        // frontend's own DOM-widget layout:
        //     size = [width - margin*2, (computedHeight ?? 50) - margin*2]
        // so the element is our requested height MINUS twice the widget's
        // margin (10 by default → 20px).  Ask for exactly the rows and the row
        // overflows the shorter element and hangs out through the node's bottom
        // edge — that was "the buttons are outside the node".
        //
        // Read widget.margin rather than measuring the element: a measured gap
        // reads as huge whenever the host is collapsed (hidden tab, pre-layout),
        // and feeding that back as the requested height ratchets the toolbar
        // upward forever — the exact failure this file already warns about.
        const margin = Number(widget?.margin);
        const chrome = 2 * (Number.isFinite(margin) ? margin : MARGIN_FALLBACK);

        return h + padV + RING + chrome;
    };

    // ── Find bar open/close ───────────────────────────────────────────
    let findOpen = false;
    function setFindOpen(open) {
        findOpen = !!open;
        row2.classList.toggle("cbtt-hidden", !findOpen);
        findBtn.classList.toggle("cbtt-toggle-on", findOpen);
        // Our height just changed; make the node re-run its widget layout so
        // the row we gave back goes to the textarea (and vice versa).
        try {
            if (typeof node.setSize === "function" && Array.isArray(node.size)) {
                node.setSize([node.size[0], node.size[1]]);
            }
            node.setDirtyCanvas?.(true, true);
        } catch (_) { /* layout nudge is best-effort */ }
        if (findOpen) {
            findInput.focus();
            findInput.select();
            refreshCount();
        } else {
            resolveTextarea()?.focus();
        }
    }

    // eslint-disable-next-line prefer-const -- measure() closes over this before
    // the assignment below runs; it only reads it after mount.
    let widget = null;
    widget = node.addDOMWidget(
        `_cb_text_toolbar_${widgetName}`, "coachbate_text_toolbar", root,
        {
            serialize: false, getValue: () => null, setValue: () => {},
            getMinHeight: measure, getMaxHeight: measure, getHeight: measure,
        },
    );
    if (widget) {
        // A separate flag from options.serialize — this is the one LGraphNode
        // .serialize() actually checks (`if (w.serialize === false) continue`).
        widget.serialize = false;
        // Nodes 2.0 sizes through the CSS grid via computeLayoutSize.  The
        // DOMWidget default is
        //   {minHeight: getMinHeight?.(), maxHeight: getMaxHeight?.(), minWidth: 0}
        // and the layout consumer destructures BOTH minHeight and maxHeight - so
        // an override returning only minHeight leaves the track unbounded, and
        // the toolbar grows to eat the node's spare height instead of the
        // textarea getting it.  Always pin maxHeight too.
        // minWidth 1 so the node's saved WIDTH still round-trips.
        // Legacy canvas renderer: node layout reads widget.computeSize(), and
        // with none defined it fell back to LiteGraph's 20px default widget
        // height — so the toolbar was allotted 20px whatever getHeight said,
        // and its row spilled out of the node.  Nodes 2.0 uses
        // computeLayoutSize below; define both.
        widget.computeSize = (width) => [width ?? 0, measure()];
        widget.computeLayoutSize = () => {
            const h = measure();
            return { minHeight: h, maxHeight: h, minWidth: 1 };
        };
        applyAdaptiveCanvasOnly(widget);
    }

    // ⚠️ The toolbar MUST stay at the end of node.widgets — do not splice it in
    // under its textarea, however much better that looks.
    //
    // serialize() writes `widgets_values[n] = value` at each widget's ARRAY
    // INDEX and skips `serialize === false` by leaving a hole, while configure()
    // walks the widgets and consumes the value array SEQUENTIALLY, skipping
    // non-serializing widgets. Save and load therefore disagree about holes, so
    // a skipped widget anywhere but the tail shifts every value after it by one.
    //
    // Measured on frontend 1.51.9 with the toolbar spliced in at index 2 of
    // BatchPrompter, one save/load round-trip:
    //     append_text     "POST" → null
    //     starting_number 7      → "POST"   (a string in an INT widget)
    //     max_prompts     500    → 7
    // widgets_values_named does not rescue it — the named map is written from
    // the same holed array. That "Failed to convert an input value to a INT
    // value: job_total, None" failure is documented in CLAUDE.md; this is how
    // you cause it. Trailing skipped widgets (_cb_status_display) are
    // harmless because nothing follows them.

    refreshCount();

    // ── Cleanup ────────────────────────────────────────────────────────────
    return () => {
        clearInterval(rebindTimer);
        bindTo(null);
        uninstallMenu();
        uninstallWheel();
        root.remove();
    };
}
