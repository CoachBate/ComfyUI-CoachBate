// Shared numbered-gutter helper for CoachBate multiline text nodes.
//
// attachNumberedGutter(node, widgetName)
//   Finds the <textarea> for the named widget and overlays a floating gutter
//   div that numbers every non-blank line.  Works in both legacy canvas mode
//   and Nodes 2.0 (Vue-rendered DOM nodes).  Returns a cleanup() function.
//
// installGutterCleanup(nodeType, widgetName)
//   Wraps nodeType.prototype.onRemoved to call cleanup stored as
//   node._cb_gutter_cleanup.

const GUTTER_W = 32;

export function attachNumberedGutter(node, widgetName) {
    const tw = node.widgets?.find(w => w.name === widgetName);
    if (!tw) {
        console.warn(`[CoachBate] attachNumberedGutter: widget "${widgetName}" not found on`, node.type);
        return () => {};
    }

    let el             = null;   // current <textarea> element (may be remounted)
    let gutter         = null;   // fixed-position overlay
    let mirror         = null;   // hidden measurement div
    let resizeObs      = null;   // ResizeObserver on current el
    let destroyed      = false;  // set true by cleanup()
    let lastSearchTime = 0;      // throttle reconnect attempts in the tick

    // ── Textarea discovery ─────────────────────────────────────────────────────
    //
    // Nodes 2.0: the node is a real DOM element identified by [data-node-id].
    //   We walk the node's widgets to find which textarea index corresponds to
    //   widgetName (a node can have multiple multiline inputs).
    //
    // Legacy canvas: widget properties (tw.inputEl etc.) point to the injected
    //   element; falling back to a value-match scan as a last resort.
    //
    const findTextarea = () => {
        // ── Nodes 2.0 path ─────────────────────────────────────────────────
        const nodeEl = document.querySelector(`[data-node-id="${node.id}"]`);
        if (nodeEl) {
            // Count how many textarea-producing widgets precede widgetName
            // so we can index into the node's <textarea> elements correctly.
            let taIdx = 0;
            const widgets = node.widgets ?? [];
            for (let i = 0; i < widgets.length; i++) {
                const w = widgets[i];
                if (w.name === widgetName) {
                    const all = nodeEl.querySelectorAll("textarea");
                    if (all[taIdx]) return all[taIdx];
                    break;
                }
                // Does this widget render a <textarea>?
                if (
                    w.type === "customtext" ||
                    w.type === "textarea"   ||
                    w.options?.multiline
                ) {
                    taIdx++;
                }
            }
            // Single-textarea fallback (covers most practical cases)
            const first = nodeEl.querySelector("textarea");
            if (first) return first;
        }

        // ── Legacy path ────────────────────────────────────────────────────
        const candidates = [tw.inputEl, tw.element, tw.input, tw.domElement, tw.dom];
        for (const c of candidates) {
            if (!c) continue;
            if (c.tagName === "TEXTAREA") return c;
            const inner = c.querySelector?.("textarea");
            if (inner) return inner;
        }

        // Legacy last resort: match by current widget value
        const want = tw.value ?? "";
        for (const ta of document.querySelectorAll("textarea")) {
            if (ta._cb_owned) continue;
            if (ta.value === want) return ta;
        }

        return null;
    };

    // ── Mirror helpers ─────────────────────────────────────────────────────────

    const syncMirror = () => {
        const cs   = getComputedStyle(el);
        const padL = parseFloat(cs.paddingLeft)  || 0;
        const padR = parseFloat(cs.paddingRight) || 0;
        Object.assign(mirror.style, {
            width:         `${el.clientWidth - padL - padR}px`,
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

    const measureLineY = (lines, n) => {
        if (n === 0) return 0;
        mirror.textContent =
            lines.slice(0, n).map(l => l.trim() === "" ? " " : l).join("\n");
        return mirror.getBoundingClientRect().height;
    };

    // ── Redraw / reposition ────────────────────────────────────────────────────

    let lastDrawKey = "";

    const redraw = () => {
        if (!el?.isConnected) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) {
            gutter.style.display = "none";
            return;
        }
        gutter.style.display = "block";
        gutter.style.left    = `${rect.left}px`;
        gutter.style.top     = `${rect.top}px`;
        gutter.style.height  = `${rect.height}px`;

        // ratio of viewport pixels to CSS layout pixels — tracks canvas zoom
        const scale   = el.offsetHeight > 0 ? rect.height / el.offsetHeight : 1;
        const needPad = `${Math.ceil((GUTTER_W + 6) / scale)}px`;
        if (el.style.paddingLeft !== needPad) el.style.paddingLeft = needPad;

        const cs      = syncMirror();
        const padTop  = parseFloat(cs.paddingTop) || 0;
        const scrollT = el.scrollTop;
        const value   = el.value ?? "";

        const key = `${value.length}|${rect.width}|${rect.height}|${scrollT}|${cs.fontSize}|${value}`;
        if (key === lastDrawKey) return;
        lastDrawKey = key;

        while (gutter.firstChild) gutter.removeChild(gutter.firstChild);

        const lines = value.split("\n");
        let n = 0;
        for (let i = 0; i < lines.length; i++) {
            if (!lines[i].trim()) continue;
            n++;
            const yContent = measureLineY(lines, i);
            const yVisible = (padTop + yContent - scrollT) * scale;
            if (yVisible < -16 || yVisible > rect.height) continue;

            const num = document.createElement("div");
            num.textContent = String(n);
            Object.assign(num.style, {
                position:   "absolute",
                left:       "0",
                right:      "0",
                top:        `${yVisible}px`,
                textAlign:  "center",
                color:      "#c89040",
                font:       cs.font,
                fontWeight: "bold",
                lineHeight: cs.lineHeight,
            });
            gutter.appendChild(num);
        }
    };

    let lastRect = "";
    const repositionOnly = () => {
        if (!el?.isConnected) return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) {
            gutter.style.display = "none";
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

    // ── Attach / detach ────────────────────────────────────────────────────────

    const resolveZ = (start) => {
        let n = start.parentElement;
        while (n && n !== document.body) {
            const z = parseInt(getComputedStyle(n).zIndex);
            if (!isNaN(z)) return z;
            n = n.parentElement;
        }
        return 1;
    };

    const detachFromEl = () => {
        if (!el) return;
        el.removeEventListener("input",  redraw);
        el.removeEventListener("scroll", redraw);
        el.style.paddingLeft = "";
        delete el._cb_owned;
        resizeObs?.disconnect();
        resizeObs = null;
        el = null;
    };

    const attachToEl = (ta) => {
        if (ta._cb_owned) return false;
        el            = ta;
        el._cb_owned  = true;
        el.style.boxSizing = "border-box";

        gutter.style.zIndex = String(resolveZ(el));

        lastDrawKey = "";
        lastRect    = "";
        redraw();

        el.addEventListener("input",  redraw);
        el.addEventListener("scroll", redraw);
        resizeObs = new ResizeObserver(() => { lastDrawKey = ""; redraw(); });
        resizeObs.observe(el);
        return true;
    };

    // ── rAF tick ───────────────────────────────────────────────────────────────
    //
    // Runs every frame to keep the gutter's position in sync with the textarea
    // (which moves when the canvas is panned/zoomed or the node is dragged).
    //
    // Also handles Nodes 2.0 remounting: when Vue unmounts the textarea (e.g.
    // node collapsed) and later remounts it (new DOM element), the tick detects
    // the disconnection, releases the old element, and reattaches to the new one.
    //
    const tick = () => {
        if (destroyed) return;

        if (!el?.isConnected) {
            if (el) detachFromEl();

            // Throttle the DOM search to ~5 per second while disconnected
            const now = performance.now();
            if (now - lastSearchTime > 200) {
                lastSearchTime = now;
                const ta = findTextarea();
                if (ta) {
                    attachToEl(ta);
                    console.debug(`[CoachBate] ${node.type}: gutter (re)attached to`, ta);
                }
            }
        }

        if (el?.isConnected) {
            if (repositionOnly()) { lastDrawKey = ""; redraw(); }
        }

        requestAnimationFrame(tick);
    };

    // ── DOM setup ──────────────────────────────────────────────────────────────

    gutter = document.createElement("div");
    Object.assign(gutter.style, {
        position:      "fixed",
        pointerEvents: "none",
        overflow:      "hidden",
        boxSizing:     "border-box",
        width:         `${GUTTER_W}px`,
        background:    "rgba(18, 18, 28, 0.93)",
        borderRight:   "1px solid rgba(200, 155, 55, 0.45)",
        display:       "none",
        zIndex:        "1",   // updated in attachToEl once we have the element
    });
    document.body.appendChild(gutter);

    mirror = document.createElement("div");
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

    const onFullscreen = () => {
        gutter.style.visibility = document.fullscreenElement ? "hidden" : "visible";
    };
    document.addEventListener("fullscreenchange", onFullscreen);

    // Initial attach: delay to let both legacy injection and Nodes 2.0 Vue
    // rendering finish before searching.  The tick handles any later remounts.
    const tryInitial = (attempt = 0) => {
        if (destroyed) return;
        const ta = findTextarea();
        if (ta) {
            attachToEl(ta);
            console.debug(`[CoachBate] ${node.type}: gutter attached to`, ta);
        } else if (attempt < 50) {
            setTimeout(() => tryInitial(attempt + 1), 200);
            return;
        } else {
            console.warn(`[CoachBate] ${node.type}: textarea not found for "${widgetName}"`);
        }
        // Start the tick regardless so remounts are caught later
        requestAnimationFrame(tick);
    };
    setTimeout(() => tryInitial(0), 200);

    // ── Cleanup ────────────────────────────────────────────────────────────────

    return () => {
        destroyed = true;
        detachFromEl();
        gutter.remove();
        mirror.remove();
        document.removeEventListener("fullscreenchange", onFullscreen);
    };
}

export function installGutterCleanup(nodeType) {
    const origOnRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
        origOnRemoved?.apply(this, arguments);
        this._cb_gutter_cleanup?.();
    };
}
