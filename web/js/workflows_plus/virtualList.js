// Minimal windowed/virtualized list for the Workflows+ panel.
//
// Keeps a small, reused pool of absolutely-positioned row elements
// regardless of how many items are set — DOM node count stays roughly
// constant (~viewport height / rowHeight + 2*overscan) whether the list
// holds 10 items or 7,400.
//
// Depends on the DOM; not unit tested headlessly (browser-verified per plan).

export class VirtualList {
    /**
     * @param {Object} opts
     * @param {HTMLElement} opts.scroller - the scrollable container element.
     *   Caller is responsible for giving it a fixed height and
     *   `overflow-y: auto`.
     * @param {number} [opts.rowHeight=26]
     * @param {number} [opts.overscan=8] - extra rows rendered above/below
     *   the visible window to reduce blank flashes on fast scroll.
     * @param {(item: any, rowEl: HTMLElement, index: number) => void} opts.renderRow -
     *   called to (re)populate a recycled row element's content for `item`.
     */
    constructor({ scroller, rowHeight = 26, overscan = 8, renderRow }) {
        this.scroller = scroller;
        this.rowHeight = rowHeight;
        this.overscan = overscan;
        this.renderRow = renderRow;

        this.items = [];
        this._pool = [];
        this._firstRenderedIndex = -1;
        this._rafHandle = null;

        this.spacer = document.createElement("div");
        this.spacer.style.position = "relative";
        this.spacer.style.width = "100%";

        this.scroller.innerHTML = "";
        this.scroller.style.position = "relative";
        this.scroller.appendChild(this.spacer);

        this._onScroll = this._onScroll.bind(this);
        this.scroller.addEventListener("scroll", this._onScroll, { passive: true });

        this._resizeObserver = typeof ResizeObserver !== "undefined"
            ? new ResizeObserver(() => this._scheduleRender())
            : null;
        if (this._resizeObserver) this._resizeObserver.observe(this.scroller);
    }

    setItems(items) {
        this.items = items || [];
        this.spacer.style.height = `${this.items.length * this.rowHeight}px`;
        this._firstRenderedIndex = -1; // force a full re-render on next pass
        this._scheduleRender();
    }

    get scrollTop() {
        return this.scroller.scrollTop;
    }

    set scrollTop(value) {
        this.scroller.scrollTop = value;
    }

    _onScroll() {
        this._scheduleRender();
    }

    _scheduleRender() {
        if (this._rafHandle !== null) return;
        this._rafHandle = requestAnimationFrame(() => {
            this._rafHandle = null;
            this._render();
        });
    }

    _poolSize() {
        const viewportH = this.scroller.clientHeight || 300;
        return Math.ceil(viewportH / this.rowHeight) + this.overscan * 2;
    }

    _ensurePool(size) {
        while (this._pool.length < size) {
            const row = document.createElement("div");
            row.style.position = "absolute";
            row.style.left = "0";
            row.style.right = "0";
            row.style.height = `${this.rowHeight}px`;
            row.style.boxSizing = "border-box";
            row.style.display = "none";
            this.spacer.appendChild(row);
            this._pool.push(row);
        }
        while (this._pool.length > size) {
            const row = this._pool.pop();
            row.remove();
        }
    }

    _render() {
        const total = this.items.length;
        const poolSize = this._poolSize();
        this._ensurePool(poolSize);

        if (total === 0) {
            for (const row of this._pool) row.style.display = "none";
            this._firstRenderedIndex = -1;
            return;
        }

        const scrollTop = this.scroller.scrollTop;
        let firstIdx = Math.floor(scrollTop / this.rowHeight) - this.overscan;
        if (firstIdx < 0) firstIdx = 0;

        const maxFirst = Math.max(0, total - poolSize);
        if (firstIdx > maxFirst) firstIdx = maxFirst;

        this._firstRenderedIndex = firstIdx;

        for (let i = 0; i < this._pool.length; i++) {
            const itemIndex = firstIdx + i;
            const row = this._pool[i];

            if (itemIndex >= total) {
                row.style.display = "none";
                continue;
            }

            row.style.display = "";
            row.style.transform = `translateY(${itemIndex * this.rowHeight}px)`;
            this.renderRow(this.items[itemIndex], row, itemIndex);
        }
    }

    destroy() {
        this.scroller.removeEventListener("scroll", this._onScroll);
        if (this._rafHandle !== null) {
            cancelAnimationFrame(this._rafHandle);
            this._rafHandle = null;
        }
        if (this._resizeObserver) this._resizeObserver.disconnect();
        for (const row of this._pool) row.remove();
        this._pool = [];
        this.items = [];
    }
}
