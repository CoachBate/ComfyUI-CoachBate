from comfy_extras.nodes_lt import LTXVAddGuide
import torch
import comfy.utils
from comfy_api.latest import io
from .ltx_director import GuideData


class LTXDirectorGuide(LTXVAddGuide):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CoachBateLTXDirectorGuide",
            display_name="LTX Director Guide",
            category="CoachBate",
            is_experimental=True,
            is_dev_only=True,
            description=(
                "Applies guide images from a Prompt Relay Timeline node at the frame positions "
                "and strengths defined on the timeline. Connect guide_data from the timeline node. "
                "Connect video_latent_length to LTX Trim Latent after the KSampler to strip the "
                "extra guide frames that LTXV appends during conditioning."
            ),
            inputs=[
                io.Conditioning.Input("positive", tooltip="Positive conditioning to add guide keyframe info to."),
                io.Conditioning.Input("negative", tooltip="Negative conditioning to add guide keyframe info to."),
                io.Vae.Input("vae", tooltip="Video VAE used to encode the guide images."),
                io.Latent.Input("latent", tooltip="Video latent — guides are inserted into this latent."),
                GuideData.Input("guide_data", tooltip="Guide data produced by Prompt Relay Encode (Timeline)."),
                io.Float.Input("scale_by", default=1.0, min=0.01, max=8.0, step=0.01, tooltip="Scale the latent by this factor."),
                io.Combo.Input("upscale_method", options=["nearest-exact", "bilinear", "area", "bicubic", "bislerp"], default="bicubic", tooltip="Method used to upscale/downscale the latent."),
            ],
            outputs=[
                io.Conditioning.Output(display_name="positive"),
                io.Conditioning.Output(display_name="negative"),
                io.Latent.Output(
                    display_name="latent",
                    tooltip=(
                        "Video latent with guide frames appended at the end for conditioning. "
                        "After the KSampler, connect to LTX Trim Latent with video_latent_length "
                        "to strip the extra guide frames before VAE decoding."
                    ),
                ),
                io.Int.Output(
                    display_name="video_latent_length",
                    tooltip=(
                        "Original video latent frame count before guide frames were appended. "
                        "Connect to LTX Trim Latent after the KSampler to trim the output to the "
                        "correct length and avoid garbage frames at the end of the decoded video."
                    ),
                ),
            ],
        )

    @classmethod
    def execute(cls, positive, negative, vae, latent, guide_data, scale_by=1.0, upscale_method="bicubic") -> io.NodeOutput:
        scale_factors = vae.downscale_index_formula

        # Clone latents to avoid mutating upstream nodes
        latent_image = latent["samples"].clone()

        if "noise_mask" in latent:
            noise_mask = latent["noise_mask"].clone()
        else:
            batch, _, latent_frames, latent_height, latent_width = latent_image.shape
            noise_mask = torch.ones(
                (batch, 1, latent_frames, 1, 1),
                dtype=torch.float32,
                device=latent_image.device,
            )

        # Apply scale factor if not 1.0
        if scale_by != 1.0:
            B, C, F, H, W = latent_image.shape
            width = round(W * scale_by)
            height = round(H * scale_by)

            # Reshape to 4D for common_upscale
            latent_4d = latent_image.permute(0, 2, 1, 3, 4).reshape(B * F, C, H, W)
            latent_resized_4d = comfy.utils.common_upscale(latent_4d, width, height, upscale_method, "disabled")
            latent_image = latent_resized_4d.reshape(B, F, C, height, width).permute(0, 2, 1, 3, 4)

            # Also resize noise mask if it's not a broadcasted mask
            if noise_mask.shape[-1] > 1 or noise_mask.shape[-2] > 1:
                mask_4d = noise_mask.permute(0, 2, 1, 3, 4).reshape(B * F, 1, H, W)
                mask_resized_4d = comfy.utils.common_upscale(mask_4d, width, height, upscale_method, "disabled")
                noise_mask = mask_resized_4d.reshape(B, F, 1, height, width).permute(0, 2, 1, 3, 4)

        _, _, latent_length, latent_height, latent_width = latent_image.shape
        # Save the video-only frame count before guide frames are appended.
        # append_keyframe grows the latent by one frame per guide image; those extra frames
        # must exist during sampling (they carry the conditioning signal) but must be stripped
        # before VAE decoding to avoid garbage frames at the end of the output video.
        original_latent_length = latent_length

        images = guide_data.get("images", [])
        insert_frames = guide_data.get("insert_frames", [])
        strengths = guide_data.get("strengths", [])

        for idx, img_tensor in enumerate(images):
            f_idx = insert_frames[idx] if idx < len(insert_frames) else 0
            strength = strengths[idx] if idx < len(strengths) else 1.0

            image_1, t = cls.encode(vae, latent_width, latent_height, img_tensor, scale_factors)
            frame_idx, latent_idx = cls.get_latent_index(positive, latent_length, len(image_1), f_idx, scale_factors)

            assert latent_idx + t.shape[2] <= latent_length, (
                f"Guide image {idx + 1}: conditioning frames exceed the length of the latent sequence."
            )

            positive, negative, latent_image, noise_mask = cls.append_keyframe(
                positive, negative, frame_idx, latent_image, noise_mask, t, strength, scale_factors,
            )

        return io.NodeOutput(
            positive,
            negative,
            {"samples": latent_image, "noise_mask": noise_mask},
            original_latent_length,
        )


