# cost.py — turns (model, input_tokens, output_tokens) into USD. every
# LLM call in the pipeline runs its tokens through this so disk artifacts
# and live logs use the same arithmetic. nothing else owns price math.

from __future__ import annotations

# dataclasses: stdlib. CostBreakdown is frozen so it can't drift after
# being attached to an LLM reply object.
from dataclasses import dataclass

# ModelSpec carries price_in / price_out in USD per 1M tokens. it is the
# only place those numbers live at runtime; they originate in
# pipeline/config.yaml and are loaded by config.py.
from .config import ModelSpec


@dataclass(frozen=True)
class CostBreakdown:
    input_tokens: int
    output_tokens: int
    input_usd: float
    output_usd: float
    total_usd: float


def usd(model: ModelSpec, input_tokens: int, output_tokens: int) -> CostBreakdown:
    # prices in config are USD per 1,000,000 tokens, so we divide.
    # input and output stay separated because some providers price them
    # very differently (e.g. Opus 5/25, DeepSeek 0.26/0.38) and the split
    # tells us where cost actually comes from per stage.
    in_usd = (input_tokens / 1_000_000.0) * model.price_in
    out_usd = (output_tokens / 1_000_000.0) * model.price_out
    return CostBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_usd=round(in_usd, 6),
        output_usd=round(out_usd, 6),
        total_usd=round(in_usd + out_usd, 6),
    )
