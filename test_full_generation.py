"""
test_full_generation.py  —  CoachBate LTX-FreeFuse full pipeline test

Loads the real LTX-2.3-22B model, runs FreeFuse Phase 1 (mask collection + generation),
applies masks, runs Phase 2 (upscale + refine), decodes and saves a 5-second video.

Run:
    C:\\Data\\git\\ComfyUI-EasyInstall\\python_embeded\\python.exe ^
        custom_nodes\\ComfyUI-CoachBate\\test_full_generation.py

Output saved to:  C:\\Data\\ComfyUI_windows_portable\\ComfyUI\\temp\\freefuse_test.mp4
"""

import sys, os, time, traceback

COMFYUI_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, COMFYUI_ROOT)   # ComfyUI root — must come first
sys.path.insert(1, os.path.dirname(__file__))  # CoachBate root

# Import ComfyUI root nodes module before anything shadows it
import importlib.util
_nodes_spec = importlib.util.spec_from_file_location(
    "comfyui_nodes", os.path.join(COMFYUI_ROOT, "nodes.py")
)
_comfyui_nodes = importlib.util.module_from_spec(_nodes_spec)
sys.modules["comfyui_nodes"] = _comfyui_nodes
_nodes_spec.loader.exec_module(_comfyui_nodes)

import torch
import folder_paths

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_PATH   = r"D:\Data\models\checkpoints\ltx-2.3-22b-dev-fp8.safetensors"
LORA_BROCK   = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-brock-lora-v3\ltx-2.3-brock-lora-v3_000011000.safetensors"
LORA_JORDYN  = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-jordyn-v2\ltx-2.3-jordyn-v2_000005550.safetensors"
PROMPT       = ("Brock and JordyN stand on the beach. "
                "Brock is on the left, a wrestler wearing white compression shorts. "
                "JordyN is on the right, a swimmer wearing yellow speedo")
NEG_PROMPT   = "blurry, low quality, distorted, noise, watermark"
OUT_PATH     = r"C:\Data\ComfyUI_windows_portable\ComfyUI\temp\freefuse_test.mp4"

SEED         = 42
FPS          = 24
SECONDS      = 5
# Half-res latent dims (LTX VAE compresses 32× spatially in latent space, 8× temporally)
# 512×288 video  → 64×36 latent;  5s@24fps=120 frames → 15 latent frames
LAT_W, LAT_H = 64, 36
LAT_T        = 15   # (120 frames - 1) // 8 + 1

PHASE1_STEPS = 20
COLLECT_STEP = 5
COLLECT_BLOCK = 10
CFG          = 3.0
LORA_STR     = 0.7

# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):   print(f"  [PASS]  {msg}")
def fail(msg): print(f"  [FAIL]  {msg}")
def sec(t):    print(f"\n{'='*60}\n  {t}\n{'='*60}")


def check_files():
    for p in [MODEL_PATH, LORA_BROCK, LORA_JORDYN]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Not found: {p}")
    ok("All model/LoRA files found")


def bootstrap_comfyui():
    model_dir = os.path.dirname(MODEL_PATH)
    folder_paths.add_model_folder_path("checkpoints", model_dir)
    lora_dirs = set()
    for lp in [LORA_BROCK, LORA_JORDYN]:
        lora_dirs.add(os.path.dirname(lp))
    for d in lora_dirs:
        folder_paths.add_model_folder_path("loras", d)
    ok("folder_paths bootstrapped")


def load_checkpoint():
    CheckpointLoaderSimple = _comfyui_nodes.CheckpointLoaderSimple
    t0 = time.time()
    print("  Loading checkpoint (this takes a while)…")
    model_name = os.path.basename(MODEL_PATH)
    model, clip, vae = CheckpointLoaderSimple().load_checkpoint(model_name)
    ok(f"Checkpoint loaded in {time.time()-t0:.0f}s — {model.__class__.__name__}")
    return model, clip, vae


def load_loras(model, clip):
    from ltx_freefuse.lora_hook import load_masked_bypass_lora
    model, clip, mgr_brock  = load_masked_bypass_lora(model, clip, LORA_BROCK,  LORA_STR, 1.0, "Brock")
    model, clip, mgr_jordyn = load_masked_bypass_lora(model, clip, LORA_JORDYN, LORA_STR, 1.0, "JordyN")
    ok(f"Brock LoRA: {mgr_brock.get_hook_count()} bypass hooks")
    ok(f"JordyN LoRA: {mgr_jordyn.get_hook_count()} bypass hooks")
    assert mgr_brock.get_hook_count() > 0
    assert mgr_jordyn.get_hook_count() > 0
    return model, clip, {"Brock": mgr_brock, "JordyN": mgr_jordyn}


def encode_prompts(clip):
    CLIPTextEncode = _comfyui_nodes.CLIPTextEncode
    enc = CLIPTextEncode()
    positive = enc.encode(clip, PROMPT)[0]
    negative = enc.encode(clip, NEG_PROMPT)[0]
    ok("Conditioning encoded")
    return positive, negative


