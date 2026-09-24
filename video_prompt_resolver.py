"""Recover the positive prompt from a ComfyUI-generated image/video's embedded graph.

Lifted verbatim (resolver section) from Marc's
`Documents/scripts/embed_civitai_metadata_windows_python.ps1`, which is the tested,
battle-hardened version: it walks the API `prompt` JSON back from the save node to
the sampler to the encoder, follows links through switches, concatenates, primitives
and the CoachBate capture/batch nodes, and prefers the `workflow` JSON's widget values
where the API snapshot is known to be stale. Keep the two in sync: fix bugs THERE,
then re-copy the section here.

Only `read_comfy_tags()` / `prompt_from_video()` at the bottom are new.
"""

import json
import os
import re
import shutil
import subprocess

# The script writes this when nothing can be recovered; the node reports the
# failure instead, so it stays empty here.
PLACEHOLDER = ""

# Encoder text-input field names, in priority order. Covers CLIPTextEncode
# (text), Qwen/Boogu (prompt), Flux (t5xxl/clip_l), Wan (positive_prompt),
# SDXL-JPS (text_pos) and core SDXL (text_g/text_l). Negative fields
# (negative_prompt / text_neg) are intentionally absent so they are never used.
# `global_prompt` / `local_prompts` belong to timeline-style encoders
# (PromptRelayEncodeTimeline and friends) which hold the prompt in their OWN
# widgets instead of a CLIPTextEncode. Without them the trace walked straight
# past the only node carrying the text.
ENCODER_TEXT_KEYS = ("text", "prompt", "t5xxl", "positive_prompt", "text_pos",
                     "text_g", "clip_l", "text_l", "populated_text",
                     "wildcard_text", "prompt_text",
                     "global_prompt", "local_prompts")
SAVE_HINTS  = ("SaveImage", "SaveVideo", "VideoCombine", "SaveAnimated", "Image Save", "SaveImageWebsocket")
SEED_KEYS   = ("seed", "noise_seed", "rand_seed")


def is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int))


# ── workflow widgets_values map (flattened, incl. subgraphs) ──────────────────
def build_wf_widgets(workflow):
    """Map flattened API node id -> widgets_values, descending into subgraphs.
    A subgraph-instance node (type == subgraph def id) contributes the prefix
    "<instance_id>:" to its inner nodes, matching API ids like "30:6"."""
    out = {}
    if not isinstance(workflow, dict):
        return out
    defs = {sg.get("id"): sg for sg in (workflow.get("definitions") or {}).get("subgraphs", [])}

    def walk(nodes, prefix=""):
        for n in nodes or []:
            key = prefix + str(n.get("id"))
            out[key] = n.get("widgets_values")
            t = n.get("type")
            if t in defs:
                walk(defs[t].get("nodes", []), key + ":")

    walk(workflow.get("nodes", []))
    return out


def wf_text(wf_widgets, nid):
    """First non-empty string in a node's persisted widgets_values, or None."""
    wv = wf_widgets.get(str(nid))
    if isinstance(wv, list):
        for item in wv:
            if isinstance(item, str) and item.strip():
                return item
    return None


# ── value resolvers ───────────────────────────────────────────────────────────
def resolve_bool(prompt, val, depth=0):
    if depth > 30:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() not in ("", "0", "false", "none", "no")
    if is_link(val):
        n = prompt.get(str(val[0]))
        if n:
            ins = n.get("inputs", {})
            for k in ("value", "boolean", "BOOL"):
                if k in ins:
                    return resolve_bool(prompt, ins[k], depth + 1)
    return False


def batch_prompter_text(node):
    """The ONE line a CoachBateBatchPrompter emitted for THIS image.

    The node holds the entire prompt list, which is why the trace used to
    refuse it outright -- embedding all 25 prompts is worse than embedding
    none. But it also records `starting_number`: the 1-BASED ordinal of the
    line that fired, counting NON-BLANK lines only (blank lines separate
    prompts and are not counted). The API `prompt` JSON is snapshot per queued
    job, so unlike the capture-node widget this value is correct per image
    even when the whole batch is queued at once.

    Mirrors CoachBateBatchPrompter.execute() exactly:
        prepend_text + line.strip() + append_text
    Keep in sync with that node if its line handling ever changes."""
    ins = node.get("inputs") or {}
    text = ins.get("multiline_text")
    if not isinstance(text, str) or not text.strip():
        return None                      # linked/absent -> nothing to index into
    try:
        start = max(1, int(ins.get("starting_number", 1) or 1))
    except Exception:
        start = 1
    ordinal, body = 0, None
    for ln in text.split("\n"):
        if ln.strip():
            ordinal += 1
            if ordinal >= start:
                body = ln.strip()
                break
    if not body:
        return None
    pre = ins.get("prepend_text") if isinstance(ins.get("prepend_text"), str) else ""
    app = ins.get("append_text")  if isinstance(ins.get("append_text"),  str) else ""
    return ((pre or "") + body + (app or "")).strip() or None


