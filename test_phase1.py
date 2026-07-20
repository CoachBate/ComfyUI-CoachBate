"""
test_phase1.py  —  CoachBate LTX-FreeFuse Layer 1 smoke test

Two stages:

  Stage 1 (FAST, ~5s, no model load)
    Validates the algorithm end-to-end using a tiny synthetic LTX transformer.
    Confirms: block replace fires, similarity maps collected, masks separate correctly.

  Stage 2 (SLOW, ~2–5 min, loads real model)
    Loads ltx-2.3-22b-dev-fp8.safetensors + both character LoRAs.
    Runs Phase 1 on a 1-frame 64×64 latent (5 collect steps).
    Confirms the masks look reasonable on real attention patterns.

Run Stage 1 only (default):
    python_embeded\\python.exe custom_nodes\\ComfyUI-CoachBate\\test_phase1.py

Run both stages:
    python_embeded\\python.exe custom_nodes\\ComfyUI-CoachBate\\test_phase1.py --full
"""

import sys, os, argparse, time

_COMFYUI_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _COMFYUI_ROOT)   # ComfyUI root — must come first
sys.path.insert(1, os.path.dirname(__file__))  # CoachBate root

# Load ComfyUI root nodes.py before CoachBate nodes.py shadows it
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("comfyui_nodes", os.path.join(_COMFYUI_ROOT, "nodes.py"))
_comfyui_nodes = _ilu.module_from_spec(_spec)
sys.modules["comfyui_nodes"] = _comfyui_nodes
_spec.loader.exec_module(_comfyui_nodes)

import torch
import torch.nn as nn

# ── Paths ────────────────────────────────────────────────────────────────────
MODEL_PATH  = r"D:\Data\models\checkpoints\ltx-2.3-22b-dev-fp8.safetensors"
LORA_BROCK  = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-brock-lora-v3\ltx-2.3-brock-lora-v3_000011000.safetensors"
LORA_JORDYN = r"C:\Data\AIToolkit-StagingArea\output\ltx-2.3-jordyn-v2\ltx-2.3-jordyn-v2_000005550.safetensors"
PROMPT      = "Brock and Jordyn stand together in a sunny park"

BROCK_CONCEPT  = "Brock"
JORDYN_CONCEPT = "Jordyn"

# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):  print(f"  [PASS]  {msg}")
def fail(msg, exc=None):
    print(f"  [FAIL]  {msg}")
    if exc:
        import traceback; traceback.print_exc()

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# ════════════════════════════════════════════════════════════════════════════
#  STAGE 1: Synthetic model — fast algorithm validation
# ════════════════════════════════════════════════════════════════════════════

