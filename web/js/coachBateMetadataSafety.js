import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { setWidgetConfig } from "../../../extensions/core/widgetInputs.js";
import { applyTextReplacements } from "../../../scripts/utils.js";

let isWindowsServer = false;
fetch("/coachbate/platform")
    .then((response) => response.json())
    .then((data) => {
        isWindowsServer = data.windows === true;
    })
    .catch(() => {});

const videoCombineWidgetOrder = {
    CoachBateVideoCombine: [
        "frame_rate",
        "loop_count",
        "filename_prefix",
        "format",
        "pingpong",
        "save_output",
        "api_key_behavior",
    ],
};

function chainCallback(object, property, callback) {
    if (!object) {
        return;
    }
    if (property in object && object[property]) {
        const original = object[property];
        object[property] = function () {
            const result = original.apply(this, arguments);
            return callback.apply(this, arguments) ?? result;
        };
    } else {
        object[property] = callback;
    }
}

function findFirstNode(items) {
    for (const item of items ?? []) {
        if (item instanceof LGraphNode) {
            return item;
        }
    }
    return undefined;
}

function fitHeight(node) {
    node.setSize([node.size[0], node.computeSize([node.size[0], node.size[1]])[1]]);
    node?.graph?.setDirtyCanvas(true);
}

function startDraggingItems(node, pointer) {
    app.canvas.emitBeforeChange();
    app.canvas.graph?.beforeChange();
    pointer.finally = () => {
        app.canvas.isDragging = false;
        app.canvas.graph?.afterChange();
        app.canvas.emitAfterChange();
    };
    app.canvas.processSelect(node, pointer.eDown, true);
    app.canvas.isDragging = true;
}

function processDraggedItems(e) {
    if (e.shiftKey || LiteGraph.alwaysSnapToGrid) {
        app.canvas?.graph?.snapToGrid(app.canvas.selectedItems);
    }
    app.canvas.dirty_canvas = true;
    app.canvas.dirty_bgcanvas = true;
    app.canvas.onNodeMoved?.(findFirstNode(app.canvas.selectedItems));
}

function allowDragFromWidget(widget) {
    widget.onPointerDown = function (pointer, node) {
        pointer.onDragStart = () => startDraggingItems(node, pointer);
        pointer.onDragEnd = processDraggedItems;
        app.canvas.dirty_canvas = true;
        return true;
    };
}

function addDateFormatting(nodeType, field) {
    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        const widget = this.widgets.find((item) => item.name === field);
        if (!widget) {
            return;
        }
        widget.serializeValue = () => applyTextReplacements(app, widget.value);
    });
}

function useVideoCombineKVState(nodeType) {
    if (nodeType.prototype._coachbateKVStatePatched) {
        return;
    }
    nodeType.prototype._coachbateKVStatePatched = true;

    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        chainCallback(this, "onConfigure", function (info) {
            if (!this.widgets || typeof info.widgets_values !== "object") {
                return;
            }

            let widgetDict = info.widgets_values;
            if (info.widgets_values.length) {
                const widgetOrder = videoCombineWidgetOrder[this.type];
                if (widgetOrder && info.widgets_values.length >= widgetOrder.length) {
                    widgetDict = {};
                    for (let i = 0; i < widgetOrder.length; i++) {
                        widgetDict[widgetOrder[i]] = info.widgets_values[i];
                    }
                }
            }

            if (widgetDict.videopreview?.params?.force_size) {
                delete widgetDict.videopreview.params.force_size;
            }

            const inputs = {};
            for (const input of this.inputs) {
                inputs[input.name] = input;
            }

            if (widgetDict.length === undefined) {
                for (const widget of this.widgets) {
                    if (widget.type === "button") {
                        continue;
                    }
                    if (widget.name in widgetDict) {
                        widget.value = widgetDict[widget.name];
                        widget.callback?.(widget.value);
                    } else {
                        const inputData = LiteGraph.getNodeType(this.type).nodeData.input;
                        let initialValue = null;
                        if (inputData?.required?.hasOwnProperty(widget.name)) {
                            if (inputData.required[widget.name][1]?.hasOwnProperty("default")) {
                                initialValue = inputData.required[widget.name][1].default;
                            } else if (inputData.required[widget.name][0].length) {
                                initialValue = inputData.required[widget.name][0][0];
                            }
                        } else if (inputData?.optional?.hasOwnProperty(widget.name)) {
                            if (inputData.optional[widget.name][1]?.hasOwnProperty("default")) {
                                initialValue = inputData.optional[widget.name][1].default;
                            } else if (inputData.optional[widget.name][0].length) {
                                initialValue = inputData.optional[widget.name][0][0];
                            }
                        }
                        if (initialValue) {
                            widget.value = initialValue;
                            widget.callback?.(widget.value);
                        }
                    }
                    if (widget.name in inputs && widget.config) {
                        setWidgetConfig(inputs[widget.name], widget.config);
                    }
                }
            } else if (info?.widgets_values?.length !== this.widgets.length) {
                app.ui.dialog.show(`Failed to restore node: ${this.title}\nPlease remove and re-add it.`);
                this.bgcolor = "#C00";
            }
        });

        chainCallback(this, "onSerialize", function (info) {
            info.widgets_values = {};
            if (!this.widgets) {
                return;
            }
            for (const widget of this.widgets) {
                info.widgets_values[widget.name] = widget.value;
            }
        });
    });
}

