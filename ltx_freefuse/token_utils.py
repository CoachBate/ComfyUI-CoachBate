"""
Token position utilities for LTX-2.3 (Gemma3 12B encoder).
"""

import logging
from typing import Dict, List, Union

log = logging.getLogger("coachbate.ltx_freefuse")

# Tokens that carry no spatial meaning and should be excluded from concept positions
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "which", "who", "where", "when",
}
_PUNCT = set(",.:;!?\"'()[]{}-–—/\\")


def _clean(token_text: str) -> str:
    return token_text.replace("▁", "").replace("</w>", "").replace("Ġ", "").strip().lower()


def _is_meaningless(token_text: str) -> bool:
    c = _clean(token_text)
    return not c or len(c) == 1 or c in _STOPWORDS or c in _PUNCT


def detect_ltx_model(model) -> bool:
    """Return True if the ComfyUI model patcher wraps an LTX-Video model."""
    core = getattr(model, "model", model)
    cls = core.__class__.__name__.lower()
    if "ltx" in cls:
        return True
    dm = getattr(core, "diffusion_model", None)
    if dm is not None:
        dm_cls = dm.__class__.__name__.lower()
        if "ltx" in dm_cls:
            return True
        # LTX uses transformer_blocks but NOT double_blocks (Flux marker)
        if hasattr(dm, "transformer_blocks") and not hasattr(dm, "double_blocks"):
            return True
    cfg = getattr(core, "model_config", None)
    unet_cfg = getattr(cfg, "unet_config", {}) if cfg else {}
    if isinstance(unet_cfg, dict) and "ltx" in str(unet_cfg.get("image_model", "")).lower():
        return True
    return False


def get_gemma3_tokenizer_for_ltx(clip):
    """
    Extract the raw SentencePieceProcessor from a ComfyUI CLIP object for LTX-2.3+.
    Navigates: clip.tokenizer.gemma3_12b (SPieceTokenizer) -> .tokenizer (SentencePieceProcessor)
    """
    tokenizer_wrapper = getattr(clip, "tokenizer", None)
    if tokenizer_wrapper is None:
        raise ValueError("[LTXFreeFuse] clip.tokenizer is None")

    gemma_wrapper = getattr(tokenizer_wrapper, "gemma3_12b", None)
    if gemma_wrapper is not None:
        spiece = getattr(gemma_wrapper, "tokenizer", None)  # SPieceTokenizer
        if spiece is not None:
            sp = getattr(spiece, "tokenizer", spiece)  # SentencePieceProcessor
            if sp is not None:
                return sp

    raise ValueError(
        "[LTXFreeFuse] Could not find Gemma3 tokenizer in CLIP. "
        f"Available attrs: {sorted(vars(tokenizer_wrapper).keys())}"
    )


def _is_special_token(piece: str) -> bool:
    """Return True for SentencePiece control/special tokens that don't appear in the source text."""
    # Standard angle-bracket specials: <bos>, <eos>, <pad>, <unk>, <mask>, etc.
    if piece.startswith("<") and piece.endswith(">"):
        return True
    # Gemma / LLaMA-style special tokens: <start_of_turn>, <end_of_turn>, etc.
    if piece.startswith("<") and ">" in piece:
        return True
    return False


def _sp_offsets(text: str, pieces: list) -> list:
    """
    Reconstruct (char_start, char_end) for each SentencePiece piece against the original text.
    The ▁ prefix marks a word boundary (preceding space); it doesn't appear in the source text.
    Special/zero-length tokens get a zero-width position at the current cursor.
    """
    offsets = []
    pos = 0
    for piece in pieces:
        # Special tokens (<bos>, <eos>, etc.) don't correspond to source characters.
        # Give them a zero-length anchor so they don't consume characters from the text
        # and corrupt every subsequent token's offset.
        if _is_special_token(piece):
            offsets.append((pos, pos))
            continue
        clean = piece.lstrip("▁")
        if not clean:
            offsets.append((pos, pos))
            continue
        if piece != clean:  # had ▁ prefix — skip any whitespace in source
            while pos < len(text) and text[pos] == " ":
                pos += 1
        start = pos
        pos = min(pos + len(clean), len(text))
        offsets.append((start, pos))
    return offsets


def find_concept_positions_gemma3(
    tokenizer,
    prompt: Union[str, List[str]],
    concepts: Dict[str, str],
    filter_meaningless: bool = True,
) -> Dict[str, List[List[int]]]:
    """
    Find token positions for each concept using SentencePiece encode_as_pieces
    and manually reconstructed character offsets (Gemma3 / LTX-2.3+).

    Args:
        tokenizer: SentencePieceProcessor (from clip.tokenizer.gemma3_12b.tokenizer.tokenizer)
        prompt: Single prompt string or list of prompts (one per batch element)
        concepts: Dict[adapter_name -> concept_text]

    Returns:
        Dict[adapter_name -> list-of-position-lists (one per prompt)]
    """
    if isinstance(prompt, str):
        prompts = [prompt]
    else:
        prompts = list(prompt)

    prompt_data = []
    for p in prompts:
        pieces = tokenizer.encode_as_pieces(p)
        offsets = _sp_offsets(p, pieces)
        prompt_data.append({"text": p, "pieces": pieces, "offsets": offsets})

    result: Dict[str, List[List[int]]] = {}
    for name, concept_text in concepts.items():
        positions_per_prompt = []
        for pd in prompt_data:
            text = pd["text"]
            pieces = pd["pieces"]
            offsets = pd["offsets"]

            found: List[int] = []
            search_start = 0
            while True:
                char_start = text.find(concept_text, search_start)
                if char_start == -1:
                    break
                char_end = char_start + len(concept_text)

                for tok_idx, (tok_start, tok_end) in enumerate(offsets):
                    if tok_end > char_start and tok_start < char_end:
                        if tok_idx not in found:
                            if filter_meaningless:
                                if _is_meaningless(_clean(pieces[tok_idx])):
                                    continue
                            found.append(tok_idx)

                search_start = char_start + 1

            if not found:
                log.warning(
                    "[LTXFreeFuse] Concept '%s' ('%s') not found in prompt: %.80s",
                    name, concept_text, text
                )
            positions_per_prompt.append(found)

        result[name] = positions_per_prompt

    return result
