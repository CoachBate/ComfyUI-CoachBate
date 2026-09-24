import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

let waitCursorDepth = 0;
const POST_LOAD_POLL_INTERVAL_MS = 100;
const POST_LOAD_TIMEOUT_MS = 60000;
const ENABLED_SETTING_ID = "CoachBate.WorkflowModelPathAutoFix.Enabled";
const OVERRIDE_SETTINGS_CLASS = "coachbate-workflow-path-override-settings";
let customOverrides = [];
let customOverridesPromise = null;
let loraModelOptionsPromise = null;


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

// Training tools vary the zero-padding on step counters between runs
// (foo_000008000.safetensors vs foo_08000.safetensors), so compare
// basenames with each digit run reduced to its numeric value.
function basenameDigitNormalized(value) {
    return basenameLower(value).replace(/\d+/g, (run) => String(Number(run)));
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

    await customOverridesPromise;
}


function renderOverrideSettings() {
    const container = document.createElement("div");
    container.classList.add(OVERRIDE_SETTINGS_CLASS);
    container.style.cssText = "display:flex;flex-direction:column;gap:10px;width:clamp(320px,calc(100vw - 340px),900px);font-size:14px;";
    container.hidden = app.ui.settings.getSettingValue(ENABLED_SETTING_ID) === false;

    const description = document.createElement("div");
    description.textContent = "Rules are applied from top to bottom. Source and target cannot contain whitespace.";
    description.style.cssText = "font-size:13px;opacity:.8;";

    const table = document.createElement("table");
    table.style.cssText = "width:100%;table-layout:fixed;border-collapse:separate;border-spacing:6px 4px;";
    table.innerHTML = "<colgroup><col><col><col style='width:76px'></colgroup><thead><tr><th style='text-align:left'>Source</th><th style='text-align:left'>Target</th><th></th></tr></thead>";
    const tableBody = document.createElement("tbody");
    table.append(tableBody);

    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;align-items:center;gap:8px;";
    const addButton = document.createElement("button");
    addButton.type = "button";
    addButton.textContent = "Add row";
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save replacements";
    const status = document.createElement("span");
    status.style.cssText = "font-size:13px;";
    actions.append(addButton, saveButton, status);
    container.append(description, table, actions);

    const setStatus = (message, isError = false) => {
        status.textContent = message;
        status.style.color = isError ? "var(--error-text, #e66)" : "";
    };

    const addRow = (search = "", replacement = "") => {
        const row = document.createElement("tr");
        const sourceCell = document.createElement("td");
        const targetCell = document.createElement("td");
        const removeCell = document.createElement("td");
        const sourceInput = document.createElement("input");
        const targetInput = document.createElement("input");
        const removeButton = document.createElement("button");

        sourceInput.type = "text";
        sourceInput.value = search;
        sourceInput.placeholder = "old/model.safetensors";
        sourceInput.autocomplete = "off";
        sourceInput.style.cssText = "width:100%;min-width:0;box-sizing:border-box;font:inherit;";
        targetInput.type = "text";
        targetInput.value = replacement;
        targetInput.placeholder = "new/model.safetensors";
        targetInput.autocomplete = "off";
        targetInput.style.cssText = "width:100%;min-width:0;box-sizing:border-box;font:inherit;";
        removeButton.type = "button";
        removeButton.textContent = "Remove";
        removeButton.addEventListener("click", () => row.remove());

        sourceCell.append(sourceInput);
        targetCell.append(targetInput);
        removeCell.append(removeButton);
        row.append(sourceCell, targetCell, removeCell);
        tableBody.append(row);
    };

    const loadRows = async () => {
        setStatus("Loading…");
        try {
            const response = await api.fetchApi("/coachbate/workflow_path_autofix/overrides");
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload?.error || `HTTP ${response.status}`);
            }
            const overrides = Array.isArray(payload?.overrides) ? payload.overrides : [];
            customOverrides = overrides;
            tableBody.replaceChildren();
            for (const override of overrides) {
                addRow(override.search, override.replacement);
            }
            setStatus(`${overrides.length} rule${overrides.length === 1 ? "" : "s"} loaded`);
        } catch (error) {
            setStatus(`Failed to load: ${error.message || error}`, true);
        }
    };

    addButton.addEventListener("click", () => {
        addRow();
        const inputs = tableBody.lastElementChild?.querySelectorAll("input");
        inputs?.[0]?.focus();
    });

    saveButton.addEventListener("click", async () => {
        const overrides = Array.from(tableBody.rows, (row) => {
            const inputs = row.querySelectorAll("input");
            return { search: inputs[0]?.value ?? "", replacement: inputs[1]?.value ?? "" };
        });
        const invalidIndex = overrides.findIndex(({ search, replacement }) => (
            !search || !replacement || /\s/.test(search) || /\s/.test(replacement)
        ));
        if (invalidIndex !== -1) {
            setStatus(`Row ${invalidIndex + 1} needs whitespace-free source and target values.`, true);
            return;
        }

        saveButton.disabled = true;
        setStatus("Saving…");
        try {
            const response = await api.fetchApi("/coachbate/workflow_path_autofix/overrides", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ overrides }),
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload?.error || `HTTP ${response.status}`);
            }
            customOverrides = Array.isArray(payload?.overrides) ? payload.overrides : overrides;
            setStatus(`${customOverrides.length} rule${customOverrides.length === 1 ? "" : "s"} saved`);
        } catch (error) {
            setStatus(`Failed to save: ${error.message || error}`, true);
        } finally {
            saveButton.disabled = false;
        }
    });

    void loadRows();
    return container;
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
    let basenameMatches = options.filter((option) => basenameLower(option) === basename);

    if (basenameMatches.length === 0) {
        const digitNormalized = basenameDigitNormalized(reference);
        basenameMatches = options.filter((option) => basenameDigitNormalized(option) === digitNormalized);
    }

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