function addVAEInputToggle(nodeType) {
    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        this.reject_ue_connection = (input) => input?.name === "vae";
    });
    chainCallback(nodeType.prototype, "onConnectionsChange", function (contype, slot, iscon, linkInfo) {
        if (contype !== LiteGraph.INPUT || slot !== 3 || this.inputs[3]?.type !== "VAE") {
            return;
        }

        if (iscon && linkInfo) {
            if (this.linkTimeout) {
                clearTimeout(this.linkTimeout);
                this.linkTimeout = false;
            } else if (this.inputs[0].type === "IMAGE") {
                this.linkTimeout = setTimeout(() => {
                    if (this.inputs[0].type !== "IMAGE") {
                        return;
                    }
                    this.linkTimeout = false;
                    this.disconnectInput(0);
                }, 50);
            }
            this.inputs[0].type = "LATENT";
        } else {
            if (this.inputs[0].type === "LATENT") {
                this.linkTimeout = setTimeout(() => {
                    this.linkTimeout = false;
                    this.disconnectInput(0);
                }, 50);
            }
            this.inputs[0].type = "IMAGE";
        }
    });
}

function addVideoPreview(nodeType, isInput = true) {
    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        const element = document.createElement("div");
        const previewNode = this;
        const previewWidget = this.addDOMWidget("videopreview", "preview", element, {
            serialize: false,
            hideOnZoom: false,
            getValue() {
                return element.value;
            },
            setValue(value) {
                element.value = value;
            },
        });

        allowDragFromWidget(previewWidget);
        previewWidget.computeSize = function (width) {
            if (this.aspectRatio && !this.parentEl.hidden) {
                let height = (previewNode.size[0] - 20) / this.aspectRatio + 10;
                if (!(height > 0)) {
                    height = 0;
                }
                this.computedHeight = height + 10;
                return [width, height];
            }
            return [width, -4];
        };

        for (const eventName of ["contextmenu", "pointerdown", "mousewheel", "pointermove", "pointerup"]) {
            element.addEventListener(eventName, (event) => {
                event.preventDefault();
                const callbackName = {
                    contextmenu: "_mousedown_callback",
                    pointerdown: "_mousedown_callback",
                    mousewheel: "_mousewheel_callback",
                    pointermove: "_mousemove_callback",
                    pointerup: "_mouseup_callback",
                }[eventName];
                return app.canvas[callbackName](event);
            }, true);
        }

        element.addEventListener("dragover", (event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
            app.dragOverNode = this;
        });

        previewWidget.value = {
            hidden: false,
            paused: false,
            params: {},
            muted: app.ui.settings.getSettingValue("VHS.DefaultMute"),
        };
        previewWidget.parentEl = document.createElement("div");
        previewWidget.parentEl.className = "vhs_preview";
        previewWidget.parentEl.style.width = "100%";
        element.appendChild(previewWidget.parentEl);

        previewWidget.videoEl = document.createElement("video");
        previewWidget.videoEl.controls = false;
        previewWidget.videoEl.loop = true;
        previewWidget.videoEl.muted = true;
        previewWidget.videoEl.style.width = "100%";
        previewWidget.videoEl.addEventListener("loadedmetadata", () => {
            previewWidget.aspectRatio = previewWidget.videoEl.videoWidth / previewWidget.videoEl.videoHeight;
            fitHeight(this);
        });
        previewWidget.videoEl.addEventListener("error", () => {
            previewWidget.parentEl.hidden = true;
            fitHeight(this);
        });
        previewWidget.videoEl.onmouseenter = () => {
            previewWidget.videoEl.muted = previewWidget.value.muted;
        };
        previewWidget.videoEl.onmouseleave = () => {
            previewWidget.videoEl.muted = true;
        };

        previewWidget.imgEl = document.createElement("img");
        previewWidget.imgEl.style.width = "100%";
        previewWidget.imgEl.hidden = true;
        previewWidget.imgEl.onload = () => {
            previewWidget.aspectRatio = previewWidget.imgEl.naturalWidth / previewWidget.imgEl.naturalHeight;
            fitHeight(this);
        };
        previewWidget.parentEl.appendChild(previewWidget.videoEl);
        previewWidget.parentEl.appendChild(previewWidget.imgEl);

        let timeout = null;
        this.updateParameters = (params, forceUpdate) => {
            if (!previewWidget.value.params) {
                if (typeof previewWidget.value !== "object") {
                    previewWidget.value = { hidden: false, paused: false };
                }
                previewWidget.value.params = {};
            }
            if (!Object.entries(params).some(([key, value]) => previewWidget.value.params[key] !== value)) {
                return;
            }
            Object.assign(previewWidget.value.params, params);
            if (!forceUpdate && app.ui.settings.getSettingValue("VHS.AdvancedPreviews") === "Never") {
                return;
            }
            if (timeout) {
                clearTimeout(timeout);
            }
            if (forceUpdate) {
                previewWidget.updateSource();
            } else {
                timeout = setTimeout(() => previewWidget.updateSource(), 100);
            }
        };

        previewWidget.updateSource = function () {
            if (!this.value.params) {
                return;
            }
            const params = {};
            let advancedPreview = app.ui.settings.getSettingValue("VHS.AdvancedPreviews");
            if (advancedPreview === "Never") {
                advancedPreview = false;
            } else if (advancedPreview === "Input Only") {
                advancedPreview = isInput;
            } else {
                advancedPreview = true;
            }

            Object.assign(params, this.value.params);
            params.timestamp = Date.now();
            this.parentEl.hidden = this.value.hidden;
            if (
                params.format?.split("/")[0] === "video"
                || (advancedPreview && params.format?.split("/")[1] === "gif")
                || params.format === "folder"
            ) {
                this.videoEl.autoplay = !this.value.paused && !this.value.hidden;
                if (!advancedPreview) {
                    this.videoEl.src = api.apiURL("/view?" + new URLSearchParams(params));
                } else {
                    let targetWidth = (previewNode.size[0] - 20) * 2 || 256;
                    const minWidth = app.ui.settings.getSettingValue("VHS.AdvancedPreviewsMinWidth");
                    if (targetWidth < minWidth) {
                        targetWidth = minWidth;
                    }
                    if (!params.custom_width || !params.custom_height) {
                        params.force_size = `${targetWidth}x?`;
                    } else {
                        const aspectRatio = params.custom_width / params.custom_height;
                        params.force_size = `${targetWidth}x${targetWidth / aspectRatio}`;
                    }
                    params.deadline = app.ui.settings.getSettingValue("VHS.AdvancedPreviewsDeadline");
                    this.videoEl.src = api.apiURL("/vhs/viewvideo?" + new URLSearchParams(params));
                }
                this.videoEl.hidden = false;
                this.imgEl.hidden = true;
            } else if (params.format?.split("/")[0] === "image") {
                this.imgEl.src = api.apiURL("/view?" + new URLSearchParams(params));
                this.videoEl.hidden = true;
                this.imgEl.hidden = false;
            }

            delete previewNode.video_query;
            const doQuery = async () => {
                if (!previewWidget?.value?.params?.filename) {
                    return;
                }
                const queryUrl = api.apiURL("/vhs/queryvideo?" + new URLSearchParams(previewWidget.value.params));
                try {
                    const queryResponse = await fetch(queryUrl);
                    previewNode.video_query = await queryResponse.json();
                } catch (_) {
                    // Preview metadata is best-effort and should never break the node.
                }
            };
            doQuery();
        };
        previewWidget.callback = previewWidget.updateSource;
    });
}

