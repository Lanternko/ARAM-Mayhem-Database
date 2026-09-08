"""Shared Windows system memory counters for heavy-job admission."""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Any

import psutil
from ctypes import wintypes


_MB = 1024 * 1024


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    """Windows psapi PERFORMANCE_INFORMATION with SIZE_T page counters."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


def _mb(value: int | float) -> float:
    return round(float(value) / _MB, 1)


def windows_commit_memory() -> tuple[float | None, float | None, str | None]:
    """Return Windows commit total/limit in MB, or explicit unavailable data."""

    if os.name != "nt":
        return None, None, "GetPerformanceInfo is only available on Windows"

    try:
        psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
        get_performance_info = psapi.GetPerformanceInfo
        get_performance_info.argtypes = [
            ctypes.POINTER(_PERFORMANCE_INFORMATION),
            wintypes.DWORD,
        ]
        get_performance_info.restype = wintypes.BOOL
        info = _PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(info)
        if not get_performance_info(ctypes.byref(info), info.cb):
            error = ctypes.get_last_error()
            return None, None, f"GetPerformanceInfo failed (winerror={error})"
        page_size = int(info.PageSize)
        commit_total_pages = int(info.CommitTotal)
        commit_limit_pages = int(info.CommitLimit)
        if page_size <= 0 or commit_limit_pages <= 0:
            return None, None, "GetPerformanceInfo returned invalid page or commit limit"
        return (
            _mb(commit_total_pages * page_size),
            _mb(commit_limit_pages * page_size),
            None,
        )
    except Exception as exc:
        return None, None, f"GetPerformanceInfo unavailable: {type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class ResourceSample:
    available_mb: float | None
    commit_total_mb: float | None
    commit_limit_mb: float | None
    commit_percent: float | None
    available_error: str | None = None
    commit_error: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.available_mb is not None
            and self.commit_total_mb is not None
            and self.commit_limit_mb is not None
            and self.commit_percent is not None
            and self.available_error is None
            and self.commit_error is None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "available_mb": self.available_mb,
            "commit_total_mb": self.commit_total_mb,
            "commit_limit_mb": self.commit_limit_mb,
            "commit_percent": self.commit_percent,
            "complete": self.complete,
            "available_error": self.available_error,
            "commit_error": self.commit_error,
        }


def sample_resources() -> ResourceSample:
    """Sample available RAM and Windows commit without swap or VAS metrics."""

    available_mb: float | None = None
    available_error: str | None = None
    try:
        available_mb = _mb(psutil.virtual_memory().available)
    except Exception as exc:
        available_error = f"virtual_memory unavailable: {type(exc).__name__}: {exc}"

    commit_total_mb, commit_limit_mb, commit_error = windows_commit_memory()
    commit_percent: float | None = None
    if commit_total_mb is not None and commit_limit_mb:
        commit_percent = round(commit_total_mb / commit_limit_mb * 100.0, 1)
    elif commit_error is None:
        commit_error = "commit counters unavailable"

    return ResourceSample(
        available_mb=available_mb,
        commit_total_mb=commit_total_mb,
        commit_limit_mb=commit_limit_mb,
        commit_percent=commit_percent,
        available_error=available_error,
        commit_error=commit_error,
    )


