"""
submit_workflow.py  —  Convert frontend workflow JSON to ComfyUI API format and queue it.

Usage:
    python submit_workflow.py <workflow.json>
"""

import sys, os, json, uuid, time, urllib.request, urllib.error

COMFY_URL = "http://127.0.0.1:8188"
WF_PATH   = r"C:\Data\ComfyUser\default\workflows\LTX-FreeFuse.json"

def get_object_info():
    with urllib.request.urlopen(f"{COMFY_URL}/object_info") as r:
        return json.loads(r.read())

def frontend_to_api(wf, obj_info):
    """
    Convert ComfyUI frontend workflow JSON to the API prompt format.

    Frontend: nodes=[{id, type, inputs, outputs, widgets_values}], links=[[id,src,src_slot,dst,dst_slot,type]]
    API:      {node_id: {class_type, inputs: {name: value|[node_id, slot]}}}
    """
    # Build link lookup: link_id → (src_node_id, src_slot)
    link_src = {}
    for lnk in wf.get("links", []):
        link_id, src_node, src_slot, dst_node, dst_slot, ltype = lnk
        link_src[link_id] = (str(src_node), src_slot)

    # Build node input → link_id lookup: (node_id, input_slot) → link_id
    # From inputs array with links
    node_input_links = {}  # (node_id, input_name) → link_id
    for node in wf.get("nodes", []):
        for inp in node.get("inputs", []):
            if inp.get("link") is not None:
                node_input_links[(node["id"], inp["name"])] = inp["link"]

    api_prompt = {}

    for node in wf.get("nodes", []):
        nid   = str(node["id"])
        ntype = node.get("type", "")

        # Skip muted/bypassed nodes (mode != 0)
        if node.get("mode", 0) != 0:
            continue

        # Get node schema from object_info
        schema = obj_info.get(ntype)
        if schema is None:
            # Unknown node type — try to build inputs from what we know
            api_prompt[nid] = {"class_type": ntype, "inputs": {}}
            continue

        inputs_def = schema.get("input", {})
        required   = inputs_def.get("required", {})
        optional   = inputs_def.get("optional", {})
        all_inputs = {**required, **optional}

        # Separate widget inputs (no link) from connector inputs (have link)
        connector_inputs = {inp["name"] for inp in node.get("inputs", [])}

        # Widget values are in order matching the widget inputs in schema
        widget_values = list(node.get("widgets_values", []))
        widget_idx = 0

        built_inputs = {}

        for inp_name, inp_def in all_inputs.items():
            inp_type = inp_def[0] if inp_def else "STRING"

            # Is this a connector input with a link?
            link_id = node_input_links.get((node["id"], inp_name))
            if link_id is not None and link_id in link_src:
                src_node, src_slot = link_src[link_id]
                built_inputs[inp_name] = [src_node, src_slot]
                continue

            # Is this input optional with no link and no widget? Skip.
            if inp_name not in connector_inputs and isinstance(inp_type, str) and inp_type in (
                "MODEL", "CLIP", "VAE", "LATENT", "CONDITIONING", "IMAGE", "MASK",
                "AUDIO", "SIGMAS", "SAMPLER", "NOISE", "GUIDER",
                "LTXFREEFUSE_DATA", "LTXFREEFUSE_MASKS",
                "LATENT_UPSCALE_MODEL", "GUIDE_DATA", "VHS_BatchManager",
            ):
                # Connector type with no link — optional, skip
                continue

            # Widget value
            if widget_idx < len(widget_values):
                built_inputs[inp_name] = widget_values[widget_idx]
                widget_idx += 1

        api_prompt[nid] = {
            "class_type": ntype,
            "inputs": built_inputs,
            "_meta": {"title": node.get("title", ntype)},
        }

    return api_prompt


def queue_prompt(api_prompt):
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": api_prompt, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()), client_id


def wait_for_completion(prompt_id, client_id, timeout=1800):
    """Poll /history until the prompt appears (completed or errored)."""
    print(f"  Queued prompt_id={prompt_id}, waiting for completion…")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}") as r:
            hist = json.loads(r.read())
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            completed = status.get("completed", False)
            msgs = status.get("messages", [])
            # Show any messages
            for m in msgs:
                print(f"    [{m[0]}] {m[1] if len(m)>1 else ''}")
            if completed:
                outputs = entry.get("outputs", {})
                print(f"  Completed! Outputs from nodes: {list(outputs.keys())}")
                return outputs
            # Check for errors
            for m in msgs:
                if m[0] == "execution_error":
                    raise RuntimeError(f"Execution error: {m}")
        elapsed = int(time.time() - start)
        print(f"  Still running… ({elapsed}s)", end="\r")
    raise TimeoutError(f"Timed out after {timeout}s")


def main():
    wf_path = sys.argv[1] if len(sys.argv) > 1 else WF_PATH
    print(f"Loading workflow: {wf_path}")
    with open(wf_path) as f:
        wf = json.load(f)

    print("Fetching node schemas from ComfyUI…")
    obj_info = get_object_info()
    print(f"  Got schemas for {len(obj_info)} node types")

    print("Converting to API format…")
    api_prompt = frontend_to_api(wf, obj_info)
    active = {k: v for k, v in api_prompt.items() if v["inputs"] or True}
    print(f"  {len(active)} active nodes")

    # Save API prompt for inspection
    debug_path = wf_path.replace(".json", "_api.json")
    with open(debug_path, "w") as f:
        json.dump(api_prompt, f, indent=2)
    print(f"  Saved API prompt to {debug_path}")

    print("Queuing prompt…")
    try:
        result, client_id = queue_prompt(api_prompt)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Queue error {e.code}: {body[:500]}")
        sys.exit(1)

    prompt_id = result.get("prompt_id")
    print(f"  prompt_id = {prompt_id}")

    outputs = wait_for_completion(prompt_id, client_id)
    print("\nDone!")
    for node_id, node_out in outputs.items():
        print(f"  Node {node_id}: {list(node_out.keys())}")


if __name__ == "__main__":
    main()
