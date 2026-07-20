"""
generate_video.py  —  Full FreeFuse video generation (standalone)

Produces a real MP4 of "Brock and JordyN on the beach" using:
  - LTX-2.3-22B distilled FP8 model + distilled LoRA (8 steps, fast)
  - Brock + JordyN character LoRAs with FreeFuse spatial masking
  - LTX Video API for text conditioning (no local Gemma3 needed)

Output: C:\\Data\\ComfyUI_windows_portable\\ComfyUI\\temp\\freefuse_out.mp4

Run:
    C:\\Data\\git\\ComfyUI-EasyInstall\\python_embeded\\python.exe ^
        custom_nodes\\ComfyUI-CoachBate\\generate_video.py
"""

import sys, os, io, pickle, time, traceback
import requests as _req

COMFYUI_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, COMFYUI_ROOT)
sys.path.insert(1, os.path.dirname(__file__))

# Load ComfyUI root nodes before CoachBate's nodes.py shadows it
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("comfyui_nodes", os.path.join(COMFYUI_ROOT, "nodes.py"))
_comfyui_nodes = _ilu.module_from_spec(_spec)
sys.modules["comfyui_nodes"] = _comfyui_nodes
_spec.loader.exec_module(_comfyui_nodes)

import torch
import folder_paths

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_PATH   = r"D:\Data\models\checkpoints\ltx-2.3-22b-dev-fp8.safetensors"
DISTILLED_LORA = r"M:\models\loras\ltx-2.3-22b-distilled-lora-384-1.1.safetensors"
DISTILLED_LORA_STR_P1 = 0.25   # lighter during mask collection
DISTILLED_LORA_STR_P2 = 0.6    # stronger for actual generation
LORA_BROCK   = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-brock-lora-v3\ltx-2.3-brock-lora-v3_000011000.safetensors"
LORA_JORDYN  = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-jordyn-v2\ltx-2.3-jordyn-v2_000005550.safetensors"
VIDEO_VAE    = r"D:\Data\models\vae\LTX23_video_vae_bf16.safetensors"
AUDIO_VAE    = r"D:\Data\models\vae\LTX23_audio_vae_bf16.safetensors"
GEMMA_TE     = r"M:\models\text_encoders\gemma_3_12B_it_fp4_mixed.safetensors"

LTX_API_KEY  = os.environ.get("LTX_API_KEY", "")
LTX_API_URL  = "https://api.ltx.video/v1/prompt-embedding"

PROMPT       = ("Brock and JordyN stand on the beach. "
                "Brock is on the left, a wrestler wearing white compression shorts. "
                "JordyN is on the right, a swimmer wearing yellow speedo")
NEG_PROMPT   = "blurry, low quality, distorted, noise, watermark, bad anatomy"

SEED         = 42
FPS          = 24
# 5 seconds @24fps. EmptyLTXVLatentVideo expects the raw frame count.
FRAMES       = 121          # 5s × 24fps + 1
LORA_STR     = 0.7
CFG          = 1.0   # distilled model uses CFG=1 (classifier-free guidance baked in)
PHASE1_STEPS = 8
COLLECT_STEP = 4     # collect at step 4/8 — layout well established by then
COLLECT_BLOCK = 10

# Half-res latent (LTX VAE compresses 8× spatially)
# 512×288 video → 64×36 latent
LAT_W, LAT_H = 64, 36

OUT_PATH     = r"C:\Data\ComfyUI_windows_portable\ComfyUI\temp\freefuse_out.mp4"
MASK_PATH    = r"C:\Data\ComfyUI_windows_portable\ComfyUI\temp\freefuse_masks.png"

# ── Helpers ───────────────────────────────────────────────────────────────────
def section(t): print(f"\n{'='*60}\n  {t}\n{'='*60}")
def ok(m):      print(f"  [OK]  {m}")
def info(m):    print(f"  ...   {m}")
def err(m):     print(f"  [ERR] {m}")

# ── Setup folder paths ────────────────────────────────────────────────────────
folder_paths.add_model_folder_path("checkpoints", os.path.dirname(MODEL_PATH))
folder_paths.add_model_folder_path("vae", os.path.dirname(VIDEO_VAE))
if os.path.exists(os.path.dirname(GEMMA_TE)):
    folder_paths.add_model_folder_path("text_encoders", os.path.dirname(GEMMA_TE))

