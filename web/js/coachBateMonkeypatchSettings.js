import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const PATCHES = [
    {
        key: "vhs_video_combine",
        label: "VHS Video Combine metadata protection",
        description: "Adds API-key handling and safe metadata output behavior to VHS_VideoCombine.",
    },
    {
        key: "gemma_env_key",
        label: "Gemma API key environment fallback",
        description: "Lets GemmaAPITextEncode use LTXV_API_KEY when its API-key widget is blank.",
    },
    {
        key: "whisper_dtype",
        label: "VRGDG Whisper dtype correction",
        description: "Casts Whisper input features to the loaded checkpoint dtype in VRGDG transcribe nodes.",
    },
    {
        key: "vhs_preview_loop",
        label: "VHS preview event-loop fallback",
        description: "Serves VHS video and audio previews untranscoded on Windows event loops that cannot run ffmpeg, instead of erroring.",
    },
    {
        key: "h3_facerefine",
        label: "H3-FaceRefine fixes (off by default)",
        description: "For ComfyUI-H3-FaceRefine: resets the cached YOLO predictor per run (stale ones missed nearly every frame), pads off-grid clips by repeating the last frame before VAE encode (the last frames became a different scene), and adds crop_size_from = longest_side to the tracker for subjects wider than tall.",
    },
];

function renderMonkeypatchSettings() {
    const container = document.createElement("div");
    container.style.cssText = "display:flex;flex-direction:column;gap:12px;width:clamp(320px,calc(100vw - 340px),900px);font-size:14px;line-height:1.4;";

    const explanation = document.createElement("div");
    explanation.textContent = "These patches modify third-party nodes. CoachBate-only node enhancements are not listed here.";
    explanation.style.cssText = "font-size:13px;opacity:.8;";
    container.append(explanation);

    const inputs = new Map();
    for (const patch of PATCHES) {
        const row = document.createElement("label");
        row.style.cssText = "display:grid;grid-template-columns:auto 1fr;column-gap:8px;align-items:start;";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.disabled = true;
        input.style.cssText = "width:16px;height:16px;margin-top:2px;";
        const text = document.createElement("span");
        const title = document.createElement("span");
        title.textContent = patch.label;
        title.style.cssText = "font-size:15px;font-weight:600;";
        const description = document.createElement("span");
        description.textContent = patch.description;
        description.style.cssText = "display:block;font-size:13px;opacity:.75;margin-top:2px;";
        text.append(title, description);
        row.append(input, text);
        container.append(row);
        inputs.set(patch.key, input);
    }

    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;align-items:center;gap:8px;";
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save patch settings";
    saveButton.disabled = true;
    const status = document.createElement("span");
    status.style.cssText = "font-size:13px;";
    actions.append(saveButton, status);
    container.append(actions);

    const setStatus = (message, isError = false) => {
        status.textContent = message;
        status.style.color = isError ? "var(--error-text, #e66)" : "";
    };

    const setEnabled = (enabled) => {
        saveButton.disabled = !enabled;
        for (const input of inputs.values()) input.disabled = !enabled;
    };

    saveButton.addEventListener("click", async () => {
        const settings = Object.fromEntries(
            PATCHES.map(({ key }) => [key, inputs.get(key).checked]),
        );
        setEnabled(false);
        setStatus("Saving…");
        try {
            const response = await api.fetchApi("/coachbate/monkeypatch_settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ settings }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
            setStatus("Saved. Restart ComfyUI for changes to take effect.");
        } catch (error) {
            setStatus(`Failed to save: ${error.message || error}`, true);
        } finally {
            setEnabled(true);
        }
    });

    void (async () => {
        setStatus("Loading…");
        try {
            const response = await api.fetchApi("/coachbate/monkeypatch_settings");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
            for (const { key } of PATCHES) inputs.get(key).checked = payload.settings?.[key] !== false;
            setStatus("Changes require a ComfyUI restart.");
            setEnabled(true);
        } catch (error) {
            setStatus(`Failed to load: ${error.message || error}`, true);
        }
    })();

    return container;
}

app.registerExtension({
    name: "CoachBate.MonkeypatchSettings",
    settings: [
        {
            id: "CoachBate.MonkeypatchSettings.Controls",
            category: ["CoachBate", "Compatibility Patches"],
            name: "Third-party patches",
            type: renderMonkeypatchSettings,
            defaultValue: "",
            sortOrder: 200,
            tooltip: "Enable or disable CoachBate compatibility patches for third-party nodes.",
        },
    ],
});
