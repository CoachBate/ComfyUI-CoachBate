"""
Mask generation utilities for LTX-FreeFuse.

Four mask modes:
  spatial      — hard per-pixel argmax, each pixel belongs to one character (default)
  soft         — proportional attention blend, both LoRAs contribute at every pixel
  face_priority— hard separation in the head/face region, soft blend on the body
  none         — no masking, both LoRAs active everywhere at full strength
"""

import torch
import torch.nn.functional as F


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def generate_masks(
    similarity_maps,
    latent_h,
    latent_w,
    include_background=True,
    bg_scale=0.95,
    use_morphological_cleaning=True,
    bg_threshold=None,
    mask_mode="spatial",
    **kwargs,
):
    """
    Generate per-concept spatial masks from attention similarity maps.

    Args:
        similarity_maps: Dict[name -> (H*W,) or (H, W) tensor]
        latent_h, latent_w: spatial dimensions
        mask_mode: "spatial" | "soft" | "face_priority" | "none"
        bg_scale: background suppression level (spatial/soft modes)
        bg_threshold: explicit threshold; defaults to bg_scale/2
        **kwargs: ignored (legacy compat)
    Returns:
        Dict[name -> (H, W) float mask in [0, 1]]
    """
    if not similarity_maps:
        return {}

    names = list(similarity_maps.keys())
    h, w = latent_h, latent_w
    N = h * w

    if mask_mode == "none":
        return {name: torch.ones(h, w) for name in names}

    # Flatten, resize to N, and per-character min-max normalize to [0, 1].
    # Gemma3 (LTX-2.3) per-token softmax weights are ~0.0002 — orders of
    # magnitude smaller than a fixed background constant — so normalizing
    # before competition is essential.
    normalized = []
    for name in names:
        m = similarity_maps[name]
        if m.dim() == 2:
            m = m.reshape(-1)
        m = m.float()
        if m.shape[0] != N:
            m = F.interpolate(
                m.view(1, 1, -1), size=N, mode="linear", align_corners=False
            ).squeeze()
        m_min, m_max = m.min(), m.max()
        normalized.append(
            (m - m_min) / (m_max - m_min + 1e-8) if m_max > m_min else torch.zeros_like(m)
        )

    device = normalized[0].device

    if mask_mode == "soft":
        return _masks_soft(names, normalized, h, w, N, include_background, bg_scale, device)
    elif mask_mode == "face_priority":
        return _masks_face_priority(names, normalized, h, w, N, use_morphological_cleaning, device)
    else:  # "spatial"
        if bg_threshold is None:
            bg_threshold = bg_scale / 2 if include_background else 0.0
        return _masks_spatial(names, normalized, h, w, N, include_background, bg_threshold,
                               use_morphological_cleaning, device)


# ──────────────────────────────────────────────────────────────────────────────
# Mode implementations
# ──────────────────────────────────────────────────────────────────────────────

def _masks_spatial(names, normalized, h, w, N, include_background, bg_threshold,
                   use_morphological_cleaning, device):
    """
    Hard per-pixel argmax.  Each pixel assigned to the character with the
    highest normalized attention.  Pixels below bg_threshold go to background
    (zero contribution from all LoRAs).
    """
    char_stacked = torch.stack(normalized, dim=0)       # (C, N)
    char_winner  = char_stacked.argmax(dim=0)           # (N,)
    char_max_val = char_stacked.max(dim=0).values       # (N,)

    is_bg = (char_max_val < bg_threshold) if include_background \
            else torch.zeros(N, dtype=torch.bool, device=device)

    masks_out = {}
    for idx, name in enumerate(names):
        mask_flat = ((char_winner == idx) & ~is_bg).float()
        mask_2d   = mask_flat.view(h, w)
        if use_morphological_cleaning:
            mask_2d = _morphological_clean(mask_2d, h, w)
        masks_out[name] = mask_2d
    return masks_out


def _masks_soft(names, normalized, h, w, N, include_background, bg_scale, device):
    """
    Proportional attention blend.  Each character's mask value at a pixel is
    its normalized attention divided by the sum across all characters (plus an
    optional background weight).  Both LoRAs contribute everywhere; the split
    is continuous rather than binary.
    """
    char_stacked = torch.stack(normalized, dim=0)       # (C, N)

    if include_background:
        bg    = torch.full((N,), bg_scale / 2, device=device, dtype=torch.float32)
        total = char_stacked.sum(dim=0) + bg
    else:
        total = char_stacked.sum(dim=0)
    total = total.clamp(min=1e-8)

    return {name: (char_stacked[idx] / total).view(h, w)
            for idx, name in enumerate(names)}