def resolve_number(prompt, val, depth=0, visited=None):
    """Follow a NUMERIC input through the graph to its literal value.

    steps/cfg/seed are routinely driven by a ComfySwitchNode or a primitive
    rather than typed on the sampler. extract_settings used to skip any linked
    value outright, so those graphs produced a params line with no `Steps:`
    and no `CFG scale:` -- and `Steps:` is precisely the token A1111-style
    parsers (CivitAI's included) look for to recognise the block at all. A
    file could therefore carry a perfect prompt and still read as "no
    metadata"."""
    if visited is None:
        visited = set()
    if depth > 30 or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        s = val.strip()
        try:
            return int(s) if re.fullmatch(r"[-+]?\d+", s) else float(s)
        except Exception:
            return None
    if not is_link(val):
        return None
    nid = str(val[0])
    if nid in visited:
        return None
    visited.add(nid)
    n = prompt.get(nid)
    if not n:
        return None
    ins = n.get("inputs", {})

    if "on_true" in ins and "on_false" in ins:
        sw = ins.get("switch", ins.get("boolean"))
        branch = "on_true" if resolve_bool(prompt, sw, depth) else "on_false"
        return resolve_number(prompt, ins.get(branch), depth + 1, visited)
    if "select" in ins and isinstance(ins["select"], str) and ins["select"].strip():
        key = "source_" + ins["select"].strip().lower()
        if key in ins:
            return resolve_number(prompt, ins[key], depth + 1, visited)

    # Primitive/int/float nodes expose the value under one of these.
    for k in ("value", "number", "int", "float", "INT", "FLOAT"):
        if k in ins:
            r = resolve_number(prompt, ins[k], depth + 1, visited)
            if r is not None:
                return r
    # Last resort: the single upstream numeric link (never a tensor input).
    SKIP = {"model", "clip", "vae", "conditioning", "latent", "image", "samples",
            "positive", "negative", "switch", "boolean"}
    for k, v in ins.items():
        if k in SKIP or not is_link(v):
            continue
        r = resolve_number(prompt, v, depth + 1, visited)
        if r is not None:
            return r
    return None


