# Contributing & Template Guide

This package is designed to be both a working ComfyUI node and a **clean starter template**
that you can fork or copy to build your own custom node packages.

---

## Package structure

```
custom_nodes/coachbate/
├── __init__.py          ← Entry point. Register nodes here.
├── nodes.py             ← Node class definitions (V1 API).
├── web/
│   └── notifications.js ← Frontend JS extension (auto-loaded by ComfyUI).
├── pyproject.toml       ← Package metadata for ComfyUI Manager / Comfy Registry.
├── requirements.txt     ← Python dependencies (installed by ComfyUI Manager).
├── README.md            ← User-facing documentation.
└── CONTRIBUTING.md      ← This file.
```

---

## Adding a new node

### Step 1 — Define the class in `nodes.py`

All nodes use the **V1 (classic) ComfyUI node API**. The minimum required attributes are:

```python
class MyNode:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "my_string": ("STRING", {"default": "hello"}),
                "my_int":    ("INT",    {"default": 1, "min": 0, "max": 100}),
            },
            # "optional": { ... }   ← inputs that can be left disconnected
            # "hidden":   { ... }   ← special ComfyUI-injected values (see below)
        }

    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("result_text", "result_count")   # optional but recommended

    FUNCTION  = "execute"    # name of the method ComfyUI calls
    CATEGORY  = "CoachBate"  # menu path in Add Node

    def execute(self, my_string: str, my_int: int):
        # do work
        return (my_string.upper(), my_int * 2)
```

### Standard type strings

| Type string | Python type | Notes |
|-------------|-------------|-------|
| `"STRING"`  | `str`       | Add `"multiline": True` for textarea |
| `"INT"`     | `int`       | Supports `min`, `max`, `step` |
| `"FLOAT"`   | `float`     | Supports `min`, `max`, `step` |
| `"BOOLEAN"` | `bool`      | Renders as a checkbox |
| `"IMAGE"`   | `torch.Tensor` | Shape: `[B, H, W, C]` float32 |
| `"LATENT"`  | `dict`      | `{"samples": Tensor}` |
| `"MODEL"`   | object      | Loaded checkpoint |
| `"CLIP"`    | object      | CLIP text encoder |
| `"VAE"`     | object      | VAE encoder/decoder |

### Hidden inputs

Declare in the `"hidden"` key of `INPUT_TYPES`. ComfyUI injects these at runtime — they
do **not** appear as sockets in the UI.

```python
"hidden": {
    "prompt":    "PROMPT",     # full workflow dict (for re-queuing with modified params)
    "unique_id": "UNIQUE_ID",  # this node's string ID in the graph
}
```

Then add them as parameters to your execute method:

```python
def execute(self, my_string, prompt=None, unique_id=None):
    ...
```

### IS_CHANGED and OUTPUT_NODE

```python
@classmethod
def IS_CHANGED(cls, **kwargs):
    # Return float("NaN") to force re-execution on every queue run.
    # Without this, ComfyUI may cache the output if widget values didn't change.
    return float("NaN")

OUTPUT_NODE = True
# Mark as True if your node has side effects (HTTP calls, file writes, toasts)
# even when none of its outputs are connected downstream.
```

### Step 2 — Register it in `__init__.py`

```python
from .nodes import MyNode   # add this import

NODE_CLASS_MAPPINGS = {
    "CoachBateShotLoader": CoachBateShotLoader,
    "MyNode": MyNode,        # add this entry
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CoachBateShotLoader": "CoachBate Shot Loader",
    "MyNode": "My Node",     # add this entry
}
```

The key in `NODE_CLASS_MAPPINGS` is the internal class type used in saved workflows.
**Never rename it** after publishing — it will break existing workflows.

---

## Frontend JS extensions

Any `.js` file in the `web/` directory is auto-served by ComfyUI because of
`WEB_DIRECTORY = "./web"` in `__init__.py`.

The minimal extension skeleton:

```javascript
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "CoachBate.MyExtension",   // must be globally unique

    async setup() {
        // Runs once when ComfyUI finishes loading.
        // Good place for: event listeners, DOM setup, global state.
    },

    async nodeCreated(node) {
        // Runs whenever any node is added to the canvas.
        if (node.comfyClass === "MyNode") {
            // Customise this specific node's UI here.
        }
    },
});
```

### Listening for Python events

```javascript
// Python:  PromptServer.instance.send_sync("my.event", {"key": "value"})
// JS:
api.addEventListener("my.event", (event) => {
    const { key } = event.detail;
    console.log(key);
});
```

### Toast notifications (modern ComfyUI frontend)

```javascript
app.extensionManager.toast.add({
    severity: "info",      // "info" | "success" | "warn" | "error"
    summary:  "My Node",
    detail:   "Something happened",
    life:     4000,        // milliseconds
});
```

---

## Testing manually

1. Restart ComfyUI after any Python change.
2. Verify the node appears in **Add Node → CoachBate → [your node name]**.
3. Open browser DevTools (F12) and check the Console tab for:
   - `[CoachBate] Notification extension registered.` on startup
   - Any JS errors from your extension
4. Wire the node into a minimal workflow and run it.
5. Check the ComfyUI terminal for Python `log.info` / `log.error` output prefixed with `[CoachBate]`.

There are no automated tests in this package. For a test suite, see
[pytest-comfyui](https://github.com/BadCafeCode/execution-inversion-demo-comfyui) as a reference.

---

## Publishing to ComfyUI Registry

1. Fill in `pyproject.toml`: `PublisherId`, `Repository` URL, `Icon`.
2. Follow the [Comfy Registry publishing guide](https://docs.comfy.org/registry/publishing).
3. Users will then be able to install via ComfyUI Manager by name.