let copiedPath;

function addPreviewOptions(nodeType) {
    chainCallback(nodeType.prototype, "getExtraMenuOptions", function (_, options) {
        const previewWidget = this.widgets.find((item) => item.name === "videopreview");
        if (!previewWidget) {
            return;
        }

        const newOptions = [];
        let url = null;
        if (previewWidget.videoEl?.hidden === false && previewWidget.videoEl.src) {
            if (["input", "output", "temp"].includes(previewWidget.value.params.type)) {
                url = api.apiURL("/view?" + new URLSearchParams(previewWidget.value.params));
                url = url.replace("%2503d", "001");
            }
        } else if (previewWidget.imgEl?.hidden === false && previewWidget.imgEl.src) {
            url = new URL(previewWidget.imgEl.src);
        }

        if (this.video_query?.source) {
            const info = `${this.video_query.source.size.join("x")}@${this.video_query.source.fps}fps ${this.video_query.source.frames}frames`;
            newOptions.push({ content: info, disabled: true });
        }

        if (url) {
            newOptions.push(
                {
                    content: "Open preview",
                    callback: () => window.open(url, "_blank"),
                },
                {
                    content: "Save preview",
                    callback: () => {
                        const link = document.createElement("a");
                        link.href = url;
                        link.setAttribute("download", previewWidget.value.params.filename);
                        document.body.append(link);
                        link.click();
                        requestAnimationFrame(() => link.remove());
                    },
                },
            );
            if (previewWidget.value.params.fullpath) {
                copiedPath = previewWidget.value.params.fullpath;
                const blob = new Blob([copiedPath], { type: "text/plain" });
                newOptions.push({
                    content: "Copy output filepath",
                    callback: async () => {
                        await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
                    },
                });
            }
            if (previewWidget.value.params.workflow) {
                const workflowParams = { ...previewWidget.value.params, filename: previewWidget.value.params.workflow };
                const workflowUrl = api.apiURL("/view?" + new URLSearchParams(workflowParams));
                newOptions.push({
                    content: "Save workflow image",
                    callback: () => {
                        const link = document.createElement("a");
                        link.href = workflowUrl;
                        link.setAttribute("download", previewWidget.value.params.workflow);
                        document.body.append(link);
                        link.click();
                        requestAnimationFrame(() => link.remove());
                    },
                });
            }
        }

        if (previewWidget.videoEl.hidden === false) {
            const pauseDescription = `${previewWidget.value.paused ? "Resume" : "Pause"} preview`;
            newOptions.push({
                content: pauseDescription,
                callback: () => {
                    if (previewWidget.value.paused) {
                        previewWidget.videoEl?.play();
                    } else {
                        previewWidget.videoEl?.pause();
                    }
                    previewWidget.value.paused = !previewWidget.value.paused;
                },
            });
        }

        const visibilityDescription = `${previewWidget.value.hidden ? "Show" : "Hide"} preview`;
        newOptions.push({
            content: visibilityDescription,
            callback: () => {
                if (!previewWidget.videoEl.hidden && !previewWidget.value.hidden) {
                    previewWidget.videoEl.pause();
                } else if (previewWidget.value.hidden && !previewWidget.videoEl.hidden && !previewWidget.value.paused) {
                    previewWidget.videoEl.play();
                }
                previewWidget.value.hidden = !previewWidget.value.hidden;
                previewWidget.parentEl.hidden = previewWidget.value.hidden;
                fitHeight(this);
            },
        });

        newOptions.push({
            content: "Sync preview",
            callback: () => {
                for (const preview of document.getElementsByClassName("vhs_preview")) {
                    for (const child of preview.children) {
                        if (child.tagName === "VIDEO") {
                            child.currentTime = 0;
                        } else if (child.tagName === "IMG") {
                            child.src = child.src;
                        }
                    }
                }
            },
        });

        const muteDescription = `${previewWidget.value.muted ? "Unmute" : "Mute"} Preview`;
        newOptions.push({
            content: muteDescription,
            callback: () => {
                previewWidget.value.muted = !previewWidget.value.muted;
            },
        });

        if (options.length > 0 && options[0] != null && newOptions.length > 0) {
            newOptions.push(null);
        }
        options.unshift(...newOptions);
    });
}

