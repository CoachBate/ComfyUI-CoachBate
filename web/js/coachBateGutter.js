// Shared numbered-gutter overlay for CoachBate multiline text nodes.
//
// attachNumberedGutter(node, widgetName, opts)
//   Finds the <textarea> for the named widget and mounts a gutter beside it
//   that numbers the text.  Works in both legacy canvas mode and Nodes 2.0
//   (Vue-rendered DOM nodes).  Returns a cleanup() function.
//
// installGutterCleanup(nodeType)
//   Wraps nodeType.prototype.onRemoved to call cleanup stored as
//   node._cb_gutter_cleanup.
//
// ── Why the gutter lives INSIDE the node ───────────────────────────────────
//
// It used to be a body-level `position: fixed` overlay dragged around by a
// per-frame rAF loop, which forced an impossible z-index choice: low enough to
// stay under the sidebar, dialogs and toasts, high enough to clear the node it
// belongs to.  A body-level element can't win both — the batch prompter ended
// up on 2147483000, painting over the settings dialog and the Workflows+ panel
// alike (with an elementFromPoint hack papering over the worst of it), while
// this shared copy resolved a z-index off the DOM ancestors and landed on an
// arbitrary one.
//
// Mounting the gutter as a sibling of the textarea removes the choice.  Both
// renderers wrap the graph in an `isolation: isolate` layer — the `.lg-node`
// element in Nodes 2.0, the DOM-widget container in legacy canvas mode — so
// anything inside it *cannot* paint over a sidebar, a dialog or a toast, and
// it stacks against other nodes exactly the way its own node does.  Both
// renderers also make the textarea's parent its offsetParent (`.group.relative`
// in Nodes 2.0, the fixed `.dom-widget` wrapper in legacy) and carry the canvas
// pan/zoom transform on an ancestor, so the gutter tracks the node for free:
// no per-frame repositioning, no rAF loop, and no scale arithmetic — every
// measurement below is plain layout pixels.
//
// ⚠️ Those two parent elements are minified-internal frontend surfaces.
// Verified against comfyui_frontend_package 1.51.9 in both renderers; the
// static-position guard in attachToEl is the fallback if either ever changes.

import { findWidgetTextarea } from "./coachBateNodeDom.js";

// ── Colours: theme tokens only, never literals ─────────────────────────────
// ComfyUI is themeable, so every colour resolves through its own variables —
// the PrimeVue tokens first, falling back to the legacy `--comfy-*` / `--fg-*`
// vars for older frontends.  There is deliberately no hex literal at the end of
// any chain: a literal would survive a theme change and be the one wrong colour
// on the node.  Alpha comes from color-mix() over the same tokens, so tints
// track the theme too.
const GUTTER_CSS_ID = "cb-gutter-css";
const GUTTER_CSS = `
:root {
    --cbg-ink:    var(--p-text-color, var(--input-text, var(--fg-color)));
    --cbg-fallback-bg: var(--p-surface-800, var(--comfy-menu-bg));
    /* The system highlight colour — the same accent ComfyUI uses for selection
       and focus.  The ACTIVE prompt must be painted with this, not with a mix
       of the node's own colour: a dimmed version of the surrounding text reads
       as disabled, which is the opposite of what "this one is running" should
       say.  It is deliberately the one colour here that does NOT come from the
       node, so it stands out on a grey node and a green one alike. */
    --cbg-active:      var(--p-primary-color, var(--fg-color));
    --cbg-active-ink:  var(--p-primary-contrast-color, var(--comfy-menu-bg));
}
/* The gutter is styled as the node's TITLE BAR continued down the side of the
   text: --cbg-surface is the node's own title colour, pushed in per node by
   syncTitleColor().  On a node the user has coloured orange the gutter is that
   orange with bold light numbers, exactly like the title above it. */
.cbg-gutter {
    background: var(--cbg-surface, var(--cbg-fallback-bg));
    border-right: 1px solid color-mix(in srgb,
        var(--cbg-surface, var(--cbg-fallback-bg)) 65%, var(--cbg-ink));
    color: var(--cbg-ink);
}
.cbg-band {
    background: color-mix(in srgb, var(--cbg-active) 18%, transparent);
    border-top: 1px solid color-mix(in srgb, var(--cbg-active) 60%, transparent);
    border-bottom: 1px solid color-mix(in srgb, var(--cbg-active) 60%, transparent);
}
/* Idle numbers inherit --cbg-ink from .cbg-gutter; the active one switches to
   the system highlight so it reads as running, not as dimmed-out. */
.cbg-num { color: color-mix(in srgb, var(--cbg-ink) 72%, var(--cbg-surface, transparent)); }
.cbg-num.cbg-active {
    color: var(--cbg-active-ink);
    background: var(--cbg-active);
    font-weight: bold;
}
`;

