// Shared DOM helpers for CoachBate widgets that live inside a node body.
//
// These are the bits every in-node DOM widget needs to behave in BOTH renderers
// (legacy canvas and Nodes 2.0 / Vue). They were duplicated across
// coachBateGutter.js and coachBateBatchPrompter.js; this is the one copy.
//
// ⚠️ Several of these touch minified-internal frontend surfaces
// (LiteGraph.vueNodesMode, [data-node-id], widget.options.canvasOnly).
// Re-verify on comfyui_frontend_package upgrades. Written against 1.51.9.

import { app } from "../../../scripts/app.js";

/** True when ComfyUI renders nodes via the Vue/DOM ("Nodes 2.0") pipeline. */
export const isVueNodes = () => !!window.LiteGraph?.vueNodesMode;

/**
 * Adaptive `canvasOnly` for internal DOM widgets.
 *
 * `canvasOnly: true` keeps a widget out of the legacy right-sidebar Parameters
 * tab, but Nodes 2.0 reads `shouldRenderAsVue = !options.canvasOnly` and would
 * then drop the widget from the node body entirely. A live getter gives each
 * renderer the answer it needs, re-evaluated on every render so toggling the
 * renderer at runtime is honoured.
 *
 * Call AFTER addDOMWidget; don't also pass `canvasOnly` in the options literal.
 */
export function applyAdaptiveCanvasOnly(widget) {
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

/**
 * Locate the live <textarea> element backing a node's multiline widget.
 *
 * Nodes 2.0: the node is a real DOM element tagged [data-node-id]; we count how
 *   many textarea-producing widgets precede `widgetName` to index into it (a
 *   node can have several multiline inputs — BatchPrompter does).
 * Legacy canvas: the widget object itself points at the injected element.
 * Last resort: scan the document for a textarea whose value matches.
 *
 * Returns null when the element isn't mounted yet — callers are expected to
 * retry (Vue mounts asynchronously, and remounts on collapse/expand).
 *
 * @param {object}  node
 * @param {string}  widgetName
 * @param {{skipOwned?:boolean, skipNoGutter?:boolean}} [opts] filters for the
 *        value-match fallback only: skip elements another overlay has claimed
 *        (`_cb_owned`) and/or those flagged `data-cb-no-gutter`.
 */
export function findWidgetTextarea(node, widgetName, opts = {}) {
    const { skipOwned = false, skipNoGutter = false } = opts;
    const tw = node?.widgets?.find(w => w.name === widgetName);

    // ── Nodes 2.0 path ─────────────────────────────────────────────────────
    const nodeEl = document.querySelector(`[data-node-id="${node?.id}"]`);
    if (nodeEl) {
        let taIdx = 0;
        const widgets = node.widgets ?? [];
        for (const w of widgets) {
            if (w.name === widgetName) {
                const all = nodeEl.querySelectorAll("textarea");
                if (all[taIdx]) return all[taIdx];
                break;
            }
            if (w.type === "customtext" || w.type === "textarea" || w.options?.multiline) {
                taIdx++;
            }
        }
        // Single-textarea fallback (covers most practical cases)
        const first = nodeEl.querySelector("textarea");
        if (first) return first;
    }

    if (!tw) return null;

    // ── Legacy path ────────────────────────────────────────────────────────
    for (const c of [tw.inputEl, tw.element, tw.input, tw.domElement, tw.dom]) {
        if (!c) continue;
        if (c.tagName === "TEXTAREA") return c;
        const inner = c.querySelector?.("textarea");
        if (inner) return inner;
    }

    // ── Legacy last resort: match by current widget value ──────────────────
    const want = tw.value ?? "";
    for (const ta of document.querySelectorAll("textarea")) {
        if (skipOwned && ta._cb_owned) continue;
        if (skipNoGutter && ta.dataset?.cbNoGutter) continue;
        if (ta.value === want) return ta;
    }
    return null;
}

/**
 * Keep the BROWSER's right-click menu (Cut/Copy/Paste) over text fields inside
 * a node body.
 *
 * Both renderers handle `contextmenu` on the node element wrapping our widget
 * and answer with the node menu (Rename / Pin / Bypass …), leaving no way to
 * paste with the mouse. Stopping the event while it's still inside our own root
 * — and only over an editable field — restores the native menu there while the
 * rest of the node body still gets the node menu.
 *
 * @returns {() => void} uninstall
 */
export function installNativeTextMenu(root) {
    if (!root || root._cbNativeTextMenu) return () => {};
    const onContextMenu = (e) => {
        const t = e.target;
        if (!t) return;
        if (t.tagName !== "INPUT" && t.tagName !== "TEXTAREA" && !t.isContentEditable) return;
        e.stopPropagation();
    };
    root.addEventListener("contextmenu", onContextMenu);
    root._cbNativeTextMenu = true;
    return () => {
        root.removeEventListener("contextmenu", onContextMenu);
        root._cbNativeTextMenu = false;
    };
}

/** True when something between `target` and `root` can still scroll that way. */
function scrollRegionWantsWheel(target, root, deltaX, deltaY) {
    const vertical = Math.abs(deltaY) >= Math.abs(deltaX);
    let el = target;
    while (el && el !== root.parentElement) {
        if (el.nodeType === 1) {
            const cs = getComputedStyle(el);
            if (vertical) {
                const oy = cs.overflowY;
                if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) {
                    const atTop = el.scrollTop <= 0;
                    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
                    if ((deltaY < 0 && !atTop) || (deltaY > 0 && !atBottom)) return true;
                }
            } else {
                const ox = cs.overflowX;
                if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) {
                    const atLeft = el.scrollLeft <= 0;
                    const atRight = el.scrollLeft + el.clientWidth >= el.scrollWidth - 1;
                    if ((deltaX < 0 && !atLeft) || (deltaX > 0 && !atRight)) return true;
                }
            }
        }
        el = el.parentElement;
    }
    return false;
}

