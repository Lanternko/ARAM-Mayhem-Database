"""Tier 1: Champion Embedding + DeepSets with antisymmetry guarantee.

Architecture:
  sum_blue = Σ embed(c) for c in blue   # set invariant
  sum_red  = Σ embed(c) for c in red
  diff     = sum_blue - sum_red          # antisymmetric channel
  total    = sum_blue + sum_red          # symmetric channel

Antisymmetry enforcement (swap teams → logit flips sign):
  logit = [MLP(diff, total) − MLP(−diff, total)] / 2

This is exactly antisymmetric by construction (no special head tricks needed).
Forward pass runs twice per sample during training — cheap for 14k params.
"""
from __future__ import annotations

import torch
import torch.nn as nn


def _mlp(in_dim: int, hidden: int, out_dim: int, dropout: float) -> nn.Sequential:
    return nn.Sequential(
        nn.Linear(in_dim, hidden),
        nn.LayerNorm(hidden),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(hidden, hidden // 2),
        nn.GELU(),
        nn.Linear(hidden // 2, out_dim),
    )


class DeepSetsARAM(nn.Module):
    def __init__(self, n_champs: int, embed_dim: int = 32, hidden: int = 64, dropout: float = 0.1):
        super().__init__()
        self.embed = nn.Embedding(n_champs, embed_dim)
        # MLP takes [diff || total] = 2 * embed_dim features
        self.mlp = _mlp(2 * embed_dim, hidden, 1, dropout)

    def _raw_logit(self, diff: torch.Tensor, total: torch.Tensor) -> torch.Tensor:
        """Unconstrained forward: logit = MLP([diff, total])."""
        h = torch.cat([diff, total], dim=-1)   # (B, 2D)
        return self.mlp(h).squeeze(-1)          # (B,)

    def forward(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        """
        blue, red: (B, 5) long tensors of 0-indexed champion IDs
        returns:   (B,) logits — positive means P(blue wins) > 0.5
        """
        e_b = self.embed(blue).sum(dim=1)   # (B, D)
        e_r = self.embed(red).sum(dim=1)    # (B, D)
        diff  = e_b - e_r                   # antisymmetric
        total = e_b + e_r                   # symmetric

        # Antisymmetry guarantee: (f(d,t) - f(-d,t)) / 2
        logit = (self._raw_logit(diff, total) - self._raw_logit(-diff, total)) / 2.0
        return logit

    @torch.no_grad()
    def predict_proba(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward(blue, red))