function injectGutterCss() {
    if (document.getElementById(GUTTER_CSS_ID)) return;
    const style = document.createElement("style");
    style.id = GUTTER_CSS_ID;
    style.textContent = GUTTER_CSS;
    document.head.appendChild(style);
}

const GUTTER_W = 32;    // gutter width, layout px
const TEXT_GAP = 6;     // clearance between the gutter and the text
const POLL_MS  = 400;   // remount check + external-state repaint

/**
 * One entry per non-blank line — the default numbering.
 * @returns {{ordinal:number, line:number, endLine:number}[]}
 */
function defaultSegments(text) {
    const lines = String(text ?? "").split("\n");
    const segs  = [];
    let ordinal = 0;
    for (let i = 0; i < lines.length; i++) {
        if (!lines[i].trim()) continue;
        segs.push({ ordinal: ++ordinal, line: i, endLine: i });
    }
    return segs;
}

/**
 * @param {object} node                the LiteGraph node
 * @param {string} widgetName          name of the multiline STRING widget
 * @param {object} [opts]
 * @param {(text:string)=>{ordinal:number,line:number,endLine:number,active?:boolean}[]}
 *        [opts.segments]              which lines get a number, and what number.
 *        A segment spanning several lines (the batch prompter's "||" mode) is
 *        numbered at `line` and highlighted through `endLine`.  One entry with
 *        `active: true` draws a highlight band across the textarea.
 * @param {()=>string} [opts.stateKey] extra redraw key, polled — for numbering
 *        that depends on node state the textarea itself never reports.
 * @returns {() => void}               cleanup, for onRemoved
 */