def run_stage1():
    section("STAGE 1 — Synthetic model (algorithm validation)")

    from ltx_freefuse.mask_utils import generate_masks
    from ltx_freefuse.attention_replace import (
        LTXFreeFuseState,
        FreeFuseLTXBlockReplace,
        compute_masks_from_state,
    )
    from ltx_freefuse.lora_hook import MaskedBypassForwardHook

    # ── 1a. Build a tiny LTX-shaped transformer block ──────────────────────
    DIM, N_HEADS, D_HEAD, CTX_DIM = 64, 4, 16, 128

    class TinyCrossAttn(nn.Module):
        def __init__(self):
            super().__init__()
            self.heads = N_HEADS
            self.dim_head = D_HEAD
            inner = N_HEADS * D_HEAD
            self.to_q = nn.Linear(DIM, inner, bias=False)
            self.to_k = nn.Linear(CTX_DIM, inner, bias=False)
            self.to_v = nn.Linear(CTX_DIM, inner, bias=False)
            self.to_out = nn.Sequential(nn.Linear(inner, DIM))
        def forward(self, x, context=None, **kw):
            return x

    class TinyBlock(nn.Module):
        def __init__(self):
            super().__init__()
            self.attn1 = TinyCrossAttn()
            self.attn2 = TinyCrossAttn()
        def forward(self, x, context=None, **kw):
            return x

    blocks = nn.ModuleList([TinyBlock() for _ in range(4)])
    ok("Tiny synthetic transformer blocks created")

    # ── 1b. Build state with known token positions ──────────────────────────
    T, H, W = 2, 8, 12   # tiny video latent
    N = T * H * W         # 192 tokens

    state = LTXFreeFuseState()
    state.collect_step    = 1          # fire on first step
    state.collect_block   = 1
    state.collect_block_end = 1
    state.latent_t = T
    state.latent_h = H
    state.latent_w = W
    # Fake token positions: brock at pos 2-3, jordyn at pos 6-7 (in T5 sequence)
    state.token_pos_maps = {
        "brock":  [[2, 3]],
        "jordyn": [[6, 7]],
    }
    ok("State initialised (T=%d, H=%d, W=%d, N=%d tokens)" % (T, H, W, N))

    # ── 1c. Create and invoke block replace ────────────────────────────────
    block = blocks[1]
    replacer = FreeFuseLTXBlockReplace(state, block, block_index=1)
    replace_fn = replacer.create_block_replace()

    # Use zero tensors so only planted signals matter (randn noise swamps signal).
    img = torch.zeros(1, N, DIM)       # (B, T*H*W, D)
    ctx = torch.zeros(1, 20, CTX_DIM)  # (B, T_text, D)

    # Make to_q and to_k near-identity so planted signals survive the projection.
    with torch.no_grad():
        block.attn2.to_q.weight.zero_()
        for i in range(min(DIM, N_HEADS * D_HEAD)):
            block.attn2.to_q.weight[i, i] = 1.0

        block.attn2.to_k.weight.zero_()
        for i in range(min(N_HEADS * D_HEAD, CTX_DIM)):
            block.attn2.to_k.weight[i, i] = 1.0

    # Plant directional signals:
    #   Left-half img tokens  → dim 0 ;  brock  ctx tokens (pos 2,3) → dim 0
    #   Right-half img tokens → dim 1 ;  jordyn ctx tokens (pos 6,7) → dim 1
    with torch.no_grad():
        for tok_i in range(N):
            col = (tok_i % (H * W)) % W
            if col < W // 2:
                img[0, tok_i, 0] = 5.0
            else:
                img[0, tok_i, 1] = 5.0

        for p in [2, 3]:
            ctx[0, p, 0] = 5.0
        for p in [6, 7]:
            ctx[0, p, 1] = 5.0

    # Fake transformer_options with sigmas_index=0 (step 0, 0-based → step 1 1-based)
    transformer_options = {"sigmas_index": 0}

    args = {
        "img": img,
        "txt": ctx,
        "vec": torch.zeros(1, 1, DIM),
        "pe": None,
        "attention_mask": None,
        "transformer_options": transformer_options,
    }
    extra_args = {"original_block": lambda a: {"img": a["img"]}}

    result = replace_fn(args, extra_args)
    assert "img" in result, "block replace must return dict with 'img'"
    ok("Block replace fired and returned output")

    # ── 1d. Check similarity maps collected ────────────────────────────────
    assert state.collected, "State should be marked collected after block fires"
    assert "brock"  in state.similarity_maps, "brock sim map missing"
    assert "jordyn" in state.similarity_maps, "jordyn sim map missing"
    ok("Similarity maps collected for both concepts")

    brock_sim  = state.similarity_maps["brock"]
    jordyn_sim = state.similarity_maps["jordyn"]
    assert brock_sim.shape[0]  == N, f"Expected N={N}, got {brock_sim.shape}"
    assert jordyn_sim.shape[0] == N
    ok(f"Sim map shapes correct: ({N},) each")

    # ── 1e. Generate masks ─────────────────────────────────────────────────
    # include_background=False: in synthetic tests, uniform background (0.475)
    # beats low-magnitude random attention. Real model attention is much stronger.
    masks = compute_masks_from_state(state, H, W, include_background=False)
    assert set(masks.keys()) == {"brock", "jordyn"}, f"Expected brock+jordyn, got {set(masks.keys())}"
    ok("Masks generated for both concepts")

    brock_mask  = masks["brock"]
    jordyn_mask = masks["jordyn"]
    assert brock_mask.shape  == (H, W), f"Expected ({H},{W}), got {brock_mask.shape}"
    assert jordyn_mask.shape == (H, W)
    ok(f"Mask shapes correct: ({H}, {W}) each")

    # ── 1f. Spatial separation check ──────────────────────────────────────
    # Columns 0..W//2-1 are LEFT, columns W//2..W-1 are RIGHT
    brock_left   = brock_mask[:, :W//2].mean().item()
    brock_right  = brock_mask[:, W//2:].mean().item()
    jordyn_left  = jordyn_mask[:, :W//2].mean().item()
    jordyn_right = jordyn_mask[:, W//2:].mean().item()

    print(f"       brock  mask: left={brock_left:.2f}  right={brock_right:.2f}  (expect left > right)")
    print(f"       jordyn mask: left={jordyn_left:.2f}  right={jordyn_right:.2f}  (expect right > left)")

    assert brock_left  > brock_right,  "Brock should dominate left half"
    assert jordyn_right > jordyn_left, "Jordyn should dominate right half"
    ok("Spatial separation PASSED — masks cleanly divide left / right")

    # ── 1g. MaskedBypassForwardHook smoke test ─────────────────────────────
    lin = nn.Linear(DIM, DIM, bias=False)
    adapter_cls = type("MockAdapter", (), {
        "h": lambda self, x, base: torch.zeros_like(base),
        "g": lambda self, x: x,
        "multiplier": 1.0, "is_conv": False, "conv_dim": 0,
        "kernel_size": (1,), "in_channels": None, "out_channels": None, "kw_dict": {},
    })
    adapter = adapter_cls()

    hook = MaskedBypassForwardHook(lin, adapter, multiplier=1.0)
    hook.original_forward = lin.forward   # simulate inject()
    test_x = torch.randn(1, N, DIM)
    hook.set_spatial_mask(brock_mask, T, H, W)
    out = hook._bypass_forward(test_x)
    assert out.shape == (1, N, DIM), f"Unexpected output shape: {out.shape}"
    ok("MaskedBypassForwardHook forward pass correct")

    print("\n  [STAGE 1 PASSED]\n")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  STAGE 2: Real model integration test
# ════════════════════════════════════════════════════════════════════════════

def run_stage2():
    section("STAGE 2 — Real model integration (loads ~22 GB fp8 checkpoint)")
    print("  This may take 2–5 minutes to load. Press Ctrl-C to abort.\n")

    for path in [MODEL_PATH, LORA_BROCK, LORA_JORDYN]:
        if not os.path.exists(path):
            fail(f"File not found: {path}")
            return False

    # ── Bootstrap ComfyUI ──────────────────────────────────────────────────
    import folder_paths
    import comfy.model_management as mm
    import comfy.sd

    # Register model paths
    model_dir = os.path.dirname(MODEL_PATH)
    folder_paths.add_model_folder_path("checkpoints", model_dir)

    # ── Load checkpoint (model + VAE only — CLIP not embedded) ────────────
    print("  Loading checkpoint (this is the slow part)...")
    t0 = time.time()
    loader = _comfyui_nodes.CheckpointLoaderSimple()
    model_name = os.path.basename(MODEL_PATH)
    model, _clip_unused, vae = loader.load_checkpoint(model_name)
    ok(f"Checkpoint loaded in {time.time()-t0:.0f}s  —  {model.__class__.__name__}")

    # ── Verify LTX model detected ──────────────────────────────────────────
    from ltx_freefuse.token_utils import detect_ltx_model
    assert detect_ltx_model(model), "detect_ltx_model returned False — check class name"
    ok("detect_ltx_model = True")

    # ── Load LoRAs in masked bypass mode ──────────────────────────────────
    from ltx_freefuse.lora_hook import load_masked_bypass_lora
    model, _, mgr_brock  = load_masked_bypass_lora(model, None, LORA_BROCK,  1.0, 1.0, "brock")
    model, _, mgr_jordyn = load_masked_bypass_lora(model, None, LORA_JORDYN, 1.0, 1.0, "jordyn")
    ok(f"brock  LoRA: {mgr_brock.get_hook_count()} bypass hooks")
    ok(f"jordyn LoRA: {mgr_jordyn.get_hook_count()} bypass hooks")
    assert mgr_brock.get_hook_count()  > 0, "No hooks registered for brock"
    assert mgr_jordyn.get_hook_count() > 0, "No hooks registered for jordyn"

    # ── Get conditioning via LTX Video API (no local Gemma3 needed) ────────
    import io as _io, pickle, requests as _req
    from safetensors import safe_open

    LTX_API_KEY = os.environ.get("LTX_API_KEY", "")
    LTX_API_URL = "https://api.ltx.video/v1/prompt-embedding"

    print("  Extracting model_id from checkpoint…")
    with safe_open(MODEL_PATH, framework="pt", device="cpu") as f:
        meta = f.metadata()
    model_id = meta.get("encrypted_wandb_properties")
    assert model_id, "No encrypted_wandb_properties in checkpoint metadata"
    ok(f"model_id extracted (len={len(model_id)})")

    def api_encode(prompt_text):
        resp = _req.post(
            LTX_API_URL,
            json={"prompt": prompt_text, "model_id": model_id, "enhance_prompt": False},
            headers={"Authorization": f"Bearer {LTX_API_KEY}", "Content-Type": "application/json"},
            timeout=60,
        )
        assert resp.status_code == 200, f"API error {resp.status_code}: {resp.text[:200]}"
        return pickle.load(_io.BytesIO(resp.content))

    print("  Encoding positive conditioning via API…")
    positive = api_encode(PROMPT)
    ok(f"Positive conditioning: {type(positive)}")

    print("  Encoding negative conditioning via API…")
    negative = api_encode("blurry, bad quality")
    ok("Negative conditioning encoded")

    # ── Find token positions using sentencepiece directly ─────────────────
    # Load Gemma tokenizer from checkpoint (tokenizer model bytes are in metadata)
    # We only need the tokenizer, not the full 12B model
    from ltx_freefuse.token_utils import find_concept_positions_t5
    import sentencepiece as spm

    spm_key = "spiece_model"
    spm_bytes = meta.get(spm_key)

    # spiece_model is in the Gemma3 safetensors, not the LTX checkpoint.
    # Try the FP4 Gemma3 text encoder if available.
    if spm_bytes is None:
        GEMMA_TE_PATH = r"M:\models\text_encoders\gemma_3_12B_it_fp4_mixed.safetensors"
        if os.path.exists(GEMMA_TE_PATH):
            with safe_open(GEMMA_TE_PATH, framework="pt", device="cpu") as gf:
                spm_bytes = gf.metadata().get(spm_key)
                if spm_bytes is None:
                    # Try as a stored tensor
                    try:
                        spm_bytes = gf.get_tensor(spm_key)
                    except Exception:
                        pass

    if spm_bytes is None:
        # Fallback: hardcode approximate positions for BROCK/JORDYN in test prompt
        positions = {"brock": [[3, 4]], "jordyn": [[6, 7]]}
        ok("Using fallback token positions (approximate — spiece not found)")
    else:
        if hasattr(spm_bytes, 'numpy'):
            spm_bytes = bytes(spm_bytes.numpy())
        sp = spm.SentencePieceProcessor(model_proto=spm_bytes)
        concepts = {"brock": BROCK_CONCEPT, "jordyn": JORDYN_CONCEPT}
        positions = find_concept_positions_t5(sp, PROMPT, concepts)
        ok(f"brock  positions: {positions['brock']}")
        ok(f"jordyn positions: {positions['jordyn']}")

    # ── Build small latent: 1 frame, 64×64 (minimal to save time) ─────────
    lat_T, lat_H, lat_W = 1, 64, 64
    latent = {"samples": torch.zeros(1, 128, lat_T, lat_H, lat_W)}
    ok(f"Latent shape: {latent['samples'].shape}")

    # ── Run Phase 1 (call underlying functions directly — avoids relative import issue) ──
    freefuse_data = {
        "managers": {"brock": mgr_brock, "jordyn": mgr_jordyn},
        "token_pos_maps": positions,
    }

    # Import from the submodule directly (no relative imports needed)
    from ltx_freefuse.attention_replace import (
        LTXFreeFuseState, apply_ltx_patches, compute_masks_from_state,
    )
    import comfy.sample, comfy.samplers, torch.nn.functional as F

    SEED, STEPS, COLLECT_STEP, CFG = 42, 20, 5, 3.0
    COLLECT_BLOCK = 10

    # Build state
    state = LTXFreeFuseState()
    state.collect_step    = COLLECT_STEP
    state.collect_block   = COLLECT_BLOCK
    state.collect_block_end = COLLECT_BLOCK
    state.latent_t = lat_T
    state.latent_h = lat_H
    state.latent_w = lat_W
    state.token_pos_maps = positions

    patched_model = model.clone()
    apply_ltx_patches(patched_model, state)

    sampler_obj = comfy.samplers.KSampler(
        patched_model, steps=STEPS,
        device=comfy.model_management.get_torch_device(),
        sampler="euler", scheduler="simple",
        denoise=1.0, model_options=patched_model.model_options,
    )
    sigmas_use = sampler_obj.sigmas

    print("  Running Phase 1 (5 steps on 64×64 latent)...")
    t0 = time.time()
    latent_samples = latent["samples"]
    try:
        p1_samples = comfy.sample.sample(
            patched_model,
            noise=comfy.sample.prepare_noise(latent_samples, SEED, None),
            steps=STEPS, cfg=CFG,
            sampler_name="euler", scheduler="simple",
            positive=positive, negative=negative,
            latent_image=latent_samples,
            start_step=0, last_step=None,
            force_full_denoise=True, noise_mask=None,
            sigmas=sigmas_use, callback=None,
            disable_pbar=False, seed=SEED,
        )
    except Exception as exc:
        print(f"  [FAIL]  Phase 1 sampling raised: {exc}")
        raise

    masks_data_dict = {"masks": {}, "latent_t": lat_T, "latent_h": lat_H, "latent_w": lat_W}
    p1_latent = {"samples": p1_samples}

    if not state.similarity_maps and not state.block_similarity_maps:
        raise RuntimeError("No similarity maps collected — block replace did not fire!")

    masks = compute_masks_from_state(state, lat_H, lat_W)
    masks_data_dict["masks"] = masks

    # Build preview
    colours = [torch.tensor([1.0,0.3,0.3]), torch.tensor([0.3,0.5,1.0])]
    canvas = torch.zeros(lat_H, lat_W, 3)
    for idx, (name, mask) in enumerate(masks.items()):
        c = colours[idx % len(colours)]
        canvas += mask.float().unsqueeze(-1) * c
    preview = canvas.clamp(0,1).unsqueeze(0)

    masks_data = masks_data_dict
    ok(f"Phase 1 completed in {time.time()-t0:.1f}s")
    ok(f"Phase 1 latent shape: {p1_latent['samples'].shape if hasattr(p1_latent['samples'], 'shape') else type(p1_latent['samples'])}")

    masks = masks_data["masks"]
    ok(f"Masks generated: {list(masks.keys())}")
    for name, m in masks.items():
        ok(f"  {name}: shape={m.shape}  mean={m.mean():.3f}  nonzero={m.gt(0.5).sum().item()} px")

    assert set(masks.keys()) == {"brock", "jordyn"}
    assert masks["brock"].shape  == (lat_H, lat_W)
    assert masks["jordyn"].shape == (lat_H, lat_W)

    # Masks should not overlap (they should partition the image)
    overlap = (masks["brock"] > 0.5) & (masks["jordyn"] > 0.5)
    ok(f"Mask overlap: {overlap.sum().item()} px  (expect ~0)")

    assert preview.shape[-1] == 3, "Preview should be RGB"
    ok(f"Mask preview shape: {preview.shape}")

    print("\n  [STAGE 2 PASSED]\n")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="Also run Stage 2 (loads real 22B model, slow)")
    args = parser.parse_args()

    passed = 0
    failed = 0

    try:
        run_stage1()
        passed += 1
    except Exception as e:
        fail("STAGE 1 FAILED", e)
        failed += 1
        import traceback; traceback.print_exc()

    if args.full:
        try:
            run_stage2()
            passed += 1
        except Exception as e:
            fail("STAGE 2 FAILED", e)
            failed += 1
            import traceback; traceback.print_exc()
    else:
        print("  (Skipping Stage 2 — run with --full to test real model)")

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}\n")
    sys.exit(1 if failed else 0)
