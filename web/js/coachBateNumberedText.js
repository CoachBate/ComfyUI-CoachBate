import { app } from "../../../scripts/app.js";
import { attachNumberedGutter, installGutterCleanup } from "./coachBateGutter.js";

app.registerExtension({
    name: "CoachBate.NumberedText",

    beforeRegisterNodeDef(nodeType) {
        if (nodeType.comfyClass !== "CoachBateNumberedText") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            this._cb_gutter_cleanup = attachNumberedGutter(this, "multiline_text");
        };

        installGutterCleanup(nodeType);
    },
});