def resolve_string(prompt, val, depth=0, visited=None, wf=None):
    """Resolve a value (literal or [id, slot] link) to the prompt string it
    represents, following the graph. `wf` is the workflow widgets_values map,
    used as a fallback for captured text the API JSON sent empty."""
    if visited is None:
        visited = set()
    if wf is None:
        wf = {}
    if depth > 60:
        return None
    if isinstance(val, str):
        return val
    if not is_link(val):
        return None

    nid = str(val[0])
    if nid in visited:
        return None
    visited.add(nid)
    n = prompt.get(nid)
    if not n:
        return None
    ins = n.get("inputs", {})
    ct = n.get("class_type", "")

    # CoachBateBatchPrompter holds the ENTIRE list of prompts, but it also
    # records WHICH line fired for this image (starting_number). Index into the
    # list rather than refusing outright -- the blanket skip here is what left
    # whole batch-prompted folders with no prompt at all. Returns None if the
    # list or index can't be read, so the old safe behaviour still applies.
    if "Batch" in ct and "Coach" in ct:
        return batch_prompter_text(n)

    # Text capture/display nodes: CoachBate Text Preview and Edit, or
    # ShowText|pysssss (the common LTX/other equivalent). These hold the
    # resolved single line for THIS image.
    #
    # WORKFLOW WINS over the API prompt here. The API `prompt` JSON is frozen
    # at QUEUE time, so a capture node's literal in it is whatever the widget
    # held from the PREVIOUS execution -- stale by one image, or stale for the
    # entire run when a batch is queued in one go. The `workflow` JSON is
    # serialized when the image is SAVED, after the node received this image's
    # text, so its widgets_values is the line that actually produced the file.
    if "CoachBate" in ct or "ShowText" in ct:
        wtxt = wf_text(wf, nid)          # save-time value: authoritative
        if wtxt:
            return wtxt
        for k in ("any", "text", "string", "value", "prompt"):
            v = ins.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for k in ("prompt_in", "any", "text", "string", "value"):
            if is_link(ins.get(k)):
                r = resolve_string(prompt, ins[k], depth + 1, visited, wf)
                if r and r.strip():
                    return r
        return None

    # PreviewAny / display passthrough -> follow `source`.
    if "source" in ins and len(ins) <= 2:
        r = resolve_string(prompt, ins.get("source"), depth + 1, visited, wf)
        if r:
            return r

    # Boolean switch (ComfySwitchNode uses `switch`; Crystools uses `boolean`).
    if "on_true" in ins and "on_false" in ins:
        sw = ins.get("switch", ins.get("boolean"))
        branch = "on_true" if resolve_bool(prompt, sw, depth) else "on_false"
        return resolve_string(prompt, ins.get(branch), depth + 1, visited, wf)

    # Letter selector (SimpleSelectorSwitch: select="D" -> source_d).
    if "select" in ins and isinstance(ins["select"], str) and ins["select"].strip():
        key = "source_" + ins["select"].strip().lower()
        if key in ins:
            return resolve_string(prompt, ins[key], depth + 1, visited, wf)

    # LenientSwitch (pass_if_a chooses source_a vs source_b).
    if "pass_if_a" in ins and ("source_a" in ins or "source_b" in ins):
        pick = "source_a" if resolve_bool(prompt, ins.get("pass_if_a"), depth) else "source_b"
        r = resolve_string(prompt, ins.get(pick), depth + 1, visited, wf)
        if r:
            return r

    # String concatenate (string_a/string_b etc.) -> keeps LoRA trigger words.
    pair = None
    for a, b in (("string_a", "string_b"), ("text_a", "text_b"), ("string1", "string2")):
        if a in ins or b in ins:
            pair = (a, b)
            break
    if pair:
        a = resolve_string(prompt, ins.get(pair[0], ""), depth + 1, set(visited), wf) or ""
        b = resolve_string(prompt, ins.get(pair[1], ""), depth + 1, set(visited), wf) or ""
        delim = ins.get("delimiter", "")
        if not isinstance(delim, str):
            delim = ""
        joined = (a + delim + b) if (a and b) else (a or b)
        if joined.strip():
            return joined

    # String replace.
    if "string" in ins and "find" in ins and "replace" in ins:
        base = resolve_string(prompt, ins.get("string"), depth + 1, visited, wf) or ""
        find = ins.get("find", "")
        repl = ins.get("replace", "")
        if isinstance(find, str):
            base = base.replace(find, repl if isinstance(repl, str) else "")
        return base

    # Direct literal string fields (links under the same names are handled
    # below; conditioning `positive` is always a link, so grabbing a literal
    # `positive` string here only matches text nodes like "easy positive").
    for k in ("value", "text", "string", "any", "multiline_text", "prompt",
              "positive", "positive_prompt", "text_pos", "t5xxl",
              "populated_text", "wildcard_text"):
        v = ins.get(k)
        if isinstance(v, str) and v.strip():
            return v

    # Generic: follow the first text-ish input link (rgthree Any Switch, etc.).
    SKIP = {"clip", "image", "vae", "model", "conditioning", "samples", "latent",
            "select", "delimiter", "find", "replace", "switch", "pass_if_a",
            "seed", "noise_seed", "max_length", "width", "height"}
    any_keys = sorted([k for k in ins if re.match(r"any_?\d+$", k) or k == "any"],
                      key=lambda k: (len(k), k))
    for k in any_keys + [k for k in ins if k not in any_keys]:
        if k in SKIP or k.startswith(("label", "source_")):
            continue
        v = ins.get(k)
        if is_link(v):
            r = resolve_string(prompt, v, depth + 1, visited, wf)
            if r and r.strip():
                return r
    for k in sorted(k for k in ins if k.startswith("source_")):
        r = resolve_string(prompt, ins.get(k), depth + 1, visited, wf)
        if r and r.strip():
            return r
    # Last resort: a literal value persisted in the workflow JSON for this node.
    return wf_text(wf, nid)


def stale_capture_fix(prompt, wf, text):
    """Last line of defence against the queue-time-vs-save-time staleness.

    If the resolved text is EXACTLY the stale queue-time literal of a capture
    node (CoachBate Text Preview and Edit / ShowText), swap in that node's
    save-time workflow value. This catches the case where the literal reached
    the encoder directly -- including files written by an earlier version of
    this script, whose `prompt` chunk had the stale text baked onto the encoder
    node by patch_prompt_literal and so no longer links back to the capture
    node at all."""
    if not text or not text.strip():
        return text
    t = text.strip()
    for nid, n in prompt.items():
        ct = n.get("class_type", "")
        if ("CoachBate" not in ct and "ShowText" not in ct) or "Batch" in ct:
            continue
        stale = None
        for k in ("text", "any", "string", "value", "prompt"):
            v = n.get("inputs", {}).get(k)
            if isinstance(v, str) and v.strip():
                stale = v.strip()
                break
        if stale != t:
            continue
        wtxt = wf_text(wf, nid)
        if wtxt and wtxt.strip() and wtxt.strip() != t:
            return wtxt.strip()
    return text


