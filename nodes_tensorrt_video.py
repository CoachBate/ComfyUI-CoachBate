import gc
import logging

import comfy.model_management as mm
import folder_paths
import torch


log = logging.getLogger("coachbate")


def _node_class(class_id):
    import nodes as comfy_nodes

    node_class = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_id)
    if node_class is None:
        raise RuntimeError(
            f"Required node '{class_id}' is unavailable. Restart ComfyUI after installing "
            "the TensorRT nodes and ComfyUI-VideoHelperSuite."
        )
    return node_class


def _required_input(class_id, name, fallback):
    try:
        return _node_class(class_id).INPUT_TYPES()["required"][name]
    except Exception:
        return fallback


def _parse_multipliers(value, pair_count, fallback):
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("multiplier_list must contain comma-separated integers") from exc
    values.extend([fallback] * (pair_count - len(values)))
    return [max(1, item) for item in values[:pair_count]]


def _native_interpolation_model_input():
    models = folder_paths.get_filename_list("frame_interpolation")
    film_models = [name for name in models if "film" in name.lower()]
    preferred = "film_net_fp16.safetensors"
    film_models.sort(key=lambda name: (name != preferred, name.lower()))
    options = [f"FILM (ComfyUI native): {name}" for name in film_models]
    if not options:
        options = ["No FILM model found"]
    return (options, {"default": options[0]})


def _release_cached_engine(class_id, cache_key):
    node_class = _node_class(class_id)
    module_globals = getattr(node_class, node_class.FUNCTION).__globals__
    cache = module_globals.get("ENGINE_CACHE", {})
    engine = cache.pop(cache_key, None)
    if engine is not None:
        engine.reset()


def _release_loaded_models(stage):
    free_before = mm.get_free_memory()
    mm.soft_empty_cache()
    mm.unload_all_models()
    gc.collect()
    free_after = mm.get_free_memory()
    log.info(
        "[CoachBate TensorRT] %s: freed %.2f GiB VRAM (%.2f -> %.2f GiB free).",
        stage,
        max(0, free_after - free_before) / 1024**3,
        free_before / 1024**3,
        free_after / 1024**3,
    )


def _run_film(interpolation_model, images, multipliers, interpolation_states):
    from comfy_extras.nodes_frame_interpolation import FrameInterpolate

    pair_count = len(images) - 1
    skipped = [
        interpolation_states is not None
        and interpolation_states.is_frame_skipped(index)
        for index in range(pair_count)
    ]
    if pair_count > 0 and not any(skipped) and len(set(multipliers)) == 1:
        multiplier = multipliers[0]
        if multiplier == 1:
            return images
        mm.throw_exception_if_processing_interrupted()
        return FrameInterpolate.execute(
            interp_model=interpolation_model,
            images=images,
            multiplier=multiplier,
        )[0]

    output = []
    for index, multiplier in enumerate(multipliers):
        mm.throw_exception_if_processing_interrupted()
        if skipped[index] or multiplier == 1:
            output.append(images[index : index + 1])
            continue
        pair = FrameInterpolate.execute(
            interp_model=interpolation_model,
            images=images[index : index + 2],
            multiplier=multiplier,
        )[0]
        output.append(pair[:-1])
    output.append(images[-1:])
    return torch.cat(output, dim=0)