function addVideoCombineFormatWidgets(nodeType, nodeData) {
    if (nodeType.prototype._coachbateFormatWidgetsPatched) {
        return;
    }
    nodeType.prototype._coachbateFormatWidgetsPatched = true;

    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        let formatWidget = null;
        let formatWidgetIndex = -1;
        for (let i = 0; i < this.widgets.length; i++) {
            if (this.widgets[i].name === "format") {
                formatWidget = this.widgets[i];
                formatWidgetIndex = i + 1;
                break;
            }
        }

        if (!formatWidget) {
            return;
        }

        let formatWidgetsCount = 0;
        chainCallback(formatWidget, "callback", (value) => {
            const formats = nodeData?.input?.required?.format?.[1]?.formats;
            const newWidgets = [];

            if (formats?.[value]) {
                for (const widgetDefinition of formats[value]) {
                    let type = widgetDefinition[2]?.widgetType ?? widgetDefinition[1];
                    if (Array.isArray(type)) {
                        type = "COMBO";
                    }
                    app.widgets[type](this, widgetDefinition[0], widgetDefinition.slice(1), app);
                    const widget = this.widgets.pop();
                    widget.config = widgetDefinition.slice(1);
                    newWidgets.push(widget);
                }
            }

            const removed = this.widgets.splice(formatWidgetIndex, formatWidgetsCount, ...newWidgets);
            const newNames = new Set(newWidgets.map((widget) => widget.name));
            for (const widget of removed) {
                widget?.onRemove?.();
                if (newNames.has(widget.name)) {
                    continue;
                }
                const slot = this.inputs.findIndex((input) => input.name === widget.name);
                if (slot >= 0) {
                    this.removeInput(slot);
                }
            }

            for (const widget of newWidgets) {
                const existingInput = this.inputs.find((input) => input.name === widget.name);
                if (existingInput) {
                    setWidgetConfig(existingInput, widget.config);
                } else {
                    this.addInput(widget.name, widget.config[0], { widget: { name: widget.name } });
                }
            }

            fitHeight(this);
            formatWidgetsCount = newWidgets.length;
        });

        // VHS relies on ComfyUI calling this during creation; call it explicitly
        // so CoachBate mirrors Video Combine even though VHS special-cases its own class name.
        formatWidget.callback?.(formatWidget.value);
    });
}