# ── graph walks ────────────────────────────────────────────────────────────────
def bfs_back(prompt, start_ref, predicate, max_nodes=400):
    from collections import deque
    seen, q = set(), deque()
    if is_link(start_ref):
        q.append(str(start_ref[0]))
    while q and len(seen) < max_nodes:
        nid = q.popleft()
        if nid in seen:
            continue
        seen.add(nid)
        n = prompt.get(nid)
        if not n:
            continue
        if predicate(nid, n):
            return nid
        for v in n.get("inputs", {}).values():
            if is_link(v):
                q.append(str(v[0]))
    return None


def is_sampler(nid, n):
    ins = n.get("inputs", {})
    return ("sampler_name" in ins) or ("guider" in ins) or \
           ("positive" in ins and "latent_image" in ins)


def sampler_positive_ref(prompt, sampler_nid):
    """The positive-conditioning link for a sampler. For guider-based samplers
    (SamplerCustomAdvanced) the positive lives on the CFGGuider/BasicGuider."""
    sins = prompt.get(sampler_nid, {}).get("inputs", {})
    if is_link(sins.get("positive")):
        return sins["positive"]
    if is_link(sins.get("guider")):
        gins = prompt.get(str(sins["guider"][0]), {}).get("inputs", {})
        if is_link(gins.get("positive")):
            return gins["positive"]
        if is_link(gins.get("conditioning")):
            return gins["conditioning"]
    if is_link(sins.get("conditioning")):
        return sins["conditioning"]
    return None


def is_encoder(nid, n):
    ct = n.get("class_type", "")
    ins = n.get("inputs", {})
    # PromptRelay* are timeline encoders: they emit conditioning and carry the
    # prompt themselves, so they never match the "TextEncode" name test.
    if "TextEncode" in ct or "CLIPText" in ct or "PromptRelay" in ct:
        return True
    return ("conditioning" not in ins) and any(
        k in ins for k in ("text", "prompt", "global_prompt", "local_prompts"))


def trace_to_encoder(prompt, ref, depth=0, visited=None):
    """Walk the conditioning chain from `ref` to the text encoder, following the
    ACTIVE branch of any switch (so dual-encoder graphs pick the right prompt).
    Falls back to a plain upstream search if the active path can't be followed."""
    if visited is None:
        visited = set()
    if not is_link(ref) or depth > 80:
        return None
    nid = str(ref[0])
    if nid in visited:
        return None
    visited.add(nid)
    n = prompt.get(nid)
    if not n:
        return None
    if is_encoder(nid, n):
        return nid
    ins = n.get("inputs", {})
    if "on_true" in ins and "on_false" in ins:
        sw = ins.get("switch", ins.get("boolean"))
        branch = "on_true" if resolve_bool(prompt, sw, depth) else "on_false"
        return trace_to_encoder(prompt, ins.get(branch), depth + 1, visited)
    if "select" in ins and isinstance(ins["select"], str) and ins["select"].strip():
        return trace_to_encoder(prompt, ins.get("source_" + ins["select"].strip().lower()),
                                depth + 1, visited)
    for k in ("conditioning", "positive", "cond", "base", "c", "guider"):
        if is_link(ins.get(k)):
            r = trace_to_encoder(prompt, ins[k], depth + 1, visited)
            if r:
                return r
    for v in ins.values():
        if is_link(v):
            r = trace_to_encoder(prompt, v, depth + 1, visited)
            if r:
                return r
    return None


def file_base(stem):
    m = re.match(r"^(.*?)[_\- ]?(\d{2,})[_]?$", stem)
    return m.group(1).rstrip("_- ") if m else stem


def pick_save_node(prompt, fname):
    stem = os.path.splitext(os.path.basename(fname))[0]
    base = file_base(stem)
    saves = [(nid, n) for nid, n in prompt.items()
             if any(h in n.get("class_type", "") for h in SAVE_HINTS)]
    if not saves:
        return None

    def prefix_base(n):
        p = n.get("inputs", {}).get("filename_prefix", "")
        # The prefix is frequently BUILT upstream (a string concat / preview
        # node) rather than typed, so it arrives as a link. Resolving it keeps
        # multi-save graphs matchable; treating a link as "no prefix" made
        # pick_save_node give up and fall back to the first sampler in the
        # graph, which is how a multi-stage workflow gets the wrong prompt.
        if is_link(p):
            p = resolve_string(prompt, p) or ""
        return os.path.basename(p.replace("\\", "/")) if isinstance(p, str) else ""

    for nid, n in saves:                       # exact base match
        if prefix_base(n) and prefix_base(n) == base:
            return nid
    cand = [(nid, n) for nid, n in saves       # stem startswith prefix -> longest
            if prefix_base(n) and stem.startswith(prefix_base(n))]
    if cand:
        cand.sort(key=lambda x: len(prefix_base(x[1])), reverse=True)
        return cand[0][0]
    if len(saves) == 1:
        return saves[0][0]
    return None