// Splits "ltx-2.3-brock-lora-v3_000011000.safetensors" into
// { family: "ltx-2.3-brock-lora-v3", step: 11000, ext: ".safetensors" }.
// The trailing "$" anchor forces the split onto the *last* digit run, so
// families that themselves contain numbers ("ltx-2.3-...") survive intact.
// Returns null for names with no trailing step counter.
function parseVersionedModelName(value) {
    const base = basenameLower(value);
    const dotIndex = base.lastIndexOf(".");
    if (dotIndex <= 0) {
        return null;
    }

    const match = /^(.*)[_-](\d+)$/.exec(base.slice(0, dotIndex));
    if (!match) {
        return null;
    }

    return { family: match[1], step: Number(match[2]), ext: base.slice(dotIndex) };
}

// Marc-specific: his training automation archives old aitoolkit checkpoints off
// to G: weekly, keeping only the last 2-3 steps. So a workflow referencing a
// step that no longer exists should resolve to the newest *surviving*
// checkpoint of the same lora family under aitoolkit/<job>/, rather than
// going unresolved. Only ever rolls FORWARD to a strictly higher step count —
// never backward to an older/weaker checkpoint, since a fewer-steps LoRA is
// not an acceptable silent substitute. If nothing newer survives, this
// returns null and the reference is left alone (unresolved) rather than
// downgraded. Only reached when the original path resolves to nothing, so an
// existing file is never swapped.
function resolveArchivedAitoolkitLora(reference, options) {
    const wanted = parseVersionedModelName(reference);
    if (!wanted || !Array.isArray(options)) {
        return null;
    }

    let best = null;
    for (const option of options) {
        // Only ever roll forward to a checkpoint sitting directly in its job
        // folder: aitoolkit/<job>/<file>. Anything deeper is a per-job variant
        // or archive subfolder (tast32rank, .embed_cache, ...) that represents a
        // different training config, not a newer checkpoint of the same run.
        const parts = normalizeForCompare(option).split("/");
        if (parts.length !== 3 || parts[0] !== "aitoolkit") {
            continue;
        }

        const candidate = parseVersionedModelName(option);
        if (!candidate || candidate.family !== wanted.family || candidate.ext !== wanted.ext) {
            continue;
        }

        // The job folder must itself be named for the lora family. A checkpoint
        // whose filename matches but which lives under some other job's folder
        // belongs to a renamed/different run, not a newer step of this one.
        if (parts[1] !== wanted.family) {
            continue;
        }

        // Never step backward to a lower step count than what the workflow
        // actually asked for.
        if (candidate.step <= wanted.step) {
            continue;
        }

        if (!best || candidate.step > best.step) {
            best = { step: candidate.step, option };
        }
    }

    return best?.option ?? null;
}