function addVideoCombineParity(nodeType, nodeData) {
    if (nodeType.prototype._coachbateVideoCombineParityPatched) {
        return;
    }
    nodeType.prototype._coachbateVideoCombineParityPatched = true;

    useVideoCombineKVState(nodeType);
    addDateFormatting(nodeType, "filename_prefix");
    chainCallback(nodeType.prototype, "onExecuted", function (message) {
        if (message?.gifs) {
            this.updateParameters?.(message.gifs[0], true);
        }
    });
    addVideoPreview(nodeType, false);
    addPreviewOptions(nodeType);
    addVideoCombineFormatWidgets(nodeType, nodeData);
    addVAEInputToggle(nodeType);
}

app.registerExtension({
    name: "CoachBate.MetadataSafety",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name === "CoachBateVideoCombine") {
            addVideoCombineParity(nodeType, nodeData);
        }
    },

    async nodeCreated(node) {
        if (node.comfyClass !== "CoachBateStripAPIKeyMetadata") {
            return;
        }

        if (!isWindowsServer) {
            return;
        }

        const browseButton = node.addWidget("button", "Browse for PNG / MP4...", null, () => {
            fetch("/coachbate/browse_media")
                .then((response) => response.json())
                .then((data) => {
                    if (!data.path) {
                        return;
                    }
                    const widget = node.widgets?.find((item) => item.name === "file_path");
                    if (widget) {
                        widget.value = data.path;
                        app.graph.setDirtyCanvas(true, true);
                    }
                })
                .catch((error) => console.error("[CoachBate] media browse failed:", error));
        });

        const index = node.widgets.indexOf(browseButton);
        if (index > 1) {
            node.widgets.splice(index, 1);
            node.widgets.splice(1, 0, browseButton);
        }
    },
});