class LTXTrimLatent(io.ComfyNode):
    """Trims sampled video and/or audio latents to their original lengths, removing
    any extra frames appended during guide conditioning.

    Place between the KSampler and VAE Decode nodes. Connect video_latent_length
    from LTX Director Guide and audio_latent_length from LTX Director."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CoachBateLTXTrimLatent",
            display_name="LTX Trim Latent",
            category="CoachBate",
            is_experimental=True,
            is_dev_only=True,
            description=(
                "Strips extra frames from sampled LTXV video and/or audio latents. "
                "Connect video_latent_length from LTX Director Guide and audio_latent_length "
                "from LTX Director. Place between the KSampler and VAE Decode nodes."
            ),
            inputs=[
                io.Latent.Input(
                    "video_latent",
                    optional=True,
                    tooltip="Sampled video latent from the KSampler (may contain appended guide frames).",
                ),
                io.Latent.Input(
                    "audio_latent",
                    optional=True,
                    tooltip="Sampled audio latent from the KSampler.",
                ),
                io.Int.Input(
                    "video_latent_length",
                    default=1,
                    min=0,
                    max=100000,
                    tooltip="Original video latent frame count from LTX Director Guide. 0 = no trim.",
                ),
                io.Int.Input(
                    "audio_latent_length",
                    default=0,
                    min=0,
                    max=100000,
                    tooltip="Original audio latent frame count from LTX Director. 0 = no trim.",
                ),
            ],
            outputs=[
                io.Latent.Output(
                    display_name="video_latent",
                    tooltip="Video latent trimmed to the original frame count, ready for VAE decoding.",
                ),
                io.Latent.Output(
                    display_name="audio_latent",
                    tooltip="Audio latent trimmed to the original frame count, ready for VAE decoding.",
                ),
            ],
        )

    @classmethod
    def execute(cls, video_latent_length, audio_latent_length=0, video_latent=None, audio_latent=None) -> io.NodeOutput:
        def trim(latent_dict, length):
            if latent_dict is None or not latent_dict or length <= 0:
                return latent_dict or {}
            samples = latent_dict["samples"]
            if length >= samples.shape[2]:
                return latent_dict
            trimmed = {"samples": samples[:, :, :length].clone()}
            if "noise_mask" in latent_dict:
                trimmed["noise_mask"] = latent_dict["noise_mask"][:, :, :length].clone()
            if "type" in latent_dict:
                trimmed["type"] = latent_dict["type"]
            return trimmed

        return io.NodeOutput(
            trim(video_latent, video_latent_length),
            trim(audio_latent, audio_latent_length),
        )
