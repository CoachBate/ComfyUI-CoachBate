"""Expand the selected record's ComfyUI graph without loading any models here."""
import json

from comfy_execution.graph_utils import GraphBuilder, is_link


class CoachBateShotGraph:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"shot_payload": ("STRING", {"forceInput": True})}}

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "execute"
    CATEGORY = "CoachBate"
    DESCRIPTION = "Expands the selected shot's render_graph using native ComfyUI nodes. No per-shot JSON files are needed. Connect to Save Video."

    @classmethod
    def execute(cls, shot_payload):
        payload = json.loads(shot_payload)
        shot = payload["shot"]
        if not shot.get("render_graph"):
            raise ValueError(f"Shot {shot.get('shot_id')} has no render_graph; disable it until its H3 setup is ready.")
        recipe = shot["render_graph"]
        graph = GraphBuilder()
        nodes = {key: graph.node(node["class_type"], id=key)
                 for key, node in recipe["nodes"].items()}
        for key, node in recipe["nodes"].items():
            for name, value in node["inputs"].items():
                if is_link(value):
                    if value[0] == recipe["loader_id"]:
                        value = payload["loader_outputs"][value[1]]
                    else:
                        value = nodes[value[0]].out(value[1])
                nodes[key].set_input(name, value)
        output = recipe["video_output"]
        return {"result": (nodes[output[0]].out(output[1]),), "expand": graph.finalize()}
