"""Shot-ID continuation and exact VHS output receipts; no filename scanning."""
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import uuid


logger = logging.getLogger(__name__)


def require_ready(shot):
    if shot.get("manual_input_required") or "MARC_INPUT_REQUIRED" in shot.get("video_prompt", ""):
        raise ValueError(f"{shot.get('shot_id')}: MARC_INPUT_REQUIRED; render is blocked.")


def continuation_settings(shot):
    flag = shot.get("video_continuation", False)
    if type(flag) is not bool:
        raise ValueError("video_continuation must be a JSON boolean, not text or a number")
    source = shot.get("continuation_source_shot_id", "")
    frames = shot.get("continuation_context_frames", 39)
    if not isinstance(source, str):
        raise ValueError("continuation_source_shot_id must be a string")
    if flag:
        if not source.strip() or source == str(shot.get("shot_id", "")):
            raise ValueError("Continuation requires a different, explicit source shot ID")
        if type(frames) is not int or frames < 39 or (frames - 39) % 51:
            raise ValueError("AV continuation frames must be 39, 90, 141, ...")
    return flag, source if flag else "", frames if flag else 0


def fingerprint(shot):
    # Review/order changes do not invalidate a perfectly good source performance.
    editorial = {"status", "batch_enabled", "batch_note", "episode_order", "title",
                 "video_filename_prefix", "review_revision", "revision_source_run"}
    generation = {k: v for k, v in shot.items() if k not in editorial}
    return hashlib.sha256(json.dumps(generation, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def receipt_path(directory, shot_id):
    key = hashlib.sha256(str(shot_id).encode()).hexdigest()
    return Path(directory).resolve() / ".continuity" / (key + ".json")


def read_source(payload, directory):
    flag, source, frames = continuation_settings(payload["shot"])
    if not flag:
        return "", 0
    saved = receipt_path(directory, source)
    if not saved.is_file():
        raise FileNotFoundError(f"Render source shot {source} in this run first; no output receipt exists.")
    receipt = json.loads(saved.read_text(encoding="utf-8"))
    rows = json.loads(Path(payload["shotlist_path"]).read_text(encoding="utf-8-sig"))
    rows = rows if isinstance(rows, list) else rows["shots"]
    matches = [s for s in rows if str(s.get("shot_id")) == source]
    if len(matches) != 1:
        raise ValueError(f"Continuation source {source} must occur exactly once in the shotlist.")
    if str(receipt.get("shot_id")) != source:
        raise ValueError(f"Continuation receipt does not belong to source shot {source}.")
    output = Path(receipt["filename"]).resolve()
    if output.parent != Path(directory).resolve() or not output.is_file():
        raise FileNotFoundError(f"Recorded continuation output is missing or outside this run: {output}")
    stat = output.stat()
    if stat.st_size != receipt["size"] or stat.st_mtime_ns != receipt["mtime_ns"]:
        raise ValueError(f"Recorded output was modified: {output}")
    if receipt["shot_fingerprint"] != fingerprint(matches[0]):
        logger.warning(
            "CoachBate: source shot %s was edited after its render loaded. "
            "Continuing from its recorded output %s; source edits apply when that shot is rerendered.",
            source, output,
        )
    return str(output), frames


def canonical_audio_tail(mask, audio, sample_rate, total_frames, context):
    """Validate full-clip AV drift before cropping the conditioning-only tail."""
    if not 0 < context <= total_frames:
        raise ValueError("Continuation source has fewer frames than the requested context")
    # Reuse H3's existing 0.5% conformance limit. Cropping first magnifies a
    # small full-clip encoder/grid discrepancy into a large tail discrepancy.
    canonicalize = importlib.import_module(type(mask).__module__)._canonical_audio
    full = canonicalize(audio, sample_rate, total_frames)
    count = round(context / 24 * sample_rate)
    return {**full, "waveform": full["waveform"][..., -count:]}


def fit_conditioning_grid(mask, latent, audio_vae, audio, shot, context):
    """Pad only the model-grid tail beyond a complete editorial soundtrack."""
    import torch
    timing = importlib.import_module(type(mask).__module__)
    video, audio_latent = timing._streams_from_latent(latent)
    target_frames = timing._pixel_frames(int(video.shape[2]))
    duration = float(shot["duration_seconds"])
    editorial_duration = float(shot.get("edit_duration_seconds", duration))
    if not 0 < editorial_duration <= duration:
        raise ValueError("Editorial duration must be positive and no longer than the generated shot")
    requested_frames = context + math.ceil(duration * 24)
    if not 0 <= target_frames - requested_frames < 17:
        raise ValueError("Conditioning grid must cover the requested shot with less than 17 grid padding frames")
    sr = int(audio["sample_rate"])
    if sr <= 0:
        raise ValueError("Conditioning audio sample rate must be positive")
    waveform = audio["waveform"]
    required = round(context / 24 * sr) + round(editorial_duration * sr)
    if waveform.shape[-1] < required:
        raise ValueError("Conditioning audio is shorter than the complete editorial shot; refusing to pad missing dialogue")
    vae_sr, _hop, grid_samples = timing.audio_grid_geometry(audio_vae, int(audio_latent.shape[-1]))
    target_samples = max(math.ceil(target_frames / 24 * sr), math.ceil(grid_samples * sr / vae_sr))
    missing = target_samples - waveform.shape[-1]
    if missing <= 0:
        return audio
    # The recorded master is untouched. This temporary tail is discarded by
    # CoachBateTrimShotOutput along with the video grid's extra picture frames.
    return {**audio, "waveform": torch.nn.functional.pad(waveform, (0, missing))}


class CoachBateRecordShotOutput:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"filenames": ("VHS_FILENAMES",), "shot_payload": ("STRING",),
                             "output_directory": ("STRING", {"default": ""})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("exact_video_filename",)
    FUNCTION = "record"
    CATEGORY = "CoachBate"
    OUTPUT_NODE = True

    def record(self, filenames, shot_payload, output_directory):
        if not filenames[0]:
            raise ValueError("Video Combine must use save_output=true")
        # VHS puts its final audio-muxed file last, after PNG/intermediate video.
        candidates = [Path(p).resolve() for p in filenames[1] if str(p).lower().endswith(".mp4")]
        if not candidates:
            raise ValueError("Video Combine returned no MP4 output")
        output = candidates[-1]
        directory = Path(output_directory).resolve()
        if output.parent != directory or not output.is_file():
            raise ValueError(f"VHS output is not in the selected run folder: {output}")
        payload = json.loads(shot_payload)
        shot = payload["shot"]
        require_ready(shot)
        stat = output.stat()
        receipt = dict(shot_id=shot["shot_id"], shot_fingerprint=fingerprint(shot),
                       filename=str(output), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                       all_output_files=list(filenames[1]))
        target = receipt_path(directory, shot["shot_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return {"ui": {"text": [str(output)]}, "result": (str(output),)}


class CoachBatePrepareShotContinuity:
    """Delegate latent masking to the installed H3 context nodes, not a new engine."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",), "vae": ("VAE",), "audio_vae": ("VAE",),
                             "shot_payload": ("STRING",), "output_directory": ("STRING", {"default": ""}),
                             "no_master_audio": ("BOOLEAN", {"default": True})},
                "optional": {"conditioning_audio": ("AUDIO",)}}
    RETURN_TYPES = ("LATENT", "AUDIO", "INT")
    RETURN_NAMES = ("latent", "conditioning_audio", "trim_frames")
    FUNCTION = "prepare"
    CATEGORY = "CoachBate"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def prepare(self, latent, vae, audio_vae, shot_payload, output_directory,
                no_master_audio=True, conditioning_audio=None):
        import nodes
        import torch
        payload = json.loads(shot_payload)
        require_ready(payload["shot"])
        path, context = read_source(payload, output_directory)
        source_frames = source_audio = None
        if path:
            import av
            with av.open(path) as container:
                stream = container.streams.video[0]
                if stream.average_rate != 24 or not stream.frames:
                    raise ValueError("Continuation receipts require a counted 24 fps render")
                total_frames = stream.frames
                if total_frames < context:
                    raise ValueError("Continuation source is shorter than the requested context")
                skip = total_frames - context
            # Use the installed VHS decoder so frame/audio timelines agree.
            loader = nodes.NODE_CLASS_MAPPINGS["VHS_LoadVideoPath"]()
            result = loader.load_video(video=path, force_rate=24, custom_width=0,
                                       custom_height=0, frame_load_cap=context, skip_first_frames=skip,
                                       select_every_nth=1)
            values = result["result"] if isinstance(result, dict) else result
            source_frames, source_audio = values[0], values[2]
            if len(source_frames) != context:
                raise ValueError("Continuation decoder did not return the exact requested frame count")
        if not no_master_audio:
            if conditioning_audio is None:
                raise ValueError("Recorded shot needs its conditioning audio")
            if context:
                prefix = torch.zeros((*conditioning_audio["waveform"].shape[:-1],
                                      round(context / 24 * conditioning_audio["sample_rate"])),
                                     dtype=conditioning_audio["waveform"].dtype,
                                     device=conditioning_audio["waveform"].device)
                conditioning_audio = {**conditioning_audio, "waveform": torch.cat(
                    (prefix, conditioning_audio["waveform"]), dim=-1)}
            mask = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3SongMaskedAVContext"]()
            conditioning_audio = fit_conditioning_grid(
                mask, latent, audio_vae, conditioning_audio, payload["shot"], context)
            result = mask.prepare(latent, audio_vae, conditioning_audio, context_length=context,
                                  source_fps=24, crop="center", vae=vae, source_frames=source_frames)
            return result[0], conditioning_audio, result[1]
        if not context:
            return latent, conditioning_audio, 0
        mask = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3ExistingVideoMaskedContext"]()
        # Decode the full audio only; keep video decoding limited to the tail.
        audio_loader = nodes.NODE_CLASS_MAPPINGS["VHS_LoadAudio"]()
        full_audio = audio_loader.load_audio(audio_file=path, seek_seconds=0, duration=0)[0]
        source_audio = canonical_audio_tail(mask, full_audio,
            int(getattr(audio_vae, "audio_sample_rate", 32000)), total_frames, context)
        prepared, trim, _insert_frame, _preserved_frames = mask.prepare(
            latent, vae, audio_vae, source_frames, source_audio, 24, context, "center", 0)
        return prepared, conditioning_audio, trim


def compact_keyframe_masks(latent):
    """Adapt Concat AV's channel-expanded masks without changing their values."""
    import torch
    mask = latent.get("noise_mask")
    if mask is None:
        return latent
    if isinstance(mask, torch.Tensor):
        raise ValueError("Hard H3 anchors require a two-stream AV noise mask")
    parts = list(mask.unbind()) if hasattr(mask, "unbind") else list(mask)
    if len(parts) != 2:
        raise ValueError("Hard H3 anchors require both video and audio noise masks")
    compact = []
    for stream, part, ndim in zip(("video", "audio"), parts, (5, 4)):
        if part.ndim != ndim or part.shape[1] < 1:
            raise ValueError(f"Unexpected {stream} mask shape: {tuple(part.shape)}")
        single = part[:, :1]
        if not torch.equal(part, single.expand_as(part)):
            raise ValueError(f"Cannot compact channel-dependent {stream} noise mask")
        compact.append(single)
    if isinstance(mask, (tuple, list)):
        packed = type(mask)(compact)
    else:
        packed = type(mask)(tuple(compact))
    return {**latent, "noise_mask": packed}


class CoachBateShotKeyframes:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"conditioning": ("CONDITIONING",), "latent": ("LATENT",),
                             "vae": ("VAE",), "shot_payload": ("STRING",)}}
    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("conditioning", "latent")
    FUNCTION = "apply"
    CATEGORY = "CoachBate"

    def apply(self, conditioning, latent, vae, shot_payload):
        import nodes
        import numpy as np
        import torch
        from PIL import Image
        shot = json.loads(shot_payload)["shot"]
        hard = shot.get("hard_frame_anchors", False)
        if type(hard) is not bool:
            raise ValueError("hard_frame_anchors must be a JSON boolean")
        _, _, context = continuation_settings(shot)
        positions, images = [], {}
        for key, position in (("start_image", 0), ("end_image", context +
                math.ceil(float(shot.get("edit_duration_seconds", shot["duration_seconds"])) * 24) - 1)):
            if shot.get(key) and float(shot.get(key + "_strength", 1)) > 0:
                if context and key == "start_image":
                    raise ValueError("Use a source video OR a first-frame still, not both")
                with Image.open(shot[key]) as image:
                    images[f"keyframe_image_{len(positions)+1}"] = torch.from_numpy(
                        np.array(image.convert("RGB"), dtype=np.float32) / 255).unsqueeze(0)
                positions.append(position)
        if not positions:
            return conditioning, latent
        state = json.dumps({"count": len(positions), "positions": positions})
        if hard:
            node = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3CustomKeyframesMasked"]()
            latent = compact_keyframe_masks(latent)
            masked = node.apply(latent, vae, state, "0-based", "center", **images)
            return conditioning, masked[0]
        node = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3CustomKeyframes"]()
        guided = node.apply(conditioning, vae, latent, state, "0-based", "center", **images)
        return guided[0], latent


class CoachBateTrimShotOutput:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "audio": ("AUDIO",),
                             "shot_payload": ("STRING",), "trim_frames": ("INT", {"default": 0}),
                             "no_master_audio": ("BOOLEAN", {"default": True})}}
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "trim"
    CATEGORY = "CoachBate"

    def trim(self, images, audio, shot_payload, trim_frames=0, no_master_audio=True):
        shot = json.loads(shot_payload)["shot"]
        count = math.ceil(float(shot.get("edit_duration_seconds", shot["duration_seconds"])) * 24)
        if len(images) < trim_frames + count:
            raise ValueError("Generated video is too short for context trim and complete editorial audio")
        images = images[trim_frames:trim_frames + count]
        start = round(trim_frames / 24 * audio["sample_rate"]) if no_master_audio else 0
        end = start + round(count / 24 * audio["sample_rate"])
        if audio["waveform"].shape[-1] < end:
            raise ValueError("Final audio is too short; refusing a silent/truncated replacement")
        return images, {**audio, "waveform": audio["waveform"][..., start:end]}