def _masks_face_priority(names, normalized, h, w, N, use_morphological_cleaning, device):
    """
    Hard separation in the face/head region; soft proportional blend on the body.

    Algorithm:
      1. Compute the attention-weighted centroid (cy, cx) for each character.
      2. Estimate face location as slightly above the centroid (face_y = cy - 0.15*H).
      3. Build a Gaussian spatial prior G_i centred on (face_y_i, cx_i).
      4. face_zone = max_i(G_i) — scalar field in [0, 1] indicating "how much
         this pixel is in someone's face region".
      5. Final mask = face_zone * hard_face_mask + (1-face_zone) * soft_body_mask.
         The transition is smooth — no hard face/body boundary.
    """
    rows = torch.arange(h, dtype=torch.float32, device=device)
    cols = torch.arange(w, dtype=torch.float32, device=device)
    grid_y, grid_x = torch.meshgrid(rows, cols, indexing="ij")
    grid_y = grid_y.reshape(-1)   # (N,) row index of each pixel
    grid_x = grid_x.reshape(-1)   # (N,) col index of each pixel

    # Gaussian sigma: ~25% of frame height, ~20% of frame width
    sy = max(h * 0.25, 1.0)
    sx = max(w * 0.20, 1.0)

    gaussians = []
    for attn in normalized:
        mass = attn.sum() + 1e-8
        cy   = (attn * grid_y).sum() / mass
        cx   = (attn * grid_x).sum() / mass
        # Face is roughly 15% of frame height above the attention centroid
        face_y = (cy - h * 0.15).clamp(0.0, h - 1.0)
        dist_sq = ((grid_y - face_y) / sy) ** 2 + ((grid_x - cx) / sx) ** 2
        gaussians.append(torch.exp(-0.5 * dist_sq))

    gauss_stack = torch.stack(gaussians, dim=0)         # (C, N)
    face_zone   = gauss_stack.max(dim=0).values         # (N,) strength of face region

    # Hard assignment in face region: argmax of Gaussian-weighted attention
    face_weighted = torch.stack(
        [normalized[i] * gaussians[i] for i in range(len(normalized))], dim=0
    )
    face_winner = face_weighted.argmax(dim=0)           # (N,)

    # Soft assignment in body region: proportional attention blend
    char_stacked = torch.stack(normalized, dim=0)       # (C, N)
    char_total   = char_stacked.sum(dim=0).clamp(min=1e-8)

    masks_out = {}
    for idx, name in enumerate(names):
        face_mask = (face_winner == idx).float()
        soft_mask = char_stacked[idx] / char_total
        mask_flat = face_zone * face_mask + (1.0 - face_zone) * soft_mask
        mask_2d   = mask_flat.view(h, w)
        if use_morphological_cleaning:
            mask_2d = _morphological_clean(mask_2d, h, w)
        masks_out[name] = mask_2d
    return masks_out


# ──────────────────────────────────────────────────────────────────────────────
# Morphological post-processing
# ──────────────────────────────────────────────────────────────────────────────

def _morphological_clean(mask_2d, h, w, kernel_size=3):
    """Opening then closing on a (H, W) float mask to remove noise and fill gaps."""
    m = mask_2d.unsqueeze(0).unsqueeze(0)   # (1, 1, H, W)
    p = kernel_size // 2

    def dilate(x):
        return F.max_pool2d(x, kernel_size=kernel_size, stride=1, padding=p)

    def erode(x):
        return 1.0 - F.max_pool2d(1.0 - x, kernel_size=kernel_size, stride=1, padding=p)

    m = dilate(erode(m))    # opening  — removes small isolated blobs
    m = erode(dilate(m))    # closing  — fills small holes
    return m.squeeze(0).squeeze(0)


# ──────────────────────────────────────────────────────────────────────────────
# Legacy / reference (not called by generate_masks)
# ──────────────────────────────────────────────────────────────────────────────

def linear_normalize(x, dim=1):
    x_min = x.min(dim=dim, keepdim=True)[0]
    x_max = x.max(dim=dim, keepdim=True)[0]
    return (x - x_min) / (x_max - x_min + 1e-8)
