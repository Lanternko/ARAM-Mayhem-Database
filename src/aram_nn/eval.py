"""Evaluation: log-loss, accuracy, ECE, and temperature scaling."""
from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader


# ---------- Metrics ----------

def log_loss_np(y_true: np.ndarray, y_prob: np.ndarray, eps: float = 1e-7) -> float:
    p = np.clip(y_prob, eps, 1 - eps)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def accuracy_np(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> float:
    return float(np.mean((y_prob >= threshold) == y_true.astype(bool)))


def ece_np(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if not mask.any():
            continue
        conf = y_prob[mask].mean()
        acc  = y_true[mask].mean()
        ece += mask.mean() * abs(conf - acc)
    return float(ece)


# ---------- Inference helpers ----------

@torch.no_grad()
def collect_probs(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[np.ndarray, np.ndarray]:
    model.eval()
    all_probs, all_labels = [], []
    for blue, red, y in loader:
        blue, red = blue.to(device), red.to(device)
        probs = model.predict_proba(blue, red).cpu().numpy()
        all_probs.append(probs)
        all_labels.append(y.numpy())
    return np.concatenate(all_probs), np.concatenate(all_labels)


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device, label: str = "") -> dict:
    probs, labels = collect_probs(model, loader, device)
    prefix = f"{label}/" if label else ""
    return {
        f"{prefix}log_loss": log_loss_np(labels, probs),
        f"{prefix}acc":      accuracy_np(labels, probs),
        f"{prefix}ece":      ece_np(labels, probs),
    }


# ---------- Temperature scaling ----------

class TemperatureScaler(nn.Module):
    """Wraps a model and learns a scalar temperature T on the val set.
    P_calibrated = sigmoid(logit / T)
    """
    def __init__(self, model: nn.Module):
        super().__init__()
        self.model = model
        self.temperature = nn.Parameter(torch.ones(1))

    def forward(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        logit = self.model(blue, red)
        return logit / self.temperature.clamp(min=1e-2)

    def predict_proba(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward(blue, red))

    def fit(self, val_loader: DataLoader, device: torch.device, lr: float = 0.01, steps: int = 200):
        self.to(device)
        was_training = self.model.training
        self.model.eval()
        opt = torch.optim.LBFGS([self.temperature], lr=lr, max_iter=steps)
        criterion = nn.BCEWithLogitsLoss()

        all_logits, all_labels = [], []
        with torch.no_grad():
            for blue, red, y in val_loader:
                blue, red = blue.to(device), red.to(device)
                logits = self.model(blue, red)
                all_logits.append(logits.cpu())
                all_labels.append(y)
        logits_t = torch.cat(all_logits).to(device)
        labels_t = torch.cat(all_labels).to(device)

        def closure():
            opt.zero_grad()
            scaled = logits_t / self.temperature.clamp(min=1e-2)
            loss = criterion(scaled, labels_t)
            loss.backward()
            return loss

        opt.step(closure)
        self.model.train(was_training)  # restore original train/eval state
        return self
