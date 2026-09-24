import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/js/coachBateShotLoader.js", import.meta.url), "utf8")
    .replace('import { app } from "../../../scripts/app.js";', "")
    .replace('import { api } from "../../../scripts/api.js";', "");

function harness({ enabled = true, last = false, mode = "increment", queueResult = true } = {}) {
    const listeners = new Map(), timers = new Map(), toasts = [];
    let queued = 0, timerId = 0;
    const app = {
        extensionManager: { toast: { add: item => toasts.push(item), remove() {} } },
        registerExtension(extension) { this.extension = extension; },
        async queuePrompt() { queued++; return queueResult; },
    };
    vm.runInNewContext(source, {
        app, api: { addEventListener(name, fn) { listeners.set(name, fn); } },
        fetch: async () => ({ json: async () => ({ windows: false }) }),
        setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
        clearTimeout(id) { timers.delete(id); },
        console: { error() {} }, performance: { now: () => 0 },
    });
    class Loader {
        static comfyClass = "CoachBateShotLoader";
        constructor() {
            this.id = 7; this.type = Loader.comfyClass; this.properties = {};
            this.widgets = [{ name: "mode", value: mode }, { name: "shot_number", value: 11 }];
            this.onNodeCreated();
        }
        addWidget(type, name, value, callback, options) {
            const widget = { type, name, value, callback, options };
            this.widgets.push(widget); return widget;
        }
    }
    app.extension.beforeRegisterNodeDef(Loader);
    const node = new Loader();
    node.properties.continue_after_error = enabled;
    app.graph = { nodes: [node], getNodeById: id => String(id) === "7" ? node : null, setDirtyCanvas() {} };
    const event = (name, detail = {}) => listeners.get(name)?.({ detail });
    const load = () => {
        node.onExecuted({ array_idx: [10], total: [41], text: ["7/31 S1E1_011"], is_last: [last] });
        event("executed", { node: 7, prompt_id: "ours" });
    };
    load();
    return {
        app, node, toasts, event, load,
        fail: (overrides = {}) => event("execution_error", {
            prompt_id: "ours", node_id: "continuity", exception_message: "Bad AV timing", ...overrides,
        }),
        get queued() { return queued; },
        async flush() { const pending = [...timers.values()]; timers.clear(); for (const fn of pending) await fn(); },
    };
}

test("enabled recovery queues exactly one next shot and retains failure", async () => {
    const h = harness(); h.fail(); h.fail(); await h.flush();
    assert.equal(h.queued, 1);
    assert.equal(h.node.properties.shot_loader_failures.length, 1);
    assert.equal(h.node.properties.shot_loader_failures[0].array_index, 10);
    assert.equal(h.node.widgets.find(w => w.name === "shot_number").value, 11);
});

for (const options of [{ enabled: false }, { last: true }, { mode: "fixed" }]) {
    test(`does not advance ${JSON.stringify(options)}`, async () => {
        const h = harness(options); h.fail(); await h.flush(); assert.equal(h.queued, 0);
    });
}

test("success still advances and duplicate success does not double queue", async () => {
    const h = harness({ enabled: false });
    h.event("execution_success", { prompt_id: "ours" });
    h.event("execution_success", { prompt_id: "ours" });
    await h.flush(); assert.equal(h.queued, 1);
});

test("foreign prompt errors do not stop this batch", async () => {
    const h = harness(); h.fail({ prompt_id: "other" });
    h.event("execution_success", { prompt_id: "ours" });
    await h.flush(); assert.equal(h.queued, 1);
    assert.equal(h.node.properties.shot_loader_failures, undefined);
});

test("interrupt cancels pending error recovery", async () => {
    const h = harness(); h.fail(); h.event("execution_interrupted");
    await h.flush(); assert.equal(h.queued, 0);
});

test("interrupt alone never queues", async () => {
    const h = harness(); h.event("execution_interrupted"); await h.flush();
    assert.equal(h.queued, 0);
});

test("unsafe CUDA state stops instead of requeueing", async () => {
    const h = harness(); h.fail({ exception_message: "CUDA error: an illegal memory access was encountered" });
    await h.flush(); assert.equal(h.queued, 0);
});

test("graph change cancels pending recovery", async () => {
    const h = harness(); h.fail(); h.app.graph = {}; await h.flush(); assert.equal(h.queued, 0);
});

test("turning recovery off cancels its pending queue", async () => {
    const h = harness(); h.fail(); h.node.properties.continue_after_error = false;
    await h.flush(); assert.equal(h.queued, 0);
});

test("rejected next prompt stops rather than looping", async () => {
    const h = harness({ queueResult: false }); h.fail(); await h.flush();
    assert.equal(h.queued, 1); assert.equal(h.node._coachbate_autoqueue, false);
});

test("toggle reads saved properties without changing positional widget serialization", () => {
    const h = harness(); const toggle = h.node.widgets.find(w => w.name === "Continue after error");
    assert.equal(toggle.options.serialize, false);
    h.node.properties = { continue_after_error: false }; assert.equal(toggle.value, false);
    toggle.callback(true); assert.equal(h.node.properties.continue_after_error, true);
});

test("unbound loader failure cannot repeat an unknown backend position", async () => {
    const h = harness(); h.node._coachbate_prompt_id = null;
    h.fail(); await h.flush(); assert.equal(h.queued, 0);
});