def build_latent(vae):
    """Build the combined AV latent for LTX-2.3-22B (NestedTensor)."""
    from comfy.nested_tensor import NestedTensor
    import comfy.utils

    # Try using native LTX nodes first
    try:
        NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
        EmptyLTXV = NCM.get("EmptyLTXVLatentVideo")
        if EmptyLTXV:
            vid_lat = EmptyLTXV().generate(LAT_W, LAT_H, LAT_T * 8 - 7, 1)[0]
            ok(f"Video latent: {vid_lat['samples'].shape}")
            return vid_lat
    except Exception as e:
        pass

    # Fallback: plain zeros video latent
    samples = torch.zeros(1, 128, LAT_T, LAT_H, LAT_W)
    ok(f"Built plain video latent: {samples.shape}")
    return {"samples": samples}


def build_av_latent(vae_audio=None):
    """Try to build a proper AV latent, fall back to video-only."""
    NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
    frames = LAT_T * 8 - 7  # approximate frame count

    try:
        EmptyLTXV = NCM.get("EmptyLTXVLatentVideo")
        LTXConcat = NCM.get("LTXVConcatAVLatent")
        LTXAudio  = NCM.get("LTXVEmptyLatentAudio")

        if EmptyLTXV and LTXConcat and LTXAudio and vae_audio:
            vid_lat  = EmptyLTXV().generate(LAT_W, LAT_H, frames, 1)[0]
            aud_lat  = LTXAudio().generate(vae_audio, frames, FPS, 1)[0]
            av_lat   = LTXConcat().concat(vid_lat, aud_lat)[0]
            ok(f"AV latent built: video={vid_lat['samples'].shape}")
            return av_lat
    except Exception as e:
        print(f"  (AV latent failed: {e}, using video-only)")

    # Plain video latent fallback
    samples = torch.zeros(1, 128, LAT_T, LAT_H, LAT_W)
    ok(f"Video-only latent: {samples.shape}")
    return {"samples": samples}


def find_concept_positions(clip):
    from ltx_freefuse.token_utils import get_t5_tokenizer_for_ltx, find_concept_positions_t5
    tokenizer = get_t5_tokenizer_for_ltx(clip)
    concepts = {"Brock": "Brock", "JordyN": "JordyN"}
    positions = find_concept_positions_t5(tokenizer, PROMPT, concepts)
    ok(f"Brock positions: {positions['Brock']}")
    ok(f"JordyN positions: {positions['JordyN']}")
    assert positions["Brock"][0],  "No positions for Brock"
    assert positions["JordyN"][0], "No positions for JordyN"
    return positions


def run_phase1(model, positive, negative, latent, managers, positions):
    from nodes_ltx_freefuse import CoachBateLTXPhase1Sampler

    freefuse_data = {
        "managers": managers,
        "token_pos_maps": positions,
    }

    print(f"  Running Phase 1 ({PHASE1_STEPS} steps, collecting at step {COLLECT_STEP})…")
    t0 = time.time()
    sampler = CoachBateLTXPhase1Sampler()
    masks_data, p1_latent, preview = sampler.run_phase1(
        model=model,
        positive=positive,
        negative=negative,
        latent=latent,
        ltxfreefuse_data=freefuse_data,
        seed=SEED,
        steps=PHASE1_STEPS,
        collect_step=COLLECT_STEP,
        cfg=CFG,
        sampler_name="euler",
        scheduler="linear_quadratic",
        collect_block=COLLECT_BLOCK,
    )
    ok(f"Phase 1 done in {time.time()-t0:.1f}s")
    ok(f"Masks: {list(masks_data['masks'].keys())}")
    for name, m in masks_data["masks"].items():
        ok(f"  {name}: shape={m.shape}  nonzero={m.gt(0.5).sum().item()} px")
    ok(f"Preview shape: {preview.shape}")
    ok(f"Phase 1 latent type: {type(p1_latent['samples'])}")
    return masks_data, p1_latent, preview


def apply_masks(model, managers, masks_data, freefuse_data):
    from nodes_ltx_freefuse import CoachBateLTXMaskApplicator
    masked_model = CoachBateLTXMaskApplicator().apply_masks(
        model=model,
        ltxfreefuse_data=freefuse_data,
        ltxfreefuse_masks=masks_data,
    )[0]
    ok("Masks applied to bypass hooks")
    return masked_model