def find_sampler_for_save(prompt, save_nid):
    ins = prompt.get(save_nid, {}).get("inputs", {})
    img_ref = ins.get("images") or ins.get("image") or ins.get("video") or ins.get("frames")
    if is_link(img_ref):
        s = bfs_back(prompt, img_ref, is_sampler)
        if s:
            return s
    for nid, nn in prompt.items():
        if is_sampler(nid, nn):
            return nid
    return None


def collect_upstream(prompt, start_nid, max_nodes=400):
    from collections import deque
    seen, order, q = set(), [], deque([start_nid])
    while q and len(seen) < max_nodes:
        nid = q.popleft()
        if nid in seen or nid not in prompt:
            continue
        seen.add(nid)
        order.append(nid)
        for v in prompt[nid].get("inputs", {}).values():
            if is_link(v):
                q.append(str(v[0]))
    return order


def extract_settings(prompt, sampler_nid, width=None, height=None, frames=None, fps=None):
    parts = []
    steps_val = None
    if sampler_nid:
        sins  = prompt.get(sampler_nid, {}).get("inputs", {})
        chain = collect_upstream(prompt, sampler_nid)

        def first(keys):
            """First literal for any of `keys` in the sampler's upstream chain,
            resolving THROUGH links (switches, primitives) rather than skipping
            them -- see resolve_number."""
            for nid in chain:
                ins = prompt.get(nid, {}).get("inputs", {})
                for k in keys:
                    if k not in ins:
                        continue
                    v = ins[k]
                    if not is_link(v):
                        return v
                    r = resolve_number(prompt, v)
                    if r is not None:
                        return r
            return None

        steps = first(("steps",))
        if steps is not None:
            parts.append("Steps: %s" % steps)
        sn = first(("sampler_name",))
        if sn:
            parts.append("Sampler: %s" % sn)
        sched = first(("scheduler",))
        if sched:
            parts.append("Schedule type: %s" % sched)
        cfg = sins.get("cfg")
        if cfg is None or is_link(cfg):
            cfg = first(("cfg", "guidance"))
        if cfg is not None and not is_link(cfg):
            parts.append("CFG scale: %s" % cfg)
        seed = first(SEED_KEYS)
        if seed is not None:
            parts.append("Seed: %s" % seed)
    if width and height:
        parts.append("Size: %dx%d" % (width, height))
    if frames and frames > 1:
        parts.append("Frames: %s" % frames)
    if fps:
        parts.append("FPS: %s" % fps)

    # A1111 parsers identify the settings block by a leading "Steps:". If the
    # graph genuinely has no steps value (guider-only / distilled samplers),
    # emit one anyway so the line is still recognised as generation data
    # instead of the whole chunk being written off as unparseable.
    if parts and not parts[0].startswith("Steps:"):
        parts.insert(0, "Steps: %s" % (steps_val if steps_val is not None else 0))
    return ", ".join(parts)


def negative_encoder_ids(prompt):
    """Node ids that are wired into ANY node's negative input, plus anything
    whose title says negative. Used to make sure a fallback never grabs the
    negative prompt."""
    neg = set()
    for n in prompt.values():
        if not isinstance(n, dict):
            continue
        for k, v in (n.get("inputs") or {}).items():
            if "negative" in k.lower() and is_link(v):
                neg.add(str(v[0]))
    for nid, n in prompt.items():
        if not isinstance(n, dict):
            continue
        title = ((n.get("_meta") or {}).get("title") or "")
        if "negative" in title.lower():
            neg.add(str(nid))
    return neg


def fallback_encoder_text(prompt, wf_widgets):
    """Graph-wide hunt for the positive prompt, for graphs the targeted trace
    can't walk: upscale/interpolate-only graphs with NO sampler, and graphs
    (AnimateDiff and friends) whose sampler positive input doesn't lead to a
    recognisable encoder. Together those were ~1400 files.

    Safety comes from being CONSERVATIVE rather than clever: negative-wired and
    negative-titled encoders are excluded, the batch prompter is never used,
    and if more than one DISTINCT candidate text survives the file is skipped
    rather than guessed at -- a wrong prompt is worse than none."""
    neg = negative_encoder_ids(prompt)
    found = []
    for nid, n in prompt.items():
        if not isinstance(n, dict) or str(nid) in neg:
            continue
        ct = n.get("class_type", "")
        if "Batch" in ct and "Coach" in ct:
            continue
        if not is_encoder(nid, n):
            continue
        ins = n.get("inputs") or {}
        for k in ENCODER_TEXT_KEYS:
            if k not in ins:
                continue
            r = resolve_string(prompt, ins[k], wf=wf_widgets)
            if r and r.strip():
                r = stale_capture_fix(prompt, wf_widgets, r).strip()
                found.append((r, str(nid), k))
                break
    if not found:
        return None, None, None
    distinct = {t for t, _n, _k in found}
    if len(distinct) > 1:
        return None, None, None
    return found[0]