class CoachBateUpscaleRifeTensorRT:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "upscale_model": _required_input(
                    "LoadUpscalerTensorrtModel",
                    "model",
                    (["4x_foolhardy_Remacri_ExtraSmoother"],),
                ),
                "upscale_precision": _required_input(
                    "LoadUpscalerTensorrtModel", "precision", (["fp16", "fp32"],)
                ),
                "resize_to": _required_input(
                    "UpscalerTensorrt", "resize_to", (["none", "FHD", "2k", "4k"],)
                ),
                "resize_width": ("INT", {"default": 3840, "min": 1, "max": 8192}),
                "resize_height": ("INT", {"default": 2160, "min": 1, "max": 8192}),
                "rife_model": _required_input(
                    "AutoLoadRifeTensorrtModel",
                    "model",
                    (["rife49_ensemble_True_scale_1_sim"],),
                ),
                "rife_precision": _required_input(
                    "AutoLoadRifeTensorrtModel", "precision", (["fp16", "fp32"],)
                ),
                "rife_resolution_profile": _required_input(
                    "AutoLoadRifeTensorrtModel",
                    "resolution_profile",
                    (["small", "medium", "large"], {"default": "small"}),
                ),
                "rife_custom_min_dimension": (
                    "INT", {"default": 384, "min": 64, "max": 4096, "step": 8}
                ),
                "rife_custom_opt_dimension": (
                    "INT", {"default": 720, "min": 64, "max": 4096, "step": 8}
                ),
                "rife_custom_max_dimension": (
                    "INT", {"default": 1312, "min": 64, "max": 4096, "step": 8}
                ),
                "rife_multiplier": (
                    "INT",
                    {
                        "default": 2,
                        "min": 1,
                        "max": 16,
                        "tooltip": "FILM intervals per source interval. Set to 1 to bypass FILM and preserve source FPS.",
                    },
                ),
                "rife_batch_size": ("INT", {"default": 1, "min": 1, "max": 16}),
                "clear_cache_after_n_frames": (
                    "INT", {"default": 100, "min": 1, "max": 1000}
                ),
                "keep_rife_model_loaded": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Compatibility setting; streaming manages engine lifetime automatically.",
                    },
                ),
                "source_fps": (
                    "FLOAT", {"default": 24.0, "min": 0.01, "max": 240.0, "step": 0.01}
                ),
                "playback_mode": (
                    ["preserve duration", "slow motion"],
                    {"default": "preserve duration"},
                ),
                "codec": (["H.265 / HEVC", "H.264 / AVC"],),
                "pixel_format": (["yuv420p10le", "yuv420p"],),
                "crf": ("INT", {"default": 16, "min": 0, "max": 51, "step": 1}),
                "filename_prefix": ("STRING", {"default": "CoachBate_TensorRT"}),
                "save_output": ("BOOLEAN", {"default": True}),
                "save_metadata": ("BOOLEAN", {"default": True}),
                "pingpong": ("BOOLEAN", {"default": False}),
                "trim_to_audio": ("BOOLEAN", {"default": False}),
                "interpolation_model": _native_interpolation_model_input(),
            },
            "optional": {
                "multiplier_list": ("STRING", {"multiline": True, "default": ""}),
                "interpolation_states": ("INTERPOLATION_STATES",),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("VHS_FILENAMES", "FLOAT")
    RETURN_NAMES = ("filenames", "output_fps")
    FUNCTION = "process"
    CATEGORY = "CoachBate/video"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Run TensorRT upscaling and native FILM interpolation as optimized full "
        "passes, then encode an H.264/H.265 video with audio."
    )

    def process(
        self,
        images,
        audio,
        upscale_model,
        upscale_precision,
        resize_to,
        resize_width,
        resize_height,
        rife_model,
        rife_precision,
        rife_resolution_profile,
        rife_custom_min_dimension,
        rife_custom_opt_dimension,
        rife_custom_max_dimension,
        rife_multiplier,
        rife_batch_size,
        clear_cache_after_n_frames,
        keep_rife_model_loaded,
        source_fps,
        playback_mode,
        codec,
        pixel_format,
        crf,
        filename_prefix,
        save_output,
        save_metadata,
        pingpong,
        trim_to_audio,
        interpolation_model,
        multiplier_list="",
        interpolation_states=None,
        prompt=None,
        extra_pnginfo=None,
        unique_id=None,
    ):
        # Retained in the Python signature so older queued API prompts remain
        # valid. Scheduling and cache cadence are now automatic.
        del (
            clear_cache_after_n_frames,
            keep_rife_model_loaded,
            rife_model,
            rife_precision,
            rife_resolution_profile,
            rife_custom_min_dimension,
            rife_custom_opt_dimension,
            rife_custom_max_dimension,
            rife_batch_size,
        )
        mm.throw_exception_if_processing_interrupted()
        input_frame_count = int(images.shape[0])
        if input_frame_count < 1:
            raise ValueError("At least one input frame is required")

        multipliers = _parse_multipliers(
            multiplier_list, max(input_frame_count - 1, 0), rife_multiplier
        )
        upscale_spec = _node_class("LoadUpscalerTensorrtModel")().load_upscaler_tensorrt_model(
            upscale_model, upscale_precision
        )[0]
        needs_interpolation = any(
            multiplier > 1
            and not (
                interpolation_states is not None
                and interpolation_states.is_frame_skipped(index)
            )
            for index, multiplier in enumerate(multipliers)
        )
        _release_loaded_models("Before TensorRT upscale")
        upscale_cache_key = upscale_spec["tensorrt_model_path"]
        try:
            processed_images = _node_class("UpscalerTensorrt")().upscaler_tensorrt(
                images=images,
                upscaler_trt_model=upscale_spec,
                resize_to=resize_to,
                resize_width=resize_width,
                resize_height=resize_height,
                _output_device=mm.intermediate_device(),
                _keep_model_loaded=False,
                _suppress_progress=False,
            )[0]
        finally:
            _release_cached_engine("UpscalerTensorrt", upscale_cache_key)

        if needs_interpolation:
            if not interpolation_model.startswith("FILM (ComfyUI native): "):
                raise RuntimeError(
                    "FILM interpolation was requested, but no FILM model is available in "
                    "ComfyUI's frame_interpolation model folder."
                )
            _release_loaded_models("Before FILM interpolation")
            from comfy_extras.nodes_frame_interpolation import FrameInterpolationModelLoader

            model_name = interpolation_model.split(": ", 1)[1]
            interpolation_model_patcher = FrameInterpolationModelLoader.execute(
                model_name=model_name
            )[0]
            try:
                processed_images = _run_film(
                    interpolation_model_patcher,
                    processed_images,
                    multipliers,
                    interpolation_states,
                )
            finally:
                _release_loaded_models("After FILM interpolation")

        if playback_mode == "slow motion" or input_frame_count <= 1:
            output_fps = float(source_fps)
        else:
            output_fps = (
                float(source_fps)
                * (len(processed_images) - 1)
                / (input_frame_count - 1)
            )

        log.info(
            "[CoachBate TensorRT] Encoding %d input frames into %d output frames at %.3f FPS.",
            input_frame_count,
            len(processed_images),
            output_fps,
        )
        encoded = _node_class("CoachBateVideoCombine")().combine_video(
            images=processed_images,
            audio=audio,
            frame_rate=output_fps,
            loop_count=0,
            filename_prefix=filename_prefix,
            format="video/h265-mp4" if codec.startswith("H.265") else "video/h264-mp4",
            pingpong=pingpong,
            save_output=save_output,
            pix_fmt=pixel_format,
            crf=crf,
            save_metadata=save_metadata,
            trim_to_audio=trim_to_audio,
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
            unique_id=unique_id,
        )
        encoded["result"] = encoded["result"] + (output_fps,)
        return encoded
