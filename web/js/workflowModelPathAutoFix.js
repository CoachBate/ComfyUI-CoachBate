import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

let waitCursorDepth = 0;
const POST_LOAD_POLL_INTERVAL_MS = 100;
const POST_LOAD_TIMEOUT_MS = 60000;
let customOverrides = [];
let customOverridesPromise = null;


function beginWaitCursor() {
    const root = document.documentElement;

    if (waitCursorDepth === 0) {
        if (!document.getElementById("coachbate-workflow-path-wait-style")) {
            const style = document.createElement("style");
            style.id = "coachbate-workflow-path-wait-style";
            style.textContent = `
                html.coachbate-workflow-path-wait,
                html.coachbate-workflow-path-wait * {
                    cursor: wait !important;
                }
            `;
            document.head.append(style);
        }
        root.classList.add("coachbate-workflow-path-wait");
    }
    waitCursorDepth++;
}

function endWaitCursor() {
    waitCursorDepth = Math.max(0, waitCursorDepth - 1);
    if (waitCursorDepth === 0) {
        document.documentElement.classList.remove("coachbate-workflow-path-wait");
    }
}


function normalizePath(value) {
    return String(value ?? "")
        .replaceAll("\\", "/")
        .replace(/^\/+/, "")
        .replace(/\/+/g, "/");
}

function normalizeForCompare(value) {
    return normalizePath(value).toLowerCase();
}

function basenameLower(value) {
    const parts = normalizeForCompare(value).split("/");
    return parts[parts.length - 1] ?? "";
}

function commonSuffixParts(left, right) {
    const a = normalizeForCompare(left).split("/");
    const b = normalizeForCompare(right).split("/");
    let count = 0;
    while (count < a.length && count < b.length) {
        if (a[a.length - 1 - count] !== b[b.length - 1 - count]) {
            break;
        }
        count++;
    }
    return count;
}

async function ensureCustomOverridesLoaded() {
    if (!customOverridesPromise) {
        customOverridesPromise = api.fetchApi("/coachbate/workflow_path_autofix/overrides")
            .then(async (response) => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                const payload = await response.json();
                customOverrides = Array.isArray(payload?.overrides) ? payload.overrides : [];
            })
            .catch((error) => {
                customOverrides = [];
                console.warn("[CoachBate] Failed to load workflow path auto-fix overrides:", error);
            });
    }

    await customOverridesPromise;
}

function applyCustomOverride(currentValue, override) {
    const search = override?.search;
    const replacement = override?.replacement;
    if (typeof search !== "string" || typeof replacement !== "string" || !search) {
        return null;
    }

    const normalizedCurrent = normalizeForCompare(currentValue);
    const normalizedSearch = normalizeForCompare(search);
    if (normalizedCurrent === normalizedSearch) {
        return replacement;
    }

    if (normalizedCurrent.startsWith(normalizedSearch)) {
        return `${replacement}${normalizePath(currentValue).slice(normalizePath(search).length)}`;
    }

    const matchIndex = normalizedCurrent.indexOf(normalizedSearch);
    if (matchIndex >= 0) {
        const normalizedOriginal = normalizePath(currentValue);
        return `${normalizedOriginal.slice(0, matchIndex)}${replacement}${normalizedOriginal.slice(matchIndex + normalizePath(search).length)}`;
    }

    return null;
}

function resolveOptionReference(reference, options) {
    if (!Array.isArray(options) || typeof reference !== "string" || !reference) {
        return null;
    }

    const normalizedReference = normalizeForCompare(reference);
    const exactMatch = options.find((option) => normalizeForCompare(option) === normalizedReference);
    if (exactMatch) {
        return exactMatch;
    }

    const basename = basenameLower(reference);
    const basenameMatches = options.filter((option) => basenameLower(option) === basename);
    if (basenameMatches.length === 1) {
        return basenameMatches[0];
    }
    if (basenameMatches.length <= 1) {
        return null;
    }

    const ranked = basenameMatches
        .map((option) => ({
            option,
            score: commonSuffixParts(reference, option),
            depth: normalizePath(option).split("/").length,
        }))
        .sort((left, right) =>
            right.score - left.score ||
            left.depth - right.depth ||
            left.option.localeCompare(right.option),
        );

    if ((ranked[0]?.score ?? 0) > 1 && ranked[0].score > (ranked[1]?.score ?? 0)) {
        return ranked[0].option;
    }

    return null;
}

function resolveWidgetValue(currentValue, options) {
    if (typeof currentValue !== "string") {
        return null;
    }

    for (const override of customOverrides) {
        const overrideCandidate = applyCustomOverride(currentValue, override);
        const resolvedOverride = resolveOptionReference(overrideCandidate, options);
        if (resolvedOverride && resolvedOverride !== currentValue) {
            return resolvedOverride;
        }
    }

    const resolvedValue = resolveOptionReference(currentValue, options);
    if (resolvedValue && resolvedValue !== currentValue) {
        return resolvedValue;
    }

    return null;
}