function mergeModelOptions(widgetOptions, freshOptions) {
    const merged = new Set(Array.isArray(widgetOptions) ? widgetOptions : []);
    for (const option of Array.isArray(freshOptions) ? freshOptions : []) {
        merged.add(option);
    }
    return Array.from(merged);
}

async function ensureLoraModelOptionsLoaded() {
    if (!loraModelOptionsPromise) {
        loraModelOptionsPromise = api.fetchApi("/models/loras")
            .then(async (response) => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const payload = await response.json();
                return Array.isArray(payload) ? payload : [];
            })
            .catch((error) => {
                console.warn("[CoachBate] Failed to load lora model list for workflow path auto-fix:", error);
                return [];
            });
    }

    return loraModelOptionsPromise;
}

// rgthree's "Power Lora Loader" node stores each lora row as a custom
// canvas widget whose value is an object like { on, lora, strength,
// strengthTwo } rather than a plain string with widget.options.values —
// so it needs its own resolution path against ComfyUI's /models/loras list.
function isPowerLoraRowWidget(widget) {
    return typeof widget?.name === "string"
        && widget.name.startsWith("lora_")
        && widget.value
        && typeof widget.value === "object"
        && typeof widget.value.lora === "string";
}

// `info`, when passed, receives `via` describing which tier matched — used only
// for console reporting, so a downgrade from the archive tier is visible.
function resolveWidgetValue(currentValue, options, { isLora = false, info = null } = {}) {
    if (typeof currentValue !== "string") {
        return null;
    }

    for (const override of customOverrides) {
        const overrideCandidate = applyCustomOverride(currentValue, override);
        const resolvedOverride = resolveOptionReference(overrideCandidate, options);
        if (resolvedOverride && resolvedOverride !== currentValue) {
            if (info) info.via = "override";
            return resolvedOverride;
        }
    }

    const resolvedValue = resolveOptionReference(currentValue, options);
    if (resolvedValue && resolvedValue !== currentValue) {
        if (info) info.via = "path";
        return resolvedValue;
    }

    if (isLora && !resolvedValue) {
        const archivedValue = resolveArchivedAitoolkitLora(currentValue, options);
        if (archivedValue && archivedValue !== currentValue) {
            if (info) info.via = "archive";
            return archivedValue;
        }
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

async function maybeFixWorkflowModelPaths() {
    let updatedCount = 0;
    const replacements = [];
    let loraModelOptions = null;

    const nodesToVisit = [];
    walkGraph(app.graph, (node) => nodesToVisit.push(node));

    for (const node of nodesToVisit) {
        for (const [index, widget] of (node.widgets ?? []).entries()) {
            if (isPowerLoraRowWidget(widget)) {
                if (loraModelOptions === null) {
                    loraModelOptions = await ensureLoraModelOptionsLoaded();
                }

                const info = {};
                const resolvedLora = resolveWidgetValue(widget.value.lora, loraModelOptions, { isLora: true, info });
                if (!resolvedLora) {
                    continue;
                }

                const originalValue = widget.value.lora;
                widget.value = { ...widget.value, lora: resolvedLora };
                if (Array.isArray(node.widgets_values) && index < node.widgets_values.length) {
                    const currentEntry = node.widgets_values[index];
                    node.widgets_values[index] = currentEntry && typeof currentEntry === "object"
                        ? { ...currentEntry, lora: resolvedLora }
                        : currentEntry;
                }

                replacements.push({
                    node: node.type,
                    widget: widget.name,
                    original: originalValue,
                    replacement: resolvedLora,
                    via: info.via,
                });
                updatedCount++;
                continue;
            }

            let options = widget?.options?.values;
            const isLora = typeof widget?.name === "string" && widget.name.toLowerCase().includes("lora");
            if (isLora) {
                // widget.options.values is a snapshot captured when the node's
                // combo was built (page load / object_info fetch) and does not
                // pick up files created afterwards — e.g. a training run that
                // finished after ComfyUI started. Trusting it alone as "what
                // exists" is exactly what let the archive fallback below treat
                // a freshly-written, genuinely-present checkpoint as missing
                // and step back to an older one. Union it with a fresh
                // /models/loras fetch so an exact match against the real
                // current file always wins before any fallback runs.
                if (loraModelOptions === null) {
                    loraModelOptions = await ensureLoraModelOptionsLoaded();
                }
                options = mergeModelOptions(options, loraModelOptions);
            }
            const info = {};
            const resolvedValue = resolveWidgetValue(widget?.value, options, { isLora, info });
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
                node: node.type,
                widget: widget.name,
                original: originalValue,
                replacement: resolvedValue,
                via: info.via,
            });
            updatedCount++;
        }
    }

    if (updatedCount > 0) {
        reportPathReplacementsToConsole(replacements);
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
    if (app.ui.settings.getSettingValue(ENABLED_SETTING_ID) === false) {
        return;
    }

    const workflow = app.extensionManager?.workflow?.activeWorkflow;
    if (!workflow?.changeTracker) {
        return;
    }

    await ensureCustomOverridesLoaded();

    const { updatedCount } = await maybeFixWorkflowModelPaths();
    if (updatedCount > 0) {
        beginWaitCursor();
        finishWorkflowPathUpdate(workflow, updatedCount);
    }
}

function reportPathReplacementsToConsole(replacements) {
    const rows = replacements.map((entry) => {
        const from = parseVersionedModelName(entry.original);
        const to = parseVersionedModelName(entry.replacement);
        const steppedBack = entry.via === "archive" && from && to && to.step < from.step;

        return {
            node: entry.node,
            widget: entry.widget,
            via: entry.via + (steppedBack ? ` (STEPPED BACK ${from.step} -> ${to.step})` : ""),
            from: entry.original,
            to: entry.replacement,
        };
    });

    const steppedBackCount = rows.filter((row) => row.via.includes("STEPPED BACK")).length;
    const label = `[CoachBate] Workflow model path auto-fix: ${rows.length} path${rows.length === 1 ? "" : "s"} updated`
        + (steppedBackCount ? `, ${steppedBackCount} stepped back to an older checkpoint` : "");

    console.groupCollapsed(label);
    console.table(rows);
    for (const row of rows) {
        console.log(`  ${row.via}\n    from: ${row.from}\n      to: ${row.to}`);
    }
    console.groupEnd();
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

    settings: [
        {
            id: ENABLED_SETTING_ID,
            category: ["CoachBate", "Model Path Replacements"],
            name: "Enable model path replacements",
            type: "boolean",
            defaultValue: true,
            sortOrder: 110,
            tooltip: "Automatically repair missing model and LoRA paths when a workflow is opened.",
            onChange(value) {
                for (const container of document.querySelectorAll(`.${OVERRIDE_SETTINGS_CLASS}`)) {
                    container.hidden = value === false;
                }
            },
        },
        {
            id: "CoachBate.WorkflowModelPathAutoFix.Overrides",
            category: ["CoachBate", "Model Path Replacements"],
            name: "Custom replacements",
            type: renderOverrideSettings,
            defaultValue: "",
            sortOrder: 100,
            tooltip: "Ordered source and target replacements stored in workflow_path_autofix_overrides.txt.",
        },
    ],

    async afterConfigureGraph() {
        await handleConfiguredGraph();
    },
});