# ── LTX API conditioning ─────────────────────────────────────────────────────
def extract_model_id():
    from safetensors import safe_open
    with safe_open(MODEL_PATH, framework="pt", device="cpu") as f:
        return f.metadata()["encrypted_wandb_properties"]

def api_encode(prompt_text, model_id):
    resp = _req.post(
        LTX_API_URL,
        json={"prompt": prompt_text, "model_id": model_id, "enhance_prompt": False},
        headers={"Authorization": f"Bearer {LTX_API_KEY}", "Content-Type": "application/json"},
        timeout=60,
    )
    assert resp.status_code == 200, f"API {resp.status_code}: {resp.text[:200]}"
    return pickle.load(io.BytesIO(resp.content))

# ── Tokenizer for concept positions ──────────────────────────────────────────
def load_tokenizer():
    """Load SentencePiece tokenizer from Gemma3 text encoder."""
    import sentencepiece as spm
    from safetensors import safe_open
    if not os.path.exists(GEMMA_TE):
        return None
    with safe_open(GEMMA_TE, framework="pt", device="cpu") as f:
        meta = f.metadata() or {}
        spm_bytes = meta.get("spiece_model")
        if spm_bytes is None:
            try:
                spm_bytes = f.get_tensor("spiece_model")
            except Exception:
                pass
    if spm_bytes is None:
        return None
    if hasattr(spm_bytes, "numpy"):
        spm_bytes = bytes(spm_bytes.numpy())
    return spm.SentencePieceProcessor(model_proto=spm_bytes)

# ── VAE loading ───────────────────────────────────────────────────────────────
def load_vaes():
    NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
    # Video VAE
    video_vae = NCM["VAELoader"]().load_vae(os.path.basename(VIDEO_VAE))[0]
    ok(f"Video VAE loaded: {video_vae.__class__.__name__}")
    # Audio VAE
    audio_vae = None
    AudioLoader = NCM.get("LTXVAudioVAELoader")
    if AudioLoader and os.path.exists(AUDIO_VAE):
        try:
            audio_vae = AudioLoader().load_audio_vae(os.path.basename(AUDIO_VAE))[0]
            ok(f"Audio VAE loaded: {audio_vae.__class__.__name__}")
        except Exception as e:
            info(f"Audio VAE skipped: {e}")
    return video_vae, audio_vae