/**
 * Let the mouse wheel zoom the canvas while the cursor is over an in-node DOM
 * widget (legacy renderer only — Nodes 2.0 already forwards it).
 *
 * ComfyUI binds wheel-to-zoom on the <canvas>; a DOM widget layered over it eats
 * the event, so zoom silently stops working over our toolbar. Forward it unless
 * the cursor is over a scrollable region with room left to scroll.
 *
 * @returns {() => void} uninstall
 */
export function installCanvasZoomPassthrough(root) {
    if (!root || typeof root.addEventListener !== "function") return () => {};
    const onWheel = (e) => {
        if (isVueNodes()) return;
        if (scrollRegionWantsWheel(e.target, root, e.deltaX, e.deltaY)) return;
        const canvasEl = app?.canvas?.canvas;   // read lazily; the canvas can be recreated
        if (!canvasEl) return;
        e.preventDefault();                     // needs the non-passive listener below
        e.stopPropagation();
        const { clientX, clientY, deltaX, deltaY, deltaMode, ctrlKey, metaKey, shiftKey } = e;
        canvasEl.dispatchEvent(new WheelEvent("wheel", {
            clientX, clientY, deltaX, deltaY, deltaMode,
            ctrlKey, metaKey, shiftKey, bubbles: true, cancelable: true,
        }));
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
}

// ── Textarea geometry ──────────────────────────────────────────────────────
//
// One shared hidden mirror div, styled to wrap exactly like the textarea being
// measured, so we can find the y-offset of an arbitrary character index. Same
// technique as the numbered gutter, but measuring a caret rather than a line.

let _mirror = null;
function getMirror() {
    if (_mirror?.isConnected) return _mirror;
    _mirror = document.createElement("div");
    Object.assign(_mirror.style, {
        position: "absolute", visibility: "hidden", top: "0", left: "-99999px",
        whiteSpace: "pre-wrap", overflowWrap: "break-word", wordWrap: "break-word",
        padding: "0", margin: "0", border: "0", boxSizing: "content-box",
    });
    document.body.appendChild(_mirror);
    return _mirror;
}

/** Pixel y-offset (within the textarea's content box) of character `index`. */
export function caretYOffset(el, index) {
    const m = getMirror();
    const cs = getComputedStyle(el);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;
    Object.assign(m.style, {
        width: `${Math.max(0, el.clientWidth - padL - padR)}px`,
        font: cs.font, fontFamily: cs.fontFamily, fontSize: cs.fontSize,
        fontWeight: cs.fontWeight, lineHeight: cs.lineHeight,
        letterSpacing: cs.letterSpacing, tabSize: cs.tabSize,
    });
    m.textContent = String(el.value ?? "").slice(0, index);
    // A zero-width marker gives us the caret's own box even at a line end,
    // where a trailing "\n" would otherwise collapse.
    const marker = document.createElement("span");
    marker.textContent = "​";
    m.appendChild(marker);
    const y = marker.offsetTop;
    m.textContent = "";
    return y;
}

/** Scroll `el` so character `index` is comfortably in view, if it isn't already. */
export function scrollIndexIntoView(el, index) {
    try {
        const cs = getComputedStyle(el);
        let lineH = parseFloat(cs.lineHeight);
        if (!Number.isFinite(lineH)) lineH = (parseFloat(cs.fontSize) || 12) * 1.2;
        const y = caretYOffset(el, index);
        const view = el.clientHeight;
        if (y < el.scrollTop) {
            el.scrollTop = Math.max(0, y - view / 3);
        } else if (y + lineH > el.scrollTop + view) {
            el.scrollTop = Math.max(0, y - view / 2);
        }
    } catch (_) { /* measurement is best-effort; never block the edit */ }
}