def extract(prompt, fname, wf_widgets, width=None, height=None, frames=None, fps=None):
    """Return (text, settings, debug_or_reason, enc_nid, used_key). text is None
    when nothing safe to embed could be found (reason explains why); enc_nid /
    used_key are the encoder node id and input-field name the text came from
    (None if text is None), used to patch a literal back into the prompt JSON."""
    save_nid    = pick_save_node(prompt, fname)
    sampler_nid = find_sampler_for_save(prompt, save_nid) if save_nid else None
    if not sampler_nid:
        for nid, n in prompt.items():
            if is_sampler(nid, n):
                sampler_nid = nid
                break
    if not sampler_nid:
        # Upscale / interpolate / audio-only graph: no sampler to walk back
        # from, but the encoder that described the shot is usually still here.
        t, nid, key = fallback_encoder_text(prompt, wf_widgets)
        if t:
            return (t, extract_settings(prompt, None, width, height, frames, fps),
                    "no sampler; single positive encoder %s" % nid, nid, key)
        return None, None, "no sampler node in graph", None, None

    pos_ref = sampler_positive_ref(prompt, sampler_nid)
    enc = None
    if is_link(pos_ref):
        enc = trace_to_encoder(prompt, pos_ref) or bfs_back(prompt, pos_ref, is_encoder)
    if not enc:
        t, nid, key = fallback_encoder_text(prompt, wf_widgets)
        if t:
            return (t, extract_settings(prompt, sampler_nid, width, height, frames, fps),
                    "sampler %s has no encoder on positive; single positive encoder %s"
                    % (sampler_nid, nid), nid, key)
        return None, None, "no text encoder on sampler %s positive input" % sampler_nid, None, None

    # Take the first encoder text field that resolves to a non-empty string.
    eins = prompt[enc].get("inputs", {})
    text = None
    used_key = None
    for k in ENCODER_TEXT_KEYS:
        if k in eins:
            r = resolve_string(prompt, eins[k], wf=wf_widgets)
            if r and r.strip():
                text = r
                used_key = k
                break
    if not text or not text.strip():
        # The encoder on the sampler resolves to nothing (dynamic source that
        # was never captured). Another encoder in the graph may still hold the
        # literal -- same conservative rules apply.
        t, nid, key = fallback_encoder_text(prompt, wf_widgets)
        if t:
            return (t, extract_settings(prompt, sampler_nid, width, height, frames, fps),
                    "encoder %s unresolved; single positive encoder %s" % (enc, nid), nid, key)
        return None, None, ("prompt feeding encoder %s could not be resolved to "
                            "a literal (dynamic/batch source not captured)" % enc), None, None
    text = stale_capture_fix(prompt, wf_widgets, text)

    settings = extract_settings(prompt, sampler_nid, width, height, frames, fps)
    dbg = "save=%s sampler=%s enc=%s(%s)" % (save_nid, sampler_nid, enc, prompt[enc].get("class_type"))
    return text.strip(), settings, dbg, enc, used_key


def active_batch_prompters(prompt):
    """CoachBateBatchPrompter nodes that were LIVE for this image.

    ComfyUI strips bypassed and muted nodes from the API `prompt` JSON, so a
    prompter present here was active by definition. We additionally require
    its output to be wired into something, so a stray disconnected prompter
    left lying on the canvas is never reported as the source."""
    referenced = set()
    for n in prompt.values():
        if not isinstance(n, dict):
            continue
        for v in (n.get("inputs") or {}).values():
            if is_link(v):
                referenced.add(str(v[0]))
    out = []
    for nid, n in prompt.items():
        if not isinstance(n, dict):
            continue
        ct = n.get("class_type", "")
        if "Batch" in ct and "Coach" in ct and str(nid) in referenced:
            out.append((str(nid), n))
    return out


def batch_prompter_candidate_list(prompt):
    """"dynamically generated from one of these prompts: ..." listing every
    line an active batch prompter could have fired.

    Used when the exact line can't be pinned down: naming the candidate set is
    far more useful than a bare placeholder, and still honest. Joined with
    " | " deliberately -- a newline would end the prompt line and turn the rest
    into what an A1111 parser reads as settings."""
    for _nid, n in active_batch_prompters(prompt):
        ins = n.get("inputs") or {}
        text = ins.get("multiline_text")
        if not isinstance(text, str) or not text.strip():
            continue
        pre = ins.get("prepend_text") if isinstance(ins.get("prepend_text"), str) else ""
        app = ins.get("append_text")  if isinstance(ins.get("append_text"),  str) else ""
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        joined = " | ".join((pre or "") + ln + (app or "") for ln in lines)
        return "dynamically generated from one of these prompts: " + joined
    return None


