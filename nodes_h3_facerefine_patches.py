"""Runtime patches for ComfyUI-H3-FaceRefine (Carasibana), applied without editing it.

Three fixes found while running its pipeline on non-face subjects, kept here so
anyone with both packs gets them, and so the H3-FaceRefine clone stays pristine
(docs/h3-facerefine-upstream.patch is the same set as a diff, for an upstream PR).

1. DETECTOR PREDICTOR RESET. The pack caches each ultralytics YOLO object for the
   life of the server and reuses its predictor. Observed: after earlier runs the
   reused predictor silently returned ~2/60 detections on frames a fresh object
   scored at 0.9 -> a preview full of red "interpolated" boxes. Dropping the
   predictor on every fetch (weights stay cached) costs ~0.1 s and fixes it.

2. GRID PADDING BEFORE ENCODE (H3 Inject Video Latent). H3's video VAE maps
   17k+5 frames -> 5k+2 latents and TRUNCATES an off-grid tail (26 frames encode
   like 22). On a 264-frame clip frames 261-264 were never encoded; the latent was
   then padded to the AV latent's length with reference content, which the sampler
   turned into a different scene that the stitch pasted onto the last real frames.
   Repeating the last real frame in PIXEL space up to the grid length gives the VAE
   a genuine freeze to encode; the stitch already drops the extra frames.

3. `crop_size_from` on H3 Face Track + Crop. The crop is sized from the box HEIGHT
   only (right for faces); a subject wider than tall was cut off at the sides.
   `longest_side` sizes it from the box's longest side. Implemented by squaring the
   detector's boxes (height := max(w, h) about the centre) for that node's run,
   which is what the height-based crop then sees.

custom_nodes load alphabetically, so ComfyUI-CoachBate is imported before
ComfyUI-H3-FaceRefine; the patch is applied on the PromptServer startup hook.
"""
import logging
import sys

log = logging.getLogger("coachbate")

# Per-run context for patch 3: which detector name should have its boxes squared.
_CTX = {"square_for": None}


def _h3_module():
    """The already-loaded H3-FaceRefine nodes module, via ComfyUI's registry only."""
    try:
        import nodes as comfy_nodes
        cls = (comfy_nodes.NODE_CLASS_MAPPINGS or {}).get("H3FaceTrackCrop")
        if cls is None:
            return None
        return sys.modules.get(cls.__module__)
    except Exception:
        return None


class _SquaringModel:
    """Proxy over a YOLO object whose predict() returns boxes squared to max(w, h)."""

    def __init__(self, model):
        self._m = model

    def __getattr__(self, name):
        return getattr(self._m, name)

    def predict(self, *args, **kwargs):
        results = self._m.predict(*args, **kwargs)
        try:
            from ultralytics.engine.results import Boxes
            for res in results:
                b = res.boxes
                if b is None or len(b) == 0:
                    continue
                data = b.data.clone()
                w = data[:, 2] - data[:, 0]
                h = data[:, 3] - data[:, 1]
                side = h.clone()
                wider = w > h
                side[wider] = w[wider]
                cy = (data[:, 1] + data[:, 3]) / 2.0
                data[:, 1] = cy - side / 2.0
                data[:, 3] = cy + side / 2.0
                res.boxes = Boxes(data, res.orig_shape)
        except Exception as exc:  # never let the squaring kill detection
            log.warning("[CoachBate] H3-FaceRefine box squaring skipped: %s", exc)
        return results


