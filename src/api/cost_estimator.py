"""Pre-request cost estimation using tiktoken (cl100k_base).

Tiktoken provides exact token counts matching the provider's BPE encoding.
The encoding is loaded at module level; the tokenizer data is downloaded
once at build time (see Dockerfile) and cached to a persistent volume at
runtime (TIKTOKEN_CACHE_DIR), so there is no per-request overhead.
"""

from typing import Optional

import tiktoken

from .logging_config import get_logger

logger = get_logger("lcp.cost_estimator")


# Approximate token pricing per 1M tokens (fallback if config unavailable)
# Keys match the gateway config pricing convention: cache_miss = input, output = output
_DEFAULT_PRICING = {
    "deepseek-v4-pro": {"cache_miss": 0.66, "output": 1.98},
    "deepseek-v4-flash": {"cache_miss": 0.14, "output": 0.28},
}

# Encodings — deepseek models use cl100k_base (same as GPT-4).
# Pre-downloaded at build time, persisted via TIKTOKEN_CACHE_DIR volume.
_ENCODING = tiktoken.get_encoding("cl100k_base")


def _count_text(text: str) -> int:
    """Token-count *text* without ever raising on special tokens.

    tiktoken's ``encode`` defaults to ``disallowed_special='all'`` (since
    ~0.13), which raises ``ValueError`` when the text literally contains a
    special token such as ``<|endoftext|>`` (common in pasted code, model
    echoes, or tokenizer-injected text). Token counting must never crash a
    request, so special tokens are treated as ordinary text.
    """
    if not text:
        return 0
    try:
        return len(_ENCODING.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 — a count is never worth a 500
        # Approximate fallback (1 token ≈ 4 chars) if the tokenizer is unhappy.
        return max(len(text) // 4, 1)


def count_tokens(messages: list[dict], tools: Optional[list[dict]] = None) -> int:
    """Count tokens using the cl100k_base BPE encoding.

    Matches the tokenizer used by DeepSeek/OpenAI models. Real billed costs
    come from the provider's response ``usage`` block; this is the pre-request
    estimate for the X-Estimated-Cost header and routing decisions.
    """
    token_count = 0
    for msg in messages:
        token_count += 4  # approximate per-message overhead
        content = msg.get("content", "")
        if isinstance(content, str):
            token_count += _count_text(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    token_count += _count_text(block.get("text", ""))

    if tools:
        for tool in tools:
            token_count += _count_text(str(tool))

    return token_count


def estimate_cost(
    model: str,
    input_tokens: int,
    max_tokens: int = 1024,
    pricing: Optional[dict] = None,
) -> dict:
    """Estimate cost for a request.

    Returns:
        {"input_tokens": int, "estimated_output_tokens": int,
         "estimated_input_cost": float, "estimated_output_cost": float,
         "estimated_total_cost": float, "currency": "USD"}
    """
    if pricing is None:
        pricing = _DEFAULT_PRICING.get(model, {"cache_miss": 0.66, "output": 1.98})

    input_cost = (input_tokens / 1_000_000) * pricing.get("cache_miss", pricing.get("input", 0.66))
    output_cost = (max_tokens / 1_000_000) * pricing.get("output", 1.98)

    return {
        "input_tokens": input_tokens,
        "estimated_output_tokens": max_tokens,
        "estimated_input_cost": round(input_cost, 8),
        "estimated_output_cost": round(output_cost, 8),
        "estimated_total_cost": round(input_cost + output_cost, 8),
        "currency": "USD",
    }


def estimate_from_request(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    max_tokens: int = 1024,
    pricing: Optional[dict] = None,
) -> dict:
    """Full cost estimation from request parameters."""
    input_tokens = count_tokens(messages, tools)
    result = estimate_cost(model, input_tokens, max_tokens, pricing)
    logger.debug(
        "cost_estimated",
        model=model,
        input_tokens=input_tokens,
        estimated_cost=result["estimated_total_cost"],
    )
    return result