def with_fallback_prompt(result, prompt=None, sampler_nid=None,
                         width=None, height=None, frames=None, fps=None):
    """Apply the two fallbacks when no real prompt could be recovered.

    CivitAI refuses a post whose file carries no prompt, so writing SOMETHING
    truthful beats writing nothing:
      1. an active batch prompter -> name every candidate line
      2. otherwise -> the configured placeholder text

    enc_nid stays None either way, so the graph is never patched with a fake
    literal: the metadata stays honest, and a later run with a better resolver
    still re-derives the real prompt instead of finding our own text."""
    text, settings, reason, enc_nid, used_key = result
    if text:
        return result
    fb = batch_prompter_candidate_list(prompt) if prompt else None
    label = "batch-prompter candidates"
    if not fb:
        fb, label = (PLACEHOLDER or None), "placeholder"
    if not fb:
        return result
    settings = extract_settings(prompt, sampler_nid, width, height, frames, fps) if prompt else ""
    return fb, settings, "%s (%s)" % (label.upper(), reason), None, None


def build_parameters(text, settings):
    result = text.strip()
    if settings:
        result += "\n" + settings
    return result


def patch_prompt_literal(prompt, enc_nid, used_key, text):
    """Return a copy of the API `prompt` graph with the encoder node's text
    field overwritten by the literal resolved prompt. CivitAI's native
    ComfyUI-metadata scanner reads this JSON directly and looks for a literal
    string on a text-encoder node; it does not follow graph links (switches,
    the CoachBate capture node, etc.), so for dynamic-prompt graphs the link
    left in place there is invisible to it even though our trace above
    resolved it correctly. Breaking that link here (source-of-truth for the
    written metadata remains the trace, not this node) lets CivitAI's scan
    find the same text without changing what gets rendered.

    The value being replaced is stashed alongside it under "_orig_<key>" so a
    later run can restore the untouched graph (see unpatch_prompt). Without
    that stash the patch is one-way: the trace on a re-run would stop at the
    literal this function wrote and could never correct an earlier mistake."""
    patched = copy.deepcopy(prompt)
    ins = patched[enc_nid]["inputs"]
    stash = "_orig_" + used_key
    if stash not in ins:
        ins[stash] = ins.get(used_key)
    ins[used_key] = text
    return patched


