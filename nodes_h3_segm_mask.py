"""Paste masks for ComfyUI-H3-FaceRefine from a YOLO *segmentation* model.

H3-FaceRefine's tracker (``H3 Subject Track + Crop``) only reads the bounding boxes a
YOLO model returns, so a segm model such as ``segm/CockAndBallYolo8x.pt`` works as its
``detector`` unchanged -- but the mask it also produces is thrown away and the stitch
pastes a feathered rectangle. This node runs the segm model a second time on the
stabilised crops and hands the real silhouettes to ``H3 Subject Stitch Back`` -> ``masks``,
the same slot its own ``H3 Face Mask (SAM)`` fills. That is the FaceDetailer
bbox+segm path rather than bbox+SAM: no SAM model resident next to H3, and the mask
comes from a detector trained on the thing being pasted.

Mirrors ``H3FaceMaskSAM``: masks are computed on the INPUT crops (never the decoded
result), frames the model misses fall back to the tracked rect so nothing is left
empty, and the stack is temporally smoothed so the mask edge does not flicker.
"""

import os

import cv2
import numpy as np
import torch
import torch.nn.functional as F

import folder_paths

_MODEL_CACHE = {}

# Impact subpack registers these; "ultralytics" lists both subfolders with a
# bbox\ / segm\ prefix, which is the naming H3-FaceRefine's own dropdown uses.
_FOLDER_KEYS = ("ultralytics_segm", "ultralytics", "ultralytics_bbox")


def _segm_model_list():
    names = []
    for key in _FOLDER_KEYS[:2]:
        try:
            names.extend(folder_paths.get_filename_list(key))
        except Exception:
            pass
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out or ["person_yolov8m-seg.pt"]


def _load_segm_model(name):
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    path = None
    for key in _FOLDER_KEYS:
        try:
            path = folder_paths.get_full_path(key, name)
        except Exception:
            path = None
        if path:
            break
    if path is None:
        base = getattr(folder_paths, "models_dir", "models")
        for sub in ("ultralytics/segm", "ultralytics", "ultralytics/bbox"):
            cand = os.path.join(base, *sub.split("/"), name)
            if os.path.exists(cand):
                path = cand
                break
    if path is None:
        raise FileNotFoundError(
            f"Segmentation model '{name}' not found in the ultralytics model folders."
        )
    from ultralytics import YOLO

    model = YOLO(path)
    _MODEL_CACHE[name] = model
    return model


def _box_iou(a, b):
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / max(ua, 1e-6)


def _detect_multiscale(yolo, bgr, confidence, scales, ch, cw):
    """Run the segm model on the crop at several scales and keep the best-scoring one.

    A YOLO segm model trained on medium/full shots scores a subject that fills half the
    image near zero, and the tracker's crops are exactly that: on a locker-room clip the
    organ filled 40% of the crop and scored 0.02-0.06; the same crops shrunk to half
    size inside a padded canvas scored 0.73-0.80. Masks and boxes come back mapped to
    the crop's own coordinates. Returns (masks [N,ch,cw] or None, boxes list).
    """
    best = None  # (max_conf, data, boxes)
    for s in scales:
        try:
            if abs(s - 1.0) < 1e-6:
                img, x0, y0, sw, sh = bgr, 0, 0, cw, ch
            else:
                sw, sh = max(32, int(round(cw * s))), max(32, int(round(ch * s)))
                small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_AREA)
                img = np.full((ch, cw, 3), 114, np.uint8)
                x0, y0 = (cw - sw) // 2, (ch - sh) // 2
                img[y0:y0 + sh, x0:x0 + sw] = small
            res = yolo.predict(img, conf=confidence, verbose=False, retina_masks=True)[0]
        except Exception as exc:
            print(f"[CoachBate] segm predict failed at scale {s}: {exc}")
            continue
        md = getattr(res, "masks", None)
        if md is None or md.data is None or len(md.data) == 0:
            continue
        conf = float(res.boxes.conf.max()) if res.boxes is not None and len(res.boxes) else 0.0
        if best is not None and conf <= best[0]:
            continue
        data = md.data.detach().float().cpu()  # [N, h, w] at the padded canvas's size
        if data.shape[-2:] != (ch, cw):
            data = F.interpolate(data.unsqueeze(1), size=(ch, cw),
                                 mode="bilinear", align_corners=False).squeeze(1)
        boxes = res.boxes.xyxy.tolist() if res.boxes is not None else []
        if abs(s - 1.0) >= 1e-6:
            # map the padded-canvas mask/boxes back onto the full-size crop
            data = data[:, y0:y0 + sh, x0:x0 + sw]
            data = F.interpolate(data.unsqueeze(1), size=(ch, cw),
                                 mode="bilinear", align_corners=False).squeeze(1)
            boxes = [[(b[0] - x0) / s, (b[1] - y0) / s, (b[2] - x0) / s, (b[3] - y0) / s]
                     for b in boxes]
        best = (conf, data, boxes)
    if best is None:
        return None, []
    return best[1], best[2]