# ── Build latent ─────────────────────────────────────────────────────────────
def build_latent(audio_vae):
    NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
    from comfy.nested_tensor import NestedTensor

    # Video latent
    EmptyVid = NCM.get("EmptyLTXVLatentVideo")
    if EmptyVid:
        vid_lat = EmptyVid().generate(LAT_W * 8, LAT_H * 8, FRAMES, 1)[0]
        ok(f"Video latent: {vid_lat['samples'].shape}")
    else:
        vid_lat = {"samples": torch.zeros(1, 128, (FRAMES - 1) // 4 + 1, LAT_H, LAT_W)}
        ok(f"Video latent (manual): {vid_lat['samples'].shape}")

    # Audio latent
    if audio_vae:
        EmptyAud = NCM.get("LTXVEmptyLatentAudio")
        if EmptyAud:
            try:
                aud_lat = EmptyAud().generate(audio_vae, FRAMES, FPS, 1)[0]
                ok(f"Audio latent: {aud_lat['samples'].shape}")
                # Combine into AV latent
                ConcatAV = NCM.get("LTXVConcatAVLatent")
                if ConcatAV:
                    av_lat = ConcatAV().concat(vid_lat, aud_lat)[0]
                    ok("AV latent (video+audio) built")
                    return av_lat
            except Exception as e:
                info(f"Audio latent failed: {e}, using video-only")

    return vid_lat

# ── Find token positions ──────────────────────────────────────────────────────
def get_token_positions(sp):
    from ltx_freefuse.token_utils import find_concept_positions_t5
    concepts = {"Brock": "Brock", "JordyN": "JordyN"}
    if sp is None:
        info("No tokenizer — using fallback positions")
        return {"Brock": [[1, 2]], "JordyN": [[3, 4]]}
    positions = find_concept_positions_t5(sp, PROMPT, concepts)
    ok(f"Brock positions: {positions['Brock']}")
    ok(f"JordyN positions: {positions['JordyN']}")
    return positions

# ── Phase 1: full generation + mask collection ────────────────────────────────
def run_phase1(model, positive, negative, latent, positions, managers):
    import comfy.sample, comfy.samplers, comfy.model_management
    from ltx_freefuse.attention_replace import (
        LTXFreeFuseState, apply_ltx_patches, compute_masks_from_state,
    )

    state = LTXFreeFuseState()
    state.collect_step      = COLLECT_STEP
    state.collect_block     = COLLECT_BLOCK
    state.collect_block_end = COLLECT_BLOCK
    state.token_pos_maps    = positions

    # Extract video dims from latent
    samples = latent["samples"]
    vid = samples.tensors[0] if getattr(samples, "is_nested", False) else samples
    _, _, lat_T, lat_H, lat_W = vid.shape if vid.dim() == 5 else (None, None, 1, *vid.shape[-2:])
    state.latent_t, state.latent_h, state.latent_w = lat_T, lat_H, lat_W
    ok(f"Latent dims: T={lat_T} H={lat_H} W={lat_W}")

    patched = model.clone()
    apply_ltx_patches(patched, state)

    sampler_obj = comfy.samplers.KSampler(
        patched, steps=PHASE1_STEPS,
        device=comfy.model_management.get_torch_device(),
        sampler="euler", scheduler="linear_quadratic",
        denoise=1.0, model_options=patched.model_options,
    )

    info(f"Phase 1: {PHASE1_STEPS} steps @ {lat_H}×{lat_W}...")
    t0 = time.time()
    p1_out = comfy.sample.sample(
        patched,
        noise=comfy.sample.prepare_noise(samples, SEED, None),
        steps=PHASE1_STEPS, cfg=CFG,
        sampler_name="euler", scheduler="linear_quadratic",
        positive=positive, negative=negative,
        latent_image=samples,
        start_step=0, last_step=None,
        force_full_denoise=True, noise_mask=None,
        sigmas=sampler_obj.sigmas,
        callback=None, disable_pbar=False, seed=SEED,
    )
    ok(f"Phase 1 done in {time.time()-t0:.0f}s")

    if not state.similarity_maps and not state.block_similarity_maps:
        raise RuntimeError("No similarity maps collected — block replace didn't fire")

    masks = compute_masks_from_state(state, lat_H, lat_W)
    ok(f"Masks: { {k: f'nonzero={v.gt(0.5).sum().item()}px' for k,v in masks.items()} }")

    # Save mask preview
    try:
        import PIL.Image, numpy as np
        colours = [torch.tensor([1.0,0.3,0.3]), torch.tensor([0.3,0.5,1.0])]
        canvas = torch.zeros(lat_H, lat_W, 3)
        for i,(n,m) in enumerate(masks.items()):
            canvas += m.float().unsqueeze(-1) * colours[i]
        arr = (canvas.clamp(0,1).numpy() * 255).astype("uint8")
        os.makedirs(os.path.dirname(MASK_PATH), exist_ok=True)
        PIL.Image.fromarray(arr).save(MASK_PATH)
        ok(f"Mask preview → {MASK_PATH}")
    except Exception as e:
        info(f"Mask preview save failed: {e}")

    return {"samples": p1_out}, {
        "masks": masks, "latent_t": lat_T, "latent_h": lat_H, "latent_w": lat_W
    }

# ── Apply masks ───────────────────────────────────────────────────────────────
def apply_masks(model, managers, masks_data):
    from ltx_freefuse.lora_hook import apply_masks_to_managers
    apply_masks_to_managers(
        managers, masks_data["masks"],
        masks_data["latent_t"], masks_data["latent_h"], masks_data["latent_w"]
    )
    ok("Spatial masks applied to bypass hooks")
    return model

# ── Phase 2: upscale + refine ─────────────────────────────────────────────────
def run_phase2(model, positive, negative, p1_latent, video_vae):
    import comfy.sample, comfy.model_management
    NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS

    samples = p1_latent["samples"]
    vid = samples.tensors[0] if getattr(samples, "is_nested", False) else samples

    # Try latent upscaler
    upscaled_vid = vid
    Upscaler = NCM.get("LTXVLatentUpsampler")
    UpscaleModel = NCM.get("GetNode")  # might not be available standalone
    if Upscaler:
        upscale_model_path = r"D:\Data\models\latent_upscale_models\ltx-2.3-spatial-upscaler-x2-1.1.safetensors"
        if os.path.exists(upscale_model_path):
            try:
                folder_paths.add_model_folder_path(
                    "latent_upscale_models",
                    os.path.dirname(upscale_model_path)
                )
                # Load upscale model via checkpoint loader workaround
                import comfy.utils
                upscale_sd = comfy.utils.load_torch_file(upscale_model_path)
                from comfy.ldm.lightricks.model import LTXVLatentUpscaler
                upscale_model = LTXVLatentUpscaler(upscale_sd)
                upscaled_vid = Upscaler().upscale(
                    {"samples": vid}, upscale_model, video_vae
                )[0]["samples"]
                ok(f"Upscaled latent: {upscaled_vid.shape}")
            except Exception as e:
                info(f"Upscaler failed ({e}), using Phase 1 output at original resolution")

    # Phase 2 sigmas (few refinement steps)
    phase2_sigmas = torch.tensor([0.85, 0.725, 0.4219, 0.0])
    info(f"Phase 2: {len(phase2_sigmas)-1} refinement steps @ full-res...")
    t0 = time.time()
    p2_out = comfy.sample.sample(
        model,
        noise=comfy.sample.prepare_noise(upscaled_vid, SEED + 1, None),
        steps=len(phase2_sigmas) - 1, cfg=1.0,
        sampler_name="euler", scheduler="linear_quadratic",
        positive=positive, negative=negative,
        latent_image=upscaled_vid,
        start_step=0, last_step=None,
        force_full_denoise=True, noise_mask=None,
        sigmas=phase2_sigmas,
        callback=None, disable_pbar=False, seed=SEED + 1,
    )
    ok(f"Phase 2 done in {time.time()-t0:.0f}s, output: {p2_out.shape}")
    return p2_out

# ── Decode + save ─────────────────────────────────────────────────────────────
def decode_and_save(video_tensor, video_vae):
    info("Decoding latent to frames...")
    t0 = time.time()

    # VAE decode (tiled for memory efficiency)
    NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
    TiledDecode = NCM.get("VAEDecodeTiled")
    if TiledDecode:
        try:
            images = TiledDecode().decode(
                samples={"samples": video_tensor}, vae=video_vae,
                tile_size=512, overlap=64, temporal_size=4096, temporal_overlap=8
            )[0]
        except TypeError:
            images = TiledDecode().decode({"samples": video_tensor}, video_vae)[0]
    else:
        images = NCM["VAEDecode"]().decode({"samples": video_tensor}, video_vae)[0]

    ok(f"Decoded {images.shape[0]} frames in {time.time()-t0:.0f}s, shape={images.shape}")

    # Save as MP4
    frames_np = (images.cpu().float().numpy() * 255).clip(0, 255).astype("uint8")
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    try:
        import imageio.v3 as iio
        iio.imwrite(
            OUT_PATH, frames_np, fps=FPS,
            codec="libx264",
            output_params=["-crf", "18", "-pix_fmt", "yuv420p"],
        )
        ok(f"Video saved → {OUT_PATH}  ({len(frames_np)} frames @ {FPS}fps)")
    except Exception as e:
        info(f"imageio failed ({e}), trying imageio v2...")
        try:
            import imageio
            writer = imageio.get_writer(OUT_PATH, fps=FPS, quality=8)
            for f in frames_np:
                writer.append_data(f)
            writer.close()
            ok(f"Video saved → {OUT_PATH}")
        except Exception as e2:
            # Last resort: save frames as PNG sequence
            frame_dir = OUT_PATH.replace(".mp4", "_frames")
            os.makedirs(frame_dir, exist_ok=True)
            import PIL.Image
            for i, f in enumerate(frames_np):
                PIL.Image.fromarray(f).save(os.path.join(frame_dir, f"{i:04d}.png"))
            ok(f"Saved {len(frames_np)} frames to {frame_dir}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    section("FreeFuse Video Generation")
    t_total = time.time()

    # Verify files
    for p in [MODEL_PATH, LORA_BROCK, LORA_JORDYN, VIDEO_VAE]:
        assert os.path.exists(p), f"Missing: {p}"
    ok("All required files found")

    # ── Load model ────────────────────────────────────────────────────────
    section("Loading model")
    loader = _comfyui_nodes.CheckpointLoaderSimple()
    model, _, _ = loader.load_checkpoint(os.path.basename(MODEL_PATH))
    ok(f"Model: {model.__class__.__name__}")

    # ── Load LoRAs ────────────────────────────────────────────────────────
    section("Loading LoRAs")
    from ltx_freefuse.lora_hook import load_masked_bypass_lora
    model, _, mgr_brock  = load_masked_bypass_lora(model, None, LORA_BROCK,  LORA_STR, 1.0, "Brock")
    model, _, mgr_jordyn = load_masked_bypass_lora(model, None, LORA_JORDYN, LORA_STR, 1.0, "JordyN")
    ok(f"Brock: {mgr_brock.get_hook_count()} hooks")
    ok(f"JordyN: {mgr_jordyn.get_hook_count()} hooks")
    managers = {"Brock": mgr_brock, "JordyN": mgr_jordyn}

    # ── Load VAEs ─────────────────────────────────────────────────────────
    section("Loading VAEs")
    video_vae, audio_vae = load_vaes()

    # ── Conditioning via API ──────────────────────────────────────────────
    section("Encoding conditioning via LTX API")
    model_id = extract_model_id()
    ok(f"model_id extracted (len={len(model_id)})")
    positive = api_encode(PROMPT, model_id)
    negative = api_encode(NEG_PROMPT, model_id)
    ok("Positive + negative conditioning ready")

    # ── Token positions ───────────────────────────────────────────────────
    section("Finding concept token positions")
    sp = load_tokenizer()
    positions = get_token_positions(sp)

    # ── Build latent ──────────────────────────────────────────────────────
    section("Building latent")
    latent = build_latent(audio_vae)

    # ── Load distilled LoRA (applied before both phases) ─────────────────
    section("Loading distilled LoRA")
    dist_lora_path = DISTILLED_LORA
    if not os.path.exists(dist_lora_path):
        # Try alternate locations
        for p in [
            r"D:\Data\models\loras\ltx-2.3-22b-distilled-lora-384-1.1.safetensors",
            r"M:\models\loras\ltx-2.3-22b-distilled-lora-384-1.1.safetensors",
        ]:
            if os.path.exists(p):
                dist_lora_path = p
                break
    if os.path.exists(dist_lora_path):
        NCM = _comfyui_nodes.NODE_CLASS_MAPPINGS
        LoraLoader = NCM.get("LoraLoaderModelOnly") or NCM.get("LoraLoader")
        if LoraLoader:
            folder_paths.add_model_folder_path("loras", os.path.dirname(dist_lora_path))
            lora_name = os.path.basename(dist_lora_path)
            # LoraLoaderModelOnly takes (model, lora_name, strength_model)
            # LoraLoader takes (model, clip, lora_name, strength_model, strength_clip)
            is_model_only = "ModelOnly" in LoraLoader.__name__
            if is_model_only:
                model_p1 = LoraLoader().load_lora(model, lora_name, DISTILLED_LORA_STR_P1)[0]
                model_p2 = LoraLoader().load_lora(model, lora_name, DISTILLED_LORA_STR_P2)[0]
            else:
                model_p1 = LoraLoader().load_lora(model, None, lora_name, DISTILLED_LORA_STR_P1, 0)[0]
                model_p2 = LoraLoader().load_lora(model, None, lora_name, DISTILLED_LORA_STR_P2, 0)[0]
            ok(f"Distilled LoRA loaded: P1={DISTILLED_LORA_STR_P1}× / P2={DISTILLED_LORA_STR_P2}×")
        else:
            model_p1 = model_p2 = model
            info("LoraLoaderModelOnly not found, skipping distilled LoRA")
    else:
        model_p1 = model_p2 = model
        info(f"Distilled LoRA not found at {dist_lora_path}, skipping")

    # ── Phase 1: generate + collect masks ─────────────────────────────────
    section("Phase 1: FreeFuse generation + mask collection")
    p1_latent, masks_data = run_phase1(
        model_p1, positive, negative, latent, positions, managers
    )

    # ── Apply masks ───────────────────────────────────────────────────────
    section("Applying spatial masks to LoRA hooks")
    model_p2 = apply_masks(model_p2, managers, masks_data)

    # ── Phase 2: upscale + refine ─────────────────────────────────────────
    section("Phase 2: upscale + refinement")
    p2_video = run_phase2(model_p2, positive, negative, p1_latent, video_vae)

    # ── Decode + save ─────────────────────────────────────────────────────
    section("Decoding + saving video")
    decode_and_save(p2_video, video_vae)

    section(f"DONE in {(time.time()-t_total)/60:.1f} min")
    print(f"  Video: {OUT_PATH}")
    print(f"  Masks: {MASK_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err(str(e))
        traceback.print_exc()
        sys.exit(1)