export function attachNumberedGutter(node, widgetName, opts = {}) {
    injectGutterCss();
    const segmentsOf = opts.segments ?? defaultSegments;
    const stateKey   = opts.stateKey ?? (() => "");

    if (!node.widgets?.find(w => w.name === widgetName)) {
        console.warn(`[CoachBate] attachNumberedGutter: widget "${widgetName}" not found on`, node.type);
        return () => {};
    }

    let el        = null;    // current <textarea> (Nodes 2.0 remounts it)
    let host      = null;    // element the gutter/band are mounted into
    let resizeObs = null;
    let destroyed = false;
    let lastKey   = "";

    // ── Textarea discovery ─────────────────────────────────────────────────
    // Shared with the find/replace toolbar — see coachBateNodeDom.js for the
    // Nodes 2.0 / legacy / value-match strategy.  Skip elements another
    // overlay already owns, and any explicitly flagged as gutter-free.
    const findTextarea = () =>
        findWidgetTextarea(node, widgetName, { skipOwned: true, skipNoGutter: true });

    // ── Elements ───────────────────────────────────────────────────────────

    const gutter = document.createElement("div");
    gutter.className = "cbg-gutter";
    gutter.dataset.cbGutter = "1";
    Object.assign(gutter.style, {
        position:      "absolute",
        pointerEvents: "none",
        overflow:      "hidden",
        boxSizing:     "border-box",
        width:         `${GUTTER_W}px`,
        display:       "none",
        // Local to the node's stacking context: enough to clear the textarea,
        // powerless to escape the node.  See the header comment.
        zIndex:        "2",
    });

    // Active-segment highlight band, drawn under the numbers across the full
    // textarea width.  Only used when a segment reports `active: true`.
    const band = document.createElement("div");
    band.className = "cbg-band";
    band.dataset.cbGutterBand = "1";
    Object.assign(band.style, {
        position:      "absolute",
        pointerEvents: "none",
        boxSizing:     "border-box",
        display:       "none",
        zIndex:        "1",
    });

    // Safety net for the token chains in GUTTER_CSS.  Every colour there comes
    // from a ComfyUI theme variable, but if a frontend defines none of the
    // names in a chain the declaration is invalid and the gutter paints
    // transparent — numbers straight over the prompt text, which reads as "the
    // gutter is broken".  When that happens, fall back to the textarea's own
    // resolved colours: still theme values (they ARE whatever the theme painted
    // the field with), just read at runtime instead of through a variable.
    // The node's TITLE colour, pushed into the gutter as --cbg-surface so the
    // gutter reads as that title bar running down the side of the text.  It is
    // a per-node LiteGraph property (node.color, set from the canvas Colors
    // menu), so no theme token can know it — and recolouring a node fires no
    // event, so this rides the poll below.
    let lastTitle = null;
    const syncTitleColor = () => {
        const c = node.color || window.LiteGraph?.NODE_DEFAULT_COLOR || "";
        if (c === lastTitle) return;
        lastTitle = c;
        // Both elements: the band is a SIBLING of the gutter, not a child, so
        // it inherits nothing from it — set the property on each.
        for (const elm of [gutter, band]) {
            if (c) elm.style.setProperty("--cbg-surface", c);
            else elm.style.removeProperty("--cbg-surface");
        }
    };

    const isTransparent = c => !c || c === "transparent" || /,\s*0\s*\)$/.test(c);
    const applyThemeFallback = () => {
        if (!el) return;
        try {
            const gcs = getComputedStyle(gutter);
            const tcs = getComputedStyle(el);
            if (isTransparent(gcs.backgroundColor)) gutter.style.background = tcs.backgroundColor;
            if (isTransparent(gcs.borderRightColor)) {
                gutter.style.borderRight = `1px solid ${tcs.color}`;
            }
            // An unresolvable var() makes `color` fall back to inheritance, so
            // seeding the gutter's own colour is enough to reach the numbers.
            if (isTransparent(gcs.color)) gutter.style.color = tcs.color;
        } catch (_) { /* unmounted — the stylesheet is the right answer anyway */ }
    };

    // Hidden measurement div, styled to wrap exactly like the textarea, so the
    // y-offset of any line can be measured under the real wrap rules.  Lives at
    // body level (never painted, and deliberately outside the node's transform
    // so it measures in unscaled layout pixels).
    const mirror = document.createElement("div");
    Object.assign(mirror.style, {
        position:     "absolute",
        visibility:   "hidden",
        top:          "0",
        left:         "-99999px",
        whiteSpace:   "pre-wrap",
        overflowWrap: "break-word",
        wordWrap:     "break-word",
        padding:      "0",
        margin:       "0",
        border:       "0",
        boxSizing:    "content-box",
    });
    document.body.appendChild(mirror);

    // ── Measurement ────────────────────────────────────────────────────────

    const syncMirror = () => {
        const cs   = getComputedStyle(el);
        const padL = parseFloat(cs.paddingLeft)  || 0;
        const padR = parseFloat(cs.paddingRight) || 0;
        Object.assign(mirror.style, {
            width:         `${Math.max(0, el.clientWidth - padL - padR)}px`,
            font:          cs.font,
            fontFamily:    cs.fontFamily,
            fontSize:      cs.fontSize,
            fontWeight:    cs.fontWeight,
            lineHeight:    cs.lineHeight,
            letterSpacing: cs.letterSpacing,
            tabSize:       cs.tabSize,
        });
        return cs;
    };

    // Y-offset (layout px, from the top of the content box) at which line `n`
    // begins.  Empty lines collapse to zero height in a div, so stand a space
    // in for them and each still takes one line-height.
    const measureLineY = (lines, n) => {
        if (n <= 0) return 0;
        mirror.textContent = lines.slice(0, n).map(l => l === "" ? " " : l).join("\n");
        return mirror.getBoundingClientRect().height;
    };

    // ── Draw ───────────────────────────────────────────────────────────────

    const hide = () => {
        gutter.style.display = "none";
        band.style.display   = "none";
        lastKey = "";
    };

    const redraw = () => {
        if (destroyed || !el?.isConnected) return;

        // Keep the text clear of the gutter.  Constant now that everything is
        // in layout pixels — set before measuring, since it changes clientWidth
        // and therefore where the text wraps.
        const needPad = `${GUTTER_W + TEXT_GAP}px`;
        if (el.style.paddingLeft !== needPad) el.style.paddingLeft = needPad;

        // offsetHeight is 0 while the widget is culled (legacy hides off-screen
        // DOM widgets) or the node is collapsed.
        const w = el.offsetWidth;
        const h = el.offsetHeight;
        if (!w || !h) { hide(); return; }

        gutter.style.display = "block";
        gutter.style.left    = `${el.offsetLeft}px`;
        gutter.style.top     = `${el.offsetTop}px`;
        gutter.style.height  = `${h}px`;

        const cs      = syncMirror();
        const padTop  = parseFloat(cs.paddingTop) || 0;
        const scrollT = el.scrollTop;
        const value   = el.value ?? "";

        const key = `${w}|${h}|${scrollT}|${cs.fontSize}|${cs.lineHeight}|${stateKey()}|${value}`;
        if (key === lastKey) return;
        lastKey = key;

        while (gutter.firstChild) gutter.removeChild(gutter.firstChild);

        const lines = value.split("\n");
        let bandPlaced = false;

        for (const seg of segmentsOf(value)) {
            const yContent = measureLineY(lines, seg.line);
            const yTop     = padTop + yContent - scrollT;

            // Starts below the view — nothing of it can be visible.
            if (yTop > h) continue;

            // Bottom of the segment's LAST line.  One line below yTop for a
            // single-line segment, but a multi-line one can be taller than the
            // whole textarea, so measure it separately — lazily, since only the
            // clipping cases below need it.
            let bot = null;
            const yBot = () => {
                if (bot === null) bot = padTop + measureLineY(lines, seg.endLine + 1) - scrollT;
                return bot;
            };

            // A segment whose start scrolled off the top is still on screen
            // while its body is — cull only once its end has gone too.
            if (yTop < 0 && yBot() < 0) continue;

            if (seg.active) {
                const top    = Math.max(yTop, 0);
                const height = Math.min(yBot(), h) - top;
                if (height > 0) {
                    Object.assign(band.style, {
                        display: "block",
                        left:    `${el.offsetLeft}px`,
                        top:     `${el.offsetTop + top}px`,
                        width:   `${w}px`,
                        height:  `${height}px`,
                    });
                    bandPlaced = true;
                }
            }

            // Pin the number to the top of the view while a segment taller than
            // one line is scrolled past, so a long multi-line prompt stays
            // numbered all the way through.  Short segments scroll out
            // naturally — pinning them would stack numbers on each other.
            let numY = yTop;
            if (yTop < 0) {
                const numH = Math.max(measureLineY(lines, seg.line + 1) - yContent, 4);
                if (yBot() > numH) numY = 0;
            }

            const num = document.createElement("div");
            num.className = seg.active ? "cbg-num cbg-active" : "cbg-num";
            num.textContent = String(seg.ordinal);
            Object.assign(num.style, {
                position:   "absolute",
                left:       "0",
                right:      "0",
                top:        `${numY}px`,
                textAlign:  "center",
                font:       cs.font,
                fontWeight: "bold",
                lineHeight: cs.lineHeight,
            });
            gutter.appendChild(num);
        }

        if (!bandPlaced) band.style.display = "none";
    };

    // ── Attach / detach ────────────────────────────────────────────────────

    const detachFromEl = () => {
        if (!el) return;
        el.removeEventListener("input",  redraw);
        el.removeEventListener("scroll", redraw);
        el.style.paddingLeft = "";
        delete el._cb_owned;
        resizeObs?.disconnect();
        resizeObs = null;
        gutter.remove();
        band.remove();
        host = null;
        el   = null;
        hide();
    };

    const attachToEl = (ta) => {
        const parent = ta.parentElement;
        if (ta._cb_owned || !parent) return false;

        // Both renderers already give the textarea a positioned parent, so this
        // normally does nothing.  If a future frontend makes it static, our
        // absolute offsets would resolve against some further ancestor and the
        // gutter would land in the wrong place — `relative` with no offsets
        // moves nothing and (without a z-index) creates no stacking context.
        if (getComputedStyle(parent).position === "static") parent.style.position = "relative";

        el   = ta;
        host = parent;
        el._cb_owned = true;
        el.style.boxSizing = "border-box";

        host.appendChild(band);
        host.appendChild(gutter);
        syncTitleColor();
        applyThemeFallback();

        lastKey = "";
        redraw();

        el.addEventListener("input",  redraw);
        el.addEventListener("scroll", redraw);
        resizeObs = new ResizeObserver(() => { lastKey = ""; redraw(); });
        resizeObs.observe(el);
        return true;
    };

    // ── Poll ───────────────────────────────────────────────────────────────
    //
    // One interval for the whole attachment — never a loop per attach, which is
    // how the batch prompter's old rAF version multiplied itself into a
    // reattach storm every time Nodes 2.0 remounted the textarea.
    //
    // Handles two things: reconnecting after Vue unmounts and remounts the
    // textarea (node collapsed, tab switched), and repainting when numbering
    // depends on state the textarea never fires an event for (stateKey, or a
    // widget value written programmatically).  redraw() bails on an unchanged
    // key, so this stays cheap.  Position is pure CSS now, so nothing here runs
    // per frame.
    const poll = setInterval(() => {
        if (destroyed) return;
        syncTitleColor();
        if (el?.isConnected) { redraw(); return; }

        if (el) detachFromEl();
        const ta = findTextarea();
        if (ta && attachToEl(ta)) {
            console.debug(`[CoachBate] ${node.type}: gutter attached to`, ta);
        }
    }, POLL_MS);

    // First attach: give both legacy injection and Nodes 2.0's async mount a
    // moment to produce the element.  The poll takes over from there.
    setTimeout(() => {
        if (destroyed) return;
        const ta = findTextarea();
        if (ta && attachToEl(ta)) {
            console.debug(`[CoachBate] ${node.type}: gutter attached to`, ta);
        }
    }, 200);

    // ── Cleanup ────────────────────────────────────────────────────────────

    return () => {
        destroyed = true;
        clearInterval(poll);
        detachFromEl();
        gutter.remove();
        band.remove();
        mirror.remove();
    };
}

export function installGutterCleanup(nodeType) {
    const origOnRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
        origOnRemoved?.apply(this, arguments);
        this._cb_gutter_cleanup?.();
    };
}
