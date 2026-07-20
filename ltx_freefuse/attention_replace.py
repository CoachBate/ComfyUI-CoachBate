"""
LTX-2.3 FreeFuse attention interception.

Uses ComfyUI's patches_replace / ("double_block", i) mechanism — identical to Flux —
to collect cross-attention similarity maps between video tokens and concept text tokens.

LTX differences vs Flux:
  - img tensor IS all video tokens: (B, T*H*W, D)  (no joint img+txt like Flux double blocks)
  - Cross-attention uses separate to_q / to_k / to_v  (not fused QKV)
  - 28 transformer blocks (LTXV), 48 (LTXAV)
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import torch
import torch.nn.functional as F

from .mask_utils import generate_masks

log = logging.getLogger("coachbate.ltx_freefuse")


@dataclass
class LTXFreeFuseState:
    # Sampling control
    phase: str = "collect"          # "collect" | "generate"
    collect_step: int = 5           # Which step to collect at (1-based)
    collect_block: int = 10         # Start block index
    collect_block_end: int = 10     # End block index (inclusive)

    # Token positions: adapter_name -> [[pos, ...], ...]  (outer = batch)
    token_pos_maps: Dict[str, List[List[int]]] = field(default_factory=dict)

    # Collected similarity maps: concept_name -> (N,) or (H*W,) float tensor
    similarity_maps: Dict[str, torch.Tensor] = field(default_factory=dict)
    # For multi-block collection: block_idx -> {concept_name: (N,)}
    block_similarity_maps: Dict[int, Dict[str, torch.Tensor]] = field(default_factory=dict)

    # Generated masks
    masks: Dict[str, torch.Tensor] = field(default_factory=dict)

    # Spatial dims (set by Phase1Sampler from the latent shape)
    latent_t: int = 1
    latent_h: int = 32
    latent_w: int = 32

    # Algorithm params
    top_k_ratio: float = 0.3
    collected: bool = False

    # Step counter per block (for step tracking without sigmas_index)
    _step_counter: Dict[int, int] = field(default_factory=dict)

    def mark_collected(self):
        self.collected = True

    def is_collect_step(self, block_index: int, transformer_options: dict) -> bool:
        if self.phase != "collect" or self.collected:
            return False

        # Use sigmas_index if ComfyUI provides it
        sigmas_index = transformer_options.get("sigmas_index", None)
        if sigmas_index is not None:
            target_block = self.collect_block
            if self.collect_block_end > self.collect_block:
                in_range = self.collect_block <= block_index <= self.collect_block_end
            else:
                in_range = block_index == target_block
            return in_range and (sigmas_index + 1) == self.collect_step

        # Fallback: count calls to the first collect block
        if block_index == self.collect_block:
            count = self._step_counter.get(block_index, 0) + 1
            self._step_counter[block_index] = count
            if count == self.collect_step:
                if self.collect_block_end > self.collect_block:
                    return True  # First block in range triggers collection for all
                return True
        elif self.collect_block < block_index <= self.collect_block_end:
            # Range mode: collect if the anchor block already fired
            return self._step_counter.get(self.collect_block, 0) == self.collect_step

        return False


class FreeFuseLTXBlockReplace:
    """
    Block replace function for a single LTX transformer block.

    Registered via model.set_model_patch_replace(fn, "dit", "double_block", i).
    Intercepts the forward pass at collect_step to extract cross-attention
    similarity maps between video tokens and concept text tokens.
    """

    def __init__(self, state: LTXFreeFuseState, block, block_index: int):
        self.state = state
        self.block = block
        self.block_index = block_index

    def create_block_replace(self) -> Callable:
        state = self.state
        block = self.block
        block_index = self.block_index

        def block_replace(args: dict, extra_args: dict) -> dict:
            # LTXV:  args["img"] = tensor (B, T*H*W, D),  args["txt"] = text context
            # LTXAV: args["img"] = (vx, ax) tuple,        args["v_context"] = video text context
            img_raw = args["img"]
            if isinstance(img_raw, tuple):
                img = img_raw[0]                        # video tokens only
                context = args.get("v_context")
            else:
                img = img_raw
                context = args.get("txt")

            if context is None:
                # No text context available — pass through unchanged
                return extra_args["original_block"](args)

            transformer_options = args.get("transformer_options", {})

            if not state.is_collect_step(block_index, transformer_options):
                return extra_args["original_block"](args)

            # ── Collect cross-attention similarity maps ──────────────────────
            try:
                _collect_similarity_maps(state, block, block_index, img, context)
            except Exception as exc:
                log.warning("[LTXFreeFuse] Similarity collection failed at block %d: %s", block_index, exc)

            result = extra_args["original_block"](args)

            # If this is the last block in the collection range, mark as done
            last_block = (
                block_index == state.collect_block_end
                if state.collect_block_end > state.collect_block
                else block_index == state.collect_block
            )
            if last_block and not state.collected:
                state.mark_collected()

            return result

        return block_replace


def _collect_similarity_maps(
    state: LTXFreeFuseState,
    block,
    block_index: int,
    img: torch.Tensor,
    context: torch.Tensor,
):
    """
    Compute per-concept similarity maps from the block's cross-attention (attn2).

    img:     (B, N, D)  where N = T * H * W video tokens
    context: (B, T_text, D) text tokens

    Stores (N,) float tensors in state.similarity_maps[concept_name].
    For multi-block range mode, also stores in state.block_similarity_maps.
    """
    attn2 = block.attn2
    heads = attn2.heads
    # LTX versions differ: some use dim_head, others head_dim
    d_head = getattr(attn2, "dim_head", None) or getattr(attn2, "head_dim", None)
    if d_head is None:
        raise AttributeError(f"[LTXFreeFuse] attn2 has neither dim_head nor head_dim (type={type(attn2).__name__})")

    B, N, D = img.shape

    # Project video tokens → Q, text tokens → K
    q = attn2.to_q(img)         # (B, N, heads*d_head)
    k = attn2.to_k(context)     # (B, T_text, heads*d_head)

    # Apply norms if present (some LTX variants may add them)
    if hasattr(attn2, "q_norm"):
        q = attn2.q_norm(q)
    if hasattr(attn2, "k_norm"):
        k = attn2.k_norm(k)

    # Reshape to multi-head: (B, heads, seq, d_head)
    q = q.view(B, N, heads, d_head).transpose(1, 2)
    T_text = context.shape[1]
    k = k.view(B, T_text, heads, d_head).transpose(1, 2)

    # Cross-attention scores: (B, heads, N, T_text)
    scale = math.sqrt(d_head)
    scores = torch.matmul(q, k.transpose(-2, -1)) / scale
    scores = torch.softmax(scores, dim=-1)

    # Use only the positive-conditioning batch element (last = cond in ComfyUI CFG convention).
    # Averaging over positive + negative dilutes character signal by ~50 %.
    scores_pos = scores[-1]  # (heads, N, T_text)

    # Average over heads → (N, T_text)
    scores_mean = scores_pos.mean(dim=0)  # (N, T_text)

    block_maps: Dict[str, torch.Tensor] = {}
    for concept_name, positions_per_prompt in state.token_pos_maps.items():
        # Use first prompt's positions (batch=0)
        positions = positions_per_prompt[0] if positions_per_prompt else []
        if not positions:
            continue

        # Clamp positions to valid range
        positions = [p for p in positions if p < T_text]
        if not positions:
            continue

        pos_tensor = torch.tensor(positions, dtype=torch.long, device=img.device)
        # MEAN (not sum) over concept token positions so multi-piece names like
        # "Jordyn" → ['▁Jord', 'yn'] are treated fairly vs single-token names.
        concept_attn = scores_mean[:, pos_tensor].mean(dim=-1)  # (N,)
        block_maps[concept_name] = concept_attn.cpu()

        log.debug(
            "[LTXFreeFuse] block %d '%s' positions=%s  attn range=[%.4f, %.4f]",
            block_index, concept_name, positions,
            concept_attn.min().item(), concept_attn.max().item(),
        )

    if state.collect_block_end > state.collect_block:
        # Range mode: store per-block, aggregate later
        state.block_similarity_maps[block_index] = block_maps
    else:
        # Single block: write directly
        for name, m in block_maps.items():
            prev = state.similarity_maps.get(name)
            if prev is None:
                state.similarity_maps[name] = m
            else:
                state.similarity_maps[name] = (prev + m) / 2.0


def _aggregate_block_range(state: LTXFreeFuseState):
    """Average similarity maps across a collected block range."""
    if not state.block_similarity_maps:
        return
    concept_names = set()
    for maps in state.block_similarity_maps.values():
        concept_names.update(maps.keys())

    for name in concept_names:
        block_tensors = [
            state.block_similarity_maps[bi][name]
            for bi in sorted(state.block_similarity_maps)
            if name in state.block_similarity_maps[bi]
        ]
        if block_tensors:
            state.similarity_maps[name] = torch.stack(block_tensors).mean(dim=0)


def compute_masks_from_state(
    state: LTXFreeFuseState,
    latent_h: int,
    latent_w: int,
    mask_mode: str = "spatial",
    **mask_kwargs,
) -> Dict[str, torch.Tensor]:
    """
    Convert collected similarity maps into (H, W) masks.

    Averages over the temporal dimension T before computing spatial masks,
    so the masks are frame-agnostic spatial regions.
    """
    if state.collect_block_end > state.collect_block:
        _aggregate_block_range(state)

    if not state.similarity_maps:
        log.warning("[LTXFreeFuse] No similarity maps collected — returning empty masks")
        return {}

    T = state.latent_t
    spatial_maps: Dict[str, torch.Tensor] = {}
    for name, sim in state.similarity_maps.items():
        log.info("[LTXFreeFuse] Concept '%s': raw attn range [%.6f, %.6f]", name, sim.min().item(), sim.max().item())
        # sim: (T*H*W,) or (H*W,) depending on latent dims
        N = sim.shape[0]
        H_W = latent_h * latent_w

        if N == T * H_W and T > 1:
            # Temporal dimension present — average over frames
            sim_thw = sim.view(T, H_W).mean(dim=0)  # (H*W,)
        elif N == H_W:
            sim_thw = sim
        else:
            # Unexpected size — try to reshape gracefully
            log.warning(
                "[LTXFreeFuse] Concept '%s': sim size %d != T*H*W (%d*%d*%d=%d) or H*W (%d). Truncating.",
                name, N, T, latent_h, latent_w, T * H_W, H_W,
            )
            sim_thw = sim[:H_W] if N >= H_W else F.interpolate(
                sim.view(1, 1, -1), size=H_W, mode="linear", align_corners=False
            ).squeeze()

        spatial_maps[name] = sim_thw

    log.info("[LTXFreeFuse] Generating masks with mode='%s'", mask_mode)
    masks = generate_masks(spatial_maps, latent_h=latent_h, latent_w=latent_w,
                           mask_mode=mask_mode, **mask_kwargs)
    state.masks = masks
    return masks


def apply_ltx_patches(model, state: LTXFreeFuseState):
    """
    Register block replace patches for all blocks in [collect_block, collect_block_end].
    Must be called on a cloned model before Phase 1 sampling.
    """
    dm = getattr(model.model, "diffusion_model", None)
    if dm is None:
        raise RuntimeError("[LTXFreeFuse] Could not access diffusion_model on model")

    transformer_blocks = getattr(dm, "transformer_blocks", None)
    if transformer_blocks is None:
        raise RuntimeError("[LTXFreeFuse] diffusion_model has no transformer_blocks attribute")

    n_blocks = len(transformer_blocks)
    start = max(0, state.collect_block)
    end = min(n_blocks - 1, state.collect_block_end)

    for i in range(start, end + 1):
        block = transformer_blocks[i]
        replacer = FreeFuseLTXBlockReplace(state, block, i)
        model.set_model_patch_replace(replacer.create_block_replace(), "dit", "double_block", i)

    log.info("[LTXFreeFuse] Registered block replace patches for blocks %d-%d", start, end)
    return model