def _apply_patch():
    mod = _h3_module()
    if mod is None:
        return False
    if getattr(mod, "_coachbate_h3_patch", False):
        return True

    import torch
    import nodes as comfy_nodes

    # ---- 1 + 3: wrap the shared detector loader --------------------------------
    orig_load = mod._load_detector

    def patched_load_detector(name):
        model = orig_load(name)
        try:
            model.predictor = None
        except Exception:
            pass
        if _CTX["square_for"] is not None and name == _CTX["square_for"]:
            return _SquaringModel(model)
        return model

    mod._load_detector = patched_load_detector

    # ---- 3: the option on the tracker ------------------------------------------
    Track = (comfy_nodes.NODE_CLASS_MAPPINGS or {}).get("H3FaceTrackCrop") or mod.H3FaceTrackCrop
    orig_input_types = Track.INPUT_TYPES.__func__
    orig_track_run = Track.run

    def patched_input_types(cls):
        spec = orig_input_types(cls)
        opt = spec.setdefault("optional", {})
        if "crop_size_from" not in opt:
            opt["crop_size_from"] = (["height", "longest_side"], {
                "default": "height",
                "tooltip": "What the crop is sized from. 'height' (the pack's behaviour) "
                           "suits a roughly square subject. 'longest_side' uses the larger "
                           "of the box's height and width, for subjects that can be much "
                           "wider than tall - with 'height' those are cut off at the sides "
                           "of the crop. (Added by ComfyUI-CoachBate.)"})
        if "select_override" not in opt:
            opt["select_override"] = ("STRING", {
                "default": "",
                "tooltip": "When not empty, replaces `select` (largest_face, left_most, "
                           "centre_most, right_most, ...). Lets a loop drive the subject "
                           "choice per round. (Added by ComfyUI-CoachBate.)"})
        return spec

    def patched_track_run(self, *args, crop_size_from="height", select_override="", **kwargs):
        if isinstance(select_override, str) and select_override.strip():
            kwargs["select"] = select_override.strip()
        detector = kwargs.get("detector") if "detector" in kwargs else (args[1] if len(args) > 1 else None)
        _CTX["square_for"] = detector if crop_size_from == "longest_side" else None
        try:
            return orig_track_run(self, *args, **kwargs)
        finally:
            _CTX["square_for"] = None

    Track.INPUT_TYPES = classmethod(patched_input_types)
    Track.run = patched_track_run

    # ---- 2: pixel-space grid padding on inject ----------------------------------
    # comfyui-mickmumpitz-nodes ships a verbatim copy of this node under the same id
    # and, loading later, replaces it in ComfyUI's registry - so a patch on the
    # original's class changed nothing that executed. Restore the original first.
    registered = (comfy_nodes.NODE_CLASS_MAPPINGS or {}).get("H3InjectVideoLatent")
    if registered is not None and registered is not mod.H3InjectVideoLatent:
        # Hand the node id back to the pack that owns it, so upstream fixes to
        # H3-FaceRefine's inject node are the ones that run (the copies have been
        # identical apart from CATEGORY; this keeps it that way by construction).
        comfy_nodes.NODE_CLASS_MAPPINGS["H3InjectVideoLatent"] = mod.H3InjectVideoLatent
        try:
            comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS["H3InjectVideoLatent"] =                 mod.NODE_DISPLAY_NAME_MAPPINGS.get("H3InjectVideoLatent",
                                                   comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.get("H3InjectVideoLatent"))
        except Exception:
            pass
        log.info("[CoachBate] H3InjectVideoLatent was registered by %s; restored "
                 "ComfyUI-H3-FaceRefine's own class.", getattr(registered, "__module__", "?"))
    Inject = mod.H3InjectVideoLatent
    orig_inject_run = Inject.run

    def patched_inject_run(self, av_latent, images, vae, *args, **kwargs):
        try:
            samples = av_latent.get("samples") if isinstance(av_latent, dict) else None
            video = None
            if samples is not None:
                try:
                    video = list(samples.unbind())[0]
                except Exception:
                    video = samples
            if video is not None and video.ndim >= 3:
                tmpl_t = int(video.shape[-3])
                need = (max(tmpl_t - 2, 0) // 5) * 17 + 5
                have = int(images.shape[0])
                if have < need:
                    n = need - have
                    images = torch.cat([images, images[-1:].expand(n, *images.shape[1:])], dim=0)
                    print(f"[CoachBate] H3 Inject Video Latent: padded {n} frame(s) by repeating "
                          f"the last frame to reach H3's grid ({need}) before encoding")
        except Exception as exc:
            log.warning("[CoachBate] H3 grid padding skipped: %s", exc)
        return orig_inject_run(self, av_latent, images, vae, *args, **kwargs)

    Inject.run = patched_inject_run
    Inject._coachbate_h3_inject_patch = True

    mod._coachbate_h3_patch = True
    log.info("[CoachBate] ComfyUI-H3-FaceRefine patched: detector predictor reset, "
             "grid padding before encode, crop_size_from on the tracker.")
    return True


def patch_h3_facerefine():
    """Apply now if H3-FaceRefine is already loaded, else on server startup."""
    if _apply_patch():
        return True
    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is not None and hasattr(instance, "app"):
            async def _on_startup(_app):
                if not _apply_patch():
                    log.info("[CoachBate] ComfyUI-H3-FaceRefine not found; its patches are inactive.")

            instance.app.on_startup.append(_on_startup)
            return True
    except Exception as exc:
        log.warning("[CoachBate] Could not schedule the H3-FaceRefine patch: %s", exc)
    return False