function walkGraph(graph, callback) {
    for (const node of graph?.nodes ?? []) {
        callback(node);
        if (node.subgraph) {
            walkGraph(node.subgraph, callback);
        }
    }
}

function maybeFixWorkflowModelPaths() {
    let updatedCount = 0;
    const replacements = [];

    walkGraph(app.graph, (node) => {
        for (const [index, widget] of (node.widgets ?? []).entries()) {
            const options = widget?.options?.values;
            const resolvedValue = resolveWidgetValue(widget?.value, options);
            if (!resolvedValue) {
                continue;
            }

            const originalValue = widget.value;
            widget.value = resolvedValue;
            if (Array.isArray(node.widgets_values) && index < node.widgets_values.length) {
                node.widgets_values[index] = resolvedValue;
            }

            syncWidgetDomValue(widget, resolvedValue);

            try {
                widget.callback?.(resolvedValue);
            } catch (error) {
                console.warn("[CoachBate] Widget callback failed during workflow path auto-fix:", error);
            }

            replacements.push({
                original: originalValue,
                replacement: resolvedValue,
            });
            updatedCount++;
        }
    });

    if (updatedCount > 0) {
        logPathReplacements(replacements);
        app.graph?.setDirtyCanvas?.(true, true);
    }

    return { updatedCount, replacements };
}

function syncWidgetDomValue(widget, resolvedValue) {
    const candidates = [
        widget?.inputEl,
        widget?.element,
        widget?.selectEl,
        widget?.domElement,
        widget?.dom,
        widget?.input,
    ];

    for (const candidate of candidates) {
        if (!candidate) {
            continue;
        }

        const element = resolveFormElement(candidate);
        if (!element) {
            continue;
        }

        try {
            element.value = resolvedValue;
            element.setAttribute?.("value", resolvedValue);
            element.dispatchEvent(new Event("input", { bubbles: true }));
            element.dispatchEvent(new Event("change", { bubbles: true }));
            element.dispatchEvent(new Event("blur", { bubbles: true }));
        } catch (error) {
            console.warn("[CoachBate] Failed to sync widget DOM value:", error);
        }
        return;
    }
}

function resolveFormElement(candidate) {
    if (candidate instanceof HTMLInputElement || candidate instanceof HTMLSelectElement || candidate instanceof HTMLTextAreaElement) {
        return candidate;
    }
    return candidate.querySelector?.("input, select, textarea") ?? null;
}

function finishWorkflowPathUpdate(workflow, updatedCount) {
    const startedAt = performance.now();
    let finished = false;

    const finish = () => {
        if (finished) {
            return;
        }
        finished = true;
        endWaitCursor();
    };

    const tryCapture = () => {
        try {
            const activeWorkflow = app.extensionManager?.workflow?.activeWorkflow;
            if (activeWorkflow !== workflow) {
                finish();
                return;
            }

            workflow.changeTracker?.captureCanvasState();

            const activeState = workflow.changeTracker?.activeState;
            const currentState = app.rootGraph?.serialize?.();
            if (activeState && currentState && JSON.stringify(activeState) === JSON.stringify(currentState)) {
                finish();
                app.extensionManager?.toast?.add({
                    severity: "info",
                    summary: "Workflow Paths Updated",
                    detail: `Updated ${updatedCount} model path${updatedCount === 1 ? "" : "s"} in the workflow you opened.`,
                    life: 5000,
                });
                return;
            }

            if (performance.now() - startedAt >= POST_LOAD_TIMEOUT_MS) {
                finish();
                console.warn("[CoachBate] Timed out while waiting to mark the workflow modified.");
                return;
            }

            setTimeout(tryCapture, POST_LOAD_POLL_INTERVAL_MS);
        } catch (error) {
            finish();
            console.warn("[CoachBate] Failed while marking the workflow modified:", error);
        }
    };

    setTimeout(tryCapture, 0);
}

async function handleConfiguredGraph() {
    const workflow = app.extensionManager?.workflow?.activeWorkflow;
    if (!workflow?.changeTracker) {
        return;
    }

    await ensureCustomOverridesLoaded();

    const { updatedCount } = maybeFixWorkflowModelPaths();
    if (updatedCount > 0) {
        beginWaitCursor();
        finishWorkflowPathUpdate(workflow, updatedCount);
    }
}

function logPathReplacements(replacements) {
    void api.fetchApi("/coachbate/workflow_path_autofix/log", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ replacements }),
        })
        .catch((error) => {
            console.warn("[CoachBate] Failed to log workflow path replacement:", error);
        });
}

app.registerExtension({
    name: "CoachBate.WorkflowModelPathAutoFix",

    async afterConfigureGraph() {
        await handleConfiguredGraph();
    },
});
