import { app } from "../../../scripts/app.js";
import { applyTextReplacements } from "../../../scripts/utils.js";


function setWidgetVisible(widget, visible) {
    if (!widget) return;

    if (widget._cbOriginalComputeSize === undefined) {
        widget._cbOriginalComputeSize = widget.computeSize ?? null;
    }

    widget.hidden = !visible;
    widget.options ??= {};
    widget.options.hidden = !visible;

    if (visible) {
        if (widget._cbOriginalComputeSize) {
            widget.computeSize = widget._cbOriginalComputeSize;
        } else {
            delete widget.computeSize;
        }
    } else {
        widget.computeSize = () => [0, 0];
    }

    if (widget.element) {
        widget.element.style.display = visible ? "" : "none";
    }
}


function resizeNode(node) {
    requestAnimationFrame(() => {
        const computed = node.computeSize?.([node.size[0], node.size[1]]);
        if (computed?.[1]) {
            node.setSize([node.size[0], computed[1]]);
        }
        node.setDirtyCanvas?.(true, true);
        app.graph?.setDirtyCanvas?.(true, true);
    });
}


function bindVisibility(node, controllerName, dependentNames, visibleValue) {
    const controller = node.widgets?.find((widget) => widget.name === controllerName);
    const dependents = dependentNames
        .map((name) => node.widgets?.find((widget) => widget.name === name))
        .filter(Boolean);

    if (!controller || dependents.length !== dependentNames.length) return false;

    const update = (value) => {
        const visible = value === visibleValue;
        for (const widget of dependents) setWidgetVisible(widget, visible);
        resizeNode(node);
    };

    update(controller.value);
    if (!controller._cbTensorRTVisibilityBound) {
        const originalCallback = controller.callback;
        controller.callback = function (value) {
            const result = originalCallback?.apply(this, arguments);
            update(value);
            return result;
        };
        controller._cbTensorRTVisibilityBound = true;
    }
    return true;
}


app.registerExtension({
    name: "CoachBate.TensorRTVideoWidgetVisibility",

    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "CoachBateUpscaleRifeTensorRT") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);

            const setup = () => {
                const filenameWidget = this.widgets?.find(
                    (widget) => widget.name === "filename_prefix",
                );
                if (filenameWidget && !filenameWidget._cbTextReplacementsBound) {
                    filenameWidget.serializeValue = () =>
                        applyTextReplacements(app, filenameWidget.value);
                    filenameWidget._cbTextReplacementsBound = true;
                }
                for (const name of [
                    "rife_model",
                    "rife_precision",
                    "rife_resolution_profile",
                    "rife_custom_min_dimension",
                    "rife_custom_opt_dimension",
                    "rife_custom_max_dimension",
                    "rife_batch_size",
                    "clear_cache_after_n_frames",
                    "keep_rife_model_loaded",
                ]) {
                    setWidgetVisible(
                        this.widgets?.find((widget) => widget.name === name),
                        false,
                    );
                }
                const multiplierWidget = this.widgets?.find(
                    (widget) => widget.name === "rife_multiplier",
                );
                if (multiplierWidget) multiplierWidget.label = "interpolation multiplier";
                const resizeReady = bindVisibility(
                    this,
                    "resize_to",
                    ["resize_width", "resize_height"],
                    "custom",
                );
                if (!resizeReady) requestAnimationFrame(setup);
            };

            requestAnimationFrame(setup);
        };
    },
});