def _parse_scales(text):
    out = []
    for tok in str(text or "").replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            continue
        if 0.1 <= v <= 1.0:
            out.append(v)
    return out or [1.0, 0.7, 0.5, 0.35, 0.25]


class CoachBateH3SegmMask:
    """Per-frame YOLO-segm paste masks on H3-FaceRefine's stabilised crops."""

    CATEGORY = "CoachBate/H3"
    FUNCTION = "run"
    RETURN_TYPES = ("MASK", "STRING")
    RETURN_NAMES = ("masks", "report")
    DESCRIPTION = (
        "Runs a YOLO segmentation model on the crops from H3 Subject Track + Crop and returns "
        "temporally smoothed masks for H3 Subject Stitch Back -> masks. Use with a segm "
        "detector (e.g. segm/CockAndBallYolo8x.pt) so the paste follows the real silhouette "
        "instead of a rectangle."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "crops": ("IMAGE", {
                    "tooltip": "The 'crops' output of H3 Subject Track + Crop - the INPUT crops, "
                               "not the decoded result. The mask must describe where the "
                               "subject is in the footage being replaced."}),
                "transform": ("H3FACEXFORM", {
                    "tooltip": "From H3 Subject Track + Crop. Supplies the tracked rect per "
                               "frame, used to pick the right instance and as the fallback "
                               "mask on frames the model misses."}),
                "model": (_segm_model_list(), {
                    "tooltip": "A YOLO *segmentation* model from models/ultralytics/segm. "
                               "A bbox-only model produces no masks and every frame falls "
                               "back to the rect."}),
                "confidence": ("FLOAT", {"default": 0.30, "min": 0.05, "max": 0.95, "step": 0.05}),
                "instance": (["best_overlap", "union", "largest"], {
                    "default": "best_overlap",
                    "tooltip": "Which detection to keep when the model finds several in a "
                               "crop. best_overlap: the one whose box overlaps the tracked "
                               "rect most (the subject the tracker is following). union: "
                               "all of them. largest: biggest mask area."}),
                "dilation": ("INT", {"default": 0, "min": 0, "max": 128, "step": 2,
                    "tooltip": "Grow the mask here, in canvas px. H3 Subject Stitch Back has its "
                               "own mask_dilation that does the same thing, so leave this at 0 "
                               "and use that one unless you need growth before temporal "
                               "smoothing."}),
                "temporal_smooth": ("INT", {"default": 5, "min": 1, "max": 31, "step": 2,
                    "tooltip": "Frames of averaging across the mask stack. 1 disables it "
                               "and the mask edge will shimmer."}),
                "mask_source": (["input", "output", "union"], {
                    "default": "union",
                    "tooltip": "Which crops the silhouette is traced from. input: the tracker's "
                               "crops (FaceDetailer's rule - right when the subject keeps its "
                               "shape). output: the refined crops H3 returned - the NEW "
                               "silhouette, for a subject the model reshapes or enlarges; with "
                               "input masking everything it added outside the old outline is "
                               "cut off. union: both, so neither the old nor the new edge is "
                               "left behind. output/union need refined_crops connected and fall "
                               "back to input if it is not."}),
            },
            "optional": {
                "detect_scales": ("STRING", {
                    "default": "1.0, 0.7, 0.5, 0.35, 0.25",
                    "tooltip": "Scales the crop is detected at, best score wins. A segm model "
                               "trained on medium shots scores a subject filling half the crop "
                               "near zero; the same crop at half size scores 0.7-0.8. Comma "
                               "separated, 0.1-1.0."}),
                "fallback_shape": (["none", "ellipse", "rect"], {
                    "default": "none",
                    "tooltip": "What to paste on frames the model finds no silhouette on (input "
                               "or refined crop). none: nothing - those frames keep their original "
                               "pixels, which is right when the subject is not actually there yet. "
                               "ellipse / rect: the tracked box, for detectors that miss frames "
                               "the subject IS in."}),
                "refined_crops": ("IMAGE", {
                    "tooltip": "The decoded result of the H3 pass (VAEDecode output), same "
                               "frame count as crops. Used by mask_source output / union."}),
            },
        }

    def run(self, crops, transform, model, confidence, instance, dilation, temporal_smooth,
            mask_source="union", refined_crops=None, detect_scales="1.0, 0.7, 0.5, 0.35, 0.25",
            fallback_shape="none"):
        import comfy.model_management as mm
        import comfy.utils as cu

        self._scales = _parse_scales(detect_scales)
        self._fallback_shape = fallback_shape

        rects = transform.get("face_rect") or []
        B, ch, cw, _ = crops.shape
        yolo = _load_segm_model(model)
        # ultralytics reuses its predictor for the life of the object; a stale one was
        # observed returning almost nothing in a long-running server. Rebuild per run.
        try:
            yolo.predictor = None
        except Exception:
            pass

        stacks = [("input", crops)]
        note = ""
        if mask_source in ("output", "union"):
            # the decoded clip can be longer than the crops (H3 rounds the length up to
            # its 17k+5 grid); only the first B frames correspond to the crops
            if refined_crops is not None and refined_crops.shape[0] > B:
                refined_crops = refined_crops[:B]
            if refined_crops is not None and refined_crops.shape[0] == B \
                    and tuple(refined_crops.shape[1:3]) == (ch, cw):
                stacks = [("output", refined_crops)] if mask_source == "output" \
                    else [("input", crops), ("output", refined_crops)]
            else:
                note = ("\n!! mask_source=%s but refined_crops is %s - masking the input crops "
                        "instead" % (mask_source, "not connected" if refined_crops is None
                                     else "a different size/count"))
        per_source = {}
        for label, stack in stacks:
            per_source[label] = self._segment(stack, rects, yolo, confidence, instance, mm, cu, label)
        masks = torch.stack([m for m, _, _ in per_source.values()]).max(dim=0).values
        ok = min(o for _, o, _ in per_source.values())
        multi = max(mu for _, _, mu in per_source.values())
        return self._finish(masks, rects, model, ok, B, multi, instance, dilation, temporal_smooth,
                            mask_source, per_source, note)

    def _segment(self, crops, rects, yolo, confidence, instance, mm, cu, label):
        B, ch, cw, _ = crops.shape
        masks = torch.zeros((B, ch, cw), dtype=torch.float32)
        ok = 0
        multi = 0
        pbar = cu.ProgressBar(B)
        for i in range(B):
            mm.throw_exception_if_processing_interrupted()
            pbar.update(1)
            if i % 25 == 0:
                print(f"[CoachBate] segm mask ({label}) {i}/{B}")

            fr = rects[i] if i < len(rects) else (cw * 0.25, ch * 0.25, cw * 0.5, ch * 0.5)
            rect = (float(fr[0]), float(fr[1]), float(fr[0] + fr[2]), float(fr[1] + fr[3]))

            rgb = (crops[i, ..., :3].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
            bgr = np.ascontiguousarray(rgb[..., ::-1])
            data, boxes = _detect_multiscale(yolo, bgr, confidence, self._scales, ch, cw)
            if data is None:
                continue

            n = data.shape[0]
            if n > 1:
                multi += 1
            if instance == "union" or n == 1:
                m = data.max(dim=0).values
            elif instance == "largest":
                m = data[int(data.flatten(1).sum(1).argmax())]
            else:
                if len(boxes) == n:
                    j = max(range(n), key=lambda k: _box_iou(boxes[k], rect))
                else:
                    j = int(data.flatten(1).sum(1).argmax())
                m = data[j]
            masks[i] = (m > 0.5).float()
            ok += 1
        return masks, ok, multi

    def _finish(self, masks, rects, model, ok, B, multi, instance, dilation, temporal_smooth,
                mask_source, per_source, note):
        ch, cw = masks.shape[1:]
        # frames the model missed fall back to the tracked rect so they are never empty
        fell_back = 0
        for i in range(B):
            if masks[i].max() <= 0:
                if getattr(self, "_fallback_shape", "none") == "none":
                    fell_back += 1
                    continue
                fr = rects[i] if i < len(rects) else (cw * 0.25, ch * 0.25, cw * 0.5, ch * 0.5)
                x0, y0 = max(0, int(fr[0])), max(0, int(fr[1]))
                x1, y1 = min(cw, int(fr[0] + fr[2])), min(ch, int(fr[1] + fr[3]))
                if x1 > x0 and y1 > y0:
                    if getattr(self, "_fallback_shape", "ellipse") == "ellipse":
                        yy, xx = torch.meshgrid(torch.arange(ch, dtype=torch.float32),
                                                torch.arange(cw, dtype=torch.float32), indexing="ij")
                        cx_, cy_ = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                        rx, ry = max((x1 - x0) / 2.0, 1.0), max((y1 - y0) / 2.0, 1.0)
                        masks[i] = (((xx - cx_) / rx) ** 2 + ((yy - cy_) / ry) ** 2 <= 1.0).float()
                    else:
                        masks[i, y0:y1, x0:x1] = 1.0
                    fell_back += 1

        if dilation > 0:
            k = 2 * int(dilation) + 1
            masks = F.max_pool2d(masks.unsqueeze(1), k, stride=1, padding=k // 2).squeeze(1)

        if temporal_smooth > 1 and B > 2:
            w = min(int(temporal_smooth) | 1, B if B % 2 else B - 1)
            if w >= 3:
                pad = w // 2
                t = masks.permute(1, 2, 0).reshape(-1, 1, B).contiguous()
                t = F.pad(t, (pad, pad), mode="replicate")
                kern = torch.ones(1, 1, w, dtype=t.dtype) / w
                masks = F.conv1d(t, kern).reshape(ch, cw, B).permute(2, 0, 1).contiguous()

        src = ", ".join(f"{k}: {o}/{B}" for k, (_, o, _) in per_source.items())
        report = (f"segm masks ({model}) from {mask_source} [{src}], "
                  f"{fell_back} frame(s) had no silhouette "
                  f"({'left unpasted' if getattr(self, '_fallback_shape', 'none') == 'none' else 'fell back to the tracked box'})\n"
                  f"instance={instance} ({multi} frames had more than one detection)  "
                  f"dilation={dilation}  temporal_smooth={temporal_smooth}\n"
                  f"mean coverage {float(masks.mean()) * 100:.1f}% of canvas" + note)
        if ok == 0:
            report += ("\n!! no masks at all - is this a segmentation model? A bbox model "
                       "returns boxes only, and every frame is then the rect.")
        print("[CoachBate] " + report)
        return (masks, report)


class CoachBateH3SubjectCount:
    """How many subjects a clip shows, for looping a per-subject refine pass."""

    CATEGORY = "CoachBate/H3"
    FUNCTION = "count"
    RETURN_TYPES = ("INT", "STRING")
    RETURN_NAMES = ("subjects", "report")
    DESCRIPTION = (
        "Runs a YOLO detector over sampled frames and returns the number of subjects the "
        "clip shows - the largest per-frame count that holds on at least min_fraction of the "
        "sampled frames, capped by max_subjects. Wire to Pixaroma Loop Start 'total' so a "
        "per-subject pass (select_index = loop index) runs once for each."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "detector": (_segm_model_list(), {
                    "tooltip": "The same detector the tracker uses."}),
                "confidence": ("FLOAT", {"default": 0.30, "min": 0.05, "max": 0.95, "step": 0.05}),
                "sample_every": ("INT", {"default": 1, "min": 1, "max": 60,
                    "tooltip": "Detect on every nth frame. 1 = every frame."}),
                "min_fraction": ("FLOAT", {"default": 0.05, "min": 0.01, "max": 1.0, "step": 0.01,
                    "tooltip": "A count must appear on at least this fraction of the sampled "
                               "frames to be believed, so a single-frame false positive does "
                               "not add a whole extra render pass."}),
                "max_subjects": ("INT", {"default": 20, "min": 1, "max": 50,
                    "tooltip": "Cap. Each subject is a full H3 render of the clip. 1 forces a "
                               "single pass."}),
            },
        }

    def count(self, images, detector, confidence, sample_every, min_fraction, max_subjects):
        import comfy.model_management as mm

        yolo = _load_segm_model(detector)
        try:
            yolo.predictor = None
        except Exception:
            pass
        B = images.shape[0]
        idx = list(range(0, B, max(1, int(sample_every)))) or [0]
        counts = []
        for i in idx:
            mm.throw_exception_if_processing_interrupted()
            rgb = (images[i, ..., :3].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
            bgr = np.ascontiguousarray(rgb[..., ::-1])
            try:
                res = yolo.predict(bgr, conf=confidence, verbose=False)[0]
                counts.append(int(len(res.boxes)) if res.boxes is not None else 0)
            except Exception as exc:
                print(f"[CoachBate] subject count: predict failed on frame {i}: {exc}")
                counts.append(0)
        n_samp = len(counts)
        hist = {}
        for c in counts:
            hist[c] = hist.get(c, 0) + 1
        # largest count seen on >= min_fraction of the samples (a frame showing k
        # subjects also shows k-1, so accumulate from the top down)
        subjects, acc = 0, 0
        for c in sorted(hist, reverse=True):
            acc += hist[c]
            if c > 0 and acc / n_samp >= min_fraction:
                subjects = c
                break
        subjects = max(1, min(int(max_subjects), subjects))
        report = (f"subjects={subjects} (cap {max_subjects}) from {n_samp} sampled frames of {B}, "
                  f"detector {detector} @ {confidence:.2f}\n"
                  f"per-frame counts: " + ", ".join(f"{c}x{hist[c]}" for c in sorted(hist)) +
                  f"\nmax in any one frame {max(counts) if counts else 0}; "
                  f"frames with none {hist.get(0, 0)}")
        print("[CoachBate] " + report)
        return (subjects, report)


class CoachBateH3LoopSubject:
    """Which subject a loop round should follow: by position when 2-3, by size otherwise."""

    CATEGORY = "CoachBate/H3"
    FUNCTION = "pick"
    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("select_override", "select_index")
    DESCRIPTION = (
        "For a Pixaroma loop over subjects: maps the round index to the tracker's subject "
        "choice. left_to_right with 2 subjects -> left_most / right_most, with 3 -> "
        "left_most / centre_most / right_most (bunched, similar subjects are told apart by "
        "position far more reliably than by size rank); otherwise size rank + index."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "index": ("INT", {"default": 0, "min": 0, "max": 99}),
                "total": ("INT", {"default": 1, "min": 1, "max": 99}),
                "order": (["left_to_right", "size"], {"default": "left_to_right"}),
            },
        }

    def pick(self, index, total, order):
        index, total = int(index), int(total)
        if order == "left_to_right" and 2 <= total <= 3:
            names = ["left_most", "right_most"] if total == 2 else ["left_most", "centre_most", "right_most"]
            sel = names[min(index, len(names) - 1)]
            print(f"[CoachBate] loop subject {index + 1}/{total}: {sel}")
            return (sel, 0)
        print(f"[CoachBate] loop subject {index + 1}/{total}: size rank index {index}")
        return ("", index)