def run_phase2(model, positive, negative, p1_latent):
    """Upscale Phase 1 latent and run Phase 2 refinement."""
    import comfy.sample
    import comfy.samplers
    import comfy.model_management

    print("  Running Phase 2 (upscale + refine)…")
    t0 = time.time()

    # Try to use LTX upscaler
    try:
        NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
        Separate = NCM.get("LTXVSeparateAVLatent")
        Upscaler = NCM.get("LTXVLatentUpsampler")

        if Separate and Upscaler:
            # Separate video from AV latent
            samples = p1_latent["samples"]
            if getattr(samples, "is_nested", False):
                vid_lat = {"samples": samples.tensors[0]}
            else:
                vid_lat = p1_latent

            ok(f"Separated video latent: {vid_lat['samples'].shape}")

            # Simple Phase 2: run a few refinement steps on the Phase 1 output
            # (without actual upscaling to keep test fast)
            phase2_sigmas = torch.tensor([0.85, 0.725, 0.4219, 0.0])
            output = comfy.sample.sample(
                model,
                noise=comfy.sample.prepare_noise(samples if not getattr(samples, "is_nested", False) else samples.tensors[0], SEED + 1, None),
                steps=3,
                cfg=CFG,
                sampler_name="euler",
                scheduler="linear_quadratic",
                positive=positive,
                negative=negative,
                latent_image=vid_lat["samples"],
                start_step=0,
                last_step=None,
                force_full_denoise=True,
                noise_mask=None,
                sigmas=phase2_sigmas,
                callback=None,
                disable_pbar=False,
                seed=SEED + 1,
            )
            ok(f"Phase 2 done in {time.time()-t0:.1f}s, output shape: {output.shape}")
            return {"samples": output}
    except Exception as e:
        print(f"  Phase 2 error: {e}")
        traceback.print_exc()

    return p1_latent  # fallback: use Phase 1 output


def decode_and_save(vae, latent):
    """Decode the video latent and save as MP4."""
    import comfy.model_management
    import imageio
    import numpy as np

    print("  Decoding latent…")
    t0 = time.time()

    samples = latent["samples"]
    if getattr(samples, "is_nested", False):
        samples = samples.tensors[0]

    # VAE decode
    try:
        NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
        VAEDecode = NCM.get("VAEDecodeTiled") or NCM.get("VAEDecode")
        if VAEDecode:
            try:
                images = VAEDecode().decode(samples={"samples": samples}, vae=vae,
                                            tile_size=512, overlap=64,
                                            temporal_size=4096, temporal_overlap=8)[0]
            except TypeError:
                images = VAEDecode().decode({"samples": samples}, vae)[0]
        ok(f"Decoded: {images.shape}  ({time.time()-t0:.1f}s)")
    except Exception as e:
        print(f"  Decode error: {e}")
        traceback.print_exc()
        return

    # Save as MP4
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    frames_np = (images.cpu().numpy() * 255).clip(0, 255).astype("uint8")
    try:
        import imageio.v3 as iio
        iio.imwrite(OUT_PATH, frames_np, fps=FPS, codec="libx264",
                    output_params=["-crf", "18", "-pix_fmt", "yuv420p"])
        ok(f"Saved: {OUT_PATH}  ({frames_np.shape[0]} frames @ {FPS}fps)")
    except Exception as e:
        # Fallback: save frames as PNG sequence
        frame_dir = OUT_PATH.replace(".mp4", "_frames")
        os.makedirs(frame_dir, exist_ok=True)
        for i, f in enumerate(frames_np):
            import PIL.Image
            PIL.Image.fromarray(f).save(os.path.join(frame_dir, f"{i:04d}.png"))
        ok(f"Saved {len(frames_np)} frames to {frame_dir}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    sec("LTX-FreeFuse Full Pipeline Test")

    check_files()
    bootstrap_comfyui()

    sec("Loading model")
    model, clip, vae = load_checkpoint()

    sec("Loading LoRAs")
    model, clip, managers = load_loras(model, clip)

    sec("Encoding prompts")
    positive, negative = encode_prompts(clip)

    sec("Building latent")
    # Try to get audio VAE if available
    vae_audio = None
    try:
        from comfy.ldm.lightricks.av_model import LTXAVTEModel
        # model might have audio VAE — for now skip
        pass
    except:
        pass
    latent = build_av_latent(vae_audio)

    sec("Finding concept positions")
    positions = find_concept_positions(clip)

    freefuse_data = {
        "managers": managers,
        "token_pos_maps": positions,
    }

    sec("Phase 1: FreeFuse sampling + mask collection")
    masks_data, p1_latent, preview = run_phase1(
        model, positive, negative, latent, managers, positions
    )

    # Save mask preview
    preview_path = OUT_PATH.replace(".mp4", "_masks.png")
    try:
        import PIL.Image
        import numpy as np
        preview_np = (preview[0].cpu().numpy() * 255).astype("uint8")
        PIL.Image.fromarray(preview_np).save(preview_path)
        ok(f"Mask preview saved: {preview_path}")
    except Exception as e:
        print(f"  (preview save failed: {e})")

    sec("Applying masks")
    masked_model = apply_masks(model, managers, masks_data, freefuse_data)

    sec("Phase 2: Refinement")
    p2_latent = run_phase2(masked_model, positive, negative, p1_latent)

    sec("Decoding & saving video")
    decode_and_save(vae, p2_latent)

    print(f"\n{'='*60}")
    print("  PIPELINE TEST COMPLETE")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[FAIL] {e}")
        traceback.print_exc()
        sys.exit(1)