def parse_comfy_json(raw):
    """Parse a `prompt`/`workflow` payload -> (graph_dict, plain_text).

    The payload is NOT always a graph dict, and assuming it is was crashing the
    trace on hundreds of files. Two real shapes seen in the wild:

      * DOUBLE-ENCODED -- a JSON string containing the JSON. json.loads then
        returns a str, which blew up as
        "AttributeError: 'str' object has no attribute 'values'".
        Unwrap by decoding again.
      * PLAIN PROMPT TEXT -- some workflows (LTX timed prompts, e.g.
        "[0s: ...] [1s: ...]") store the prompt STRING in the prompt chunk
        rather than a graph. That is not an error: the prompt is already in
        hand, so hand it back as plain_text and skip the graph walk.

    Returns (None, None) when there is nothing usable."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    val = raw
    for _ in range(3):                      # unwrap double/triple encoding
        try:
            val = json.loads(val)
        except Exception:
            return None, (val.strip() if isinstance(val, str) and val.strip() else None)
        if isinstance(val, dict):
            return val, None
        if not isinstance(val, str):
            return None, None               # list/number: nothing usable
    return None, None


def unpatch_prompt(prompt):
    """Restore any encoder fields a previous run overwrote, so the trace always
    starts from the ORIGINAL graph. Keeps re-runs idempotent AND repairable: a
    prompt embedded by an earlier, buggier run is never treated as evidence."""
    if not isinstance(prompt, dict):
        return prompt
    for n in prompt.values():
        if not isinstance(n, dict):
            continue
        ins = n.get("inputs")
        if not isinstance(ins, dict):
            continue
        for stash in [k for k in ins if k.startswith("_orig_")]:
            ins[stash[len("_orig_"):]] = ins.pop(stash)
    return prompt



# ── reading the tags out of an mp4 (new; not in the script) ────────────────────

def _parse_ffmetadata(text):
    """ffmpeg `-f ffmetadata` output -> {key: value}. In the values '=', ';',
    '#', '\' and newline are backslash-escaped, so a line ending in an odd
    number of backslashes continues on the next line."""
    tags, cur = {}, None
    for line in text.split("\n"):
        if cur is None:
            if not line or line.startswith(";") or line.startswith("["):
                continue
            k, sep, v = line.partition("=")
            if not sep:
                continue
            cur = [k, v]
        else:
            cur[1] += "\n" + line
        v = cur[1]
        trailing = len(v) - len(v.rstrip("\\"))
        if trailing % 2 == 1:
            cur[1] = v[:-1]
            continue
        out, esc = [], False
        for ch in cur[1]:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                esc = True
            else:
                out.append(ch)
        tags[cur[0]] = "".join(out)
        cur = None
    return tags


def _ffmpeg_exe():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def read_comfy_tags(filepath):
    """format.tags of an mp4 as a dict: ffprobe first, then the bundled
    imageio-ffmpeg's ffmetadata dump, then mutagen with the workflow/prompt
    payloads identified by content (its keys come back unnamed)."""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = subprocess.run([probe, "-v", "quiet", "-print_format", "json",
                                  "-show_format", filepath],
                                 capture_output=True, encoding="utf-8", errors="replace")
            tags = json.loads(out.stdout).get("format", {}).get("tags", {}) or {}
            if tags:
                return tags
        except Exception:
            pass
    ff = _ffmpeg_exe()
    if ff:
        try:
            out = subprocess.run([ff, "-v", "error", "-i", filepath, "-f", "ffmetadata", "-"],
                                 capture_output=True, encoding="utf-8", errors="replace")
            tags = _parse_ffmetadata(out.stdout)
            if tags:
                return tags
        except Exception:
            pass
    try:
        from mutagen.mp4 import MP4
        tags = {}
        for k, v in (MP4(filepath).tags or {}).items():
            val = v[0] if isinstance(v, list) and v else v
            if not isinstance(val, str):
                continue
            g, _plain = parse_comfy_json(val)
            if isinstance(g, dict) and "nodes" in g:
                tags["workflow"] = val
            elif isinstance(g, dict) and any(isinstance(n, dict) and "class_type" in n
                                             for n in g.values()):
                tags["prompt"] = val
            else:
                tags[str(k)] = val
        return tags
    except Exception:
        return {}


def prompt_from_video(filepath):
    """-> (text or None, reason): the prompt the file was generated with, or why not."""
    if not filepath or not os.path.isfile(filepath):
        return None, "file not found: %s" % filepath
    tags = read_comfy_tags(filepath)
    raw_prompt, raw_workflow = tags.get("prompt"), tags.get("workflow")
    if not raw_prompt:
        if tags.get("parameters"):
            # A1111-style text (civitai script / ai-toolkit): prompt is everything
            # before the negative-prompt or settings line.
            p = re.split(r"\n(?:Negative prompt:|Steps:)", tags["parameters"], 1)[0].strip()
            if p:
                return p, "parameters tag"
        return None, "no ComfyUI prompt metadata in file"
    try:
        prompt, plain = parse_comfy_json(raw_prompt)
        prompt = unpatch_prompt(prompt) if prompt else None
        wf_graph, _ = parse_comfy_json(raw_workflow)
        wf_widgets = build_wf_widgets(wf_graph) if wf_graph else {}
    except Exception as e:
        return None, "metadata JSON parse error: %s" % e
    if prompt is None:
        if plain:
            return plain, "plain-text prompt chunk"
        return None, "prompt metadata is not a usable graph"
    text, _settings, reason, _enc, _key = extract(prompt, os.path.basename(filepath), wf_widgets)
    if text:
        return text, reason
    # H3-specific last resort. A clip saved from a face/penis REFINE pass has the
    # save node's sampler fed by a second MiniMaxH3ReferenceToVideo whose prompt
    # is empty, and with two encoders in the graph the script's single-encoder
    # fallback rightly refuses. The generation prompt is still on the other one.
    # Only encoders that are LIVE: not bypassed/muted (the API JSON normally
    # strips those, but the workflow JSON's mode is checked too) and with an
    # output actually consumed by another node, so a stray encoder left on the
    # canvas is never the source.
    referenced = set()
    for n in prompt.values():
        if isinstance(n, dict):
            for v in (n.get("inputs") or {}).values():
                if is_link(v):
                    referenced.add(str(v[0]))
    inactive = set()
    if isinstance(wf_graph, dict):
        def _walk(nodes):
            for wn in nodes or []:
                if wn.get("mode") in (2, 4):
                    inactive.add(str(wn.get("id")))
        _walk(wf_graph.get("nodes"))
        for sg in (wf_graph.get("definitions") or {}).get("subgraphs", []):
            _walk(sg.get("nodes"))
    best = None
    for nid, n in prompt.items():
        if not isinstance(n, dict) or n.get("class_type") != "MiniMaxH3ReferenceToVideo":
            continue
        if str(nid) not in referenced or str(nid).split(":")[-1] in inactive:
            continue
        r = resolve_string(prompt, (n.get("inputs") or {}).get("prompt"), wf=wf_widgets)
        if r and r.strip() and (best is None or len(r) > len(best[1])):
            best = (str(nid), r.strip())
    if best:
        return best[1], "%s; took the non-empty prompt of MiniMaxH3ReferenceToVideo %s" % (reason, best[0])
    fb = batch_prompter_candidate_list(prompt)
    if fb:
        return None, reason + " (" + fb + ")"
    return None, reason
