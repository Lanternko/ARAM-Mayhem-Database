"""Low-cost system memory sampling and worker-fleet pressure control.

The controller intentionally treats incomplete samples as unknown.  A missing
counter may keep the currently observed fleet size, but it can never authorize
an upshift or a normal startup.  This keeps a failed Windows counter probe
from looking like a healthy machine.
"""

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


@dataclass(frozen=True)
class ResourceGuardConfig:
    normal_workers: int
    degraded_workers: int
    degrade_commit_percent: float = 80.0
    pause_commit_percent: float = 90.0
    resume_commit_percent: float = 70.0
    degrade_available_mb: float = 3072.0
    pause_available_mb: float = 1536.0
    resume_available_mb: float = 4096.0
    # Commit percent alone punishes hosts whose commit limit is small relative
    # to their RAM.  Only treat a high commit percent as pressure when the
    # absolute headroom left under the limit is also thin.
    degrade_commit_headroom_mb: float = 4096.0
    pause_commit_headroom_mb: float = 2048.0
    client_degrade_mb: float = 4500.0
    recovery_samples: int = 3
    # A pause holds the fleet at zero until a full healthy window.  Release
    # that hold after this many consecutive non-pause samples so chronic
    # degrade-level pressure cannot strand the fleet at zero forever.
    degraded_restart_samples: int = 10

    def as_dict(self) -> dict[str, Any]:
        return {
            "normal_workers": self.normal_workers,
            "degraded_workers": self.degraded_workers,
            "degrade_commit_percent": self.degrade_commit_percent,
            "pause_commit_percent": self.pause_commit_percent,
            "resume_commit_percent": self.resume_commit_percent,
            "degrade_available_mb": self.degrade_available_mb,
            "pause_available_mb": self.pause_available_mb,
            "resume_available_mb": self.resume_available_mb,
            "degrade_commit_headroom_mb": self.degrade_commit_headroom_mb,
            "pause_commit_headroom_mb": self.pause_commit_headroom_mb,
            "client_degrade_mb": self.client_degrade_mb,
            "recovery_samples": self.recovery_samples,
            "degraded_restart_samples": self.degraded_restart_samples,
        }


@dataclass(frozen=True)
class ResourceGuardState:
    healthy_samples: int = 0
    resume_samples: int = 0
    initialized: bool = False
    initial_actual_workers: int | None = None
    suppress_until_capture: bool = False
    resume_requires_full_window: bool = False
    last_capture_age_min: float | None = None
    nonpause_samples: int = 0


@dataclass(frozen=True)
class ResourceDecision:
    actual_workers: int
    desired_workers: int
    state: str
    paused: bool
    capture_suppressed: bool
    system_pressure: bool
    client_pressure: bool
    healthy: bool
    healthy_samples: int
    reason: str
    sample: ResourceSample
    thresholds: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "actual_workers": self.actual_workers,
            "desired_workers": self.desired_workers,
            "state": self.state,
            "paused": self.paused,
            "capture_suppressed": self.capture_suppressed,
            "system_pressure": self.system_pressure,
            "client_pressure": self.client_pressure,
            "healthy": self.healthy,
            "healthy_samples": self.healthy_samples,
            "reason": self.reason,
            "sample": self.sample.as_dict(),
            "thresholds": self.thresholds,
        }


def evaluate_resource_guard(
    state: ResourceGuardState,
    sample: ResourceSample,
    actual_workers: int,
    client_pressure_mb: float | None,
    config: ResourceGuardConfig,
    latest_capture_age_min: float | None = None,
) -> tuple[ResourceGuardState, ResourceDecision]:
    """Advance the pure worker-pressure state machine by one sample."""

    actual_workers = max(0, int(actual_workers))
    normal_workers = max(1, int(config.normal_workers))
    degraded_workers = max(1, min(normal_workers, int(config.degraded_workers)))
    recovery_samples = max(1, int(config.recovery_samples))
    initialized = state.initialized
    initial_actual_workers = state.initial_actual_workers
    if not initialized:
        initialized = True
        initial_actual_workers = actual_workers

    suppress_until_capture = state.suppress_until_capture
    resume_requires_full_window = state.resume_requires_full_window
    if (
        not state.initialized
        and actual_workers == 0
        and latest_capture_age_min is not None
    ):
        # A watcher restart cannot distinguish an intentional prior pause from
        # a crashed fleet.  Keep the capture-drought restart override disabled
        # until a fresh capture makes the age decrease.
        suppress_until_capture = True
    if (
        suppress_until_capture
        and latest_capture_age_min is not None
        and state.last_capture_age_min is not None
        and float(latest_capture_age_min) < state.last_capture_age_min
    ):
        suppress_until_capture = False
    last_capture_age = (
        float(latest_capture_age_min)
        if latest_capture_age_min is not None
        else state.last_capture_age_min
    )

    commit_percent = sample.commit_percent
    available_mb = sample.available_mb
    commit_headroom_mb: float | None = None
    if sample.commit_total_mb is not None and sample.commit_limit_mb is not None:
        commit_headroom_mb = max(0.0, sample.commit_limit_mb - sample.commit_total_mb)

    def commit_pressure(percent_threshold: float, headroom_threshold: float) -> bool:
        if commit_percent is None or commit_percent < percent_threshold:
            return False
        # Unknown headroom keeps the historical percent-only behaviour.
        return commit_headroom_mb is None or commit_headroom_mb <= headroom_threshold

    commit_recovered = commit_percent is not None and (
        commit_percent <= config.resume_commit_percent
        or (
            commit_headroom_mb is not None
            and commit_headroom_mb >= config.degrade_commit_headroom_mb
        )
    )

    pause_pressure = (
        commit_pressure(config.pause_commit_percent, config.pause_commit_headroom_mb)
        or (available_mb is not None and available_mb <= config.pause_available_mb)
    )
    degrade_pressure = (
        commit_pressure(config.degrade_commit_percent, config.degrade_commit_headroom_mb)
        or (available_mb is not None and available_mb <= config.degrade_available_mb)
    )
    client_pressure = (
        client_pressure_mb is not None
        and client_pressure_mb >= config.client_degrade_mb
    )
    system_pressure = pause_pressure or degrade_pressure
    recovery_client_limit = config.client_degrade_mb - 400.0
    healthy = (
        sample.complete
        and commit_recovered
        and available_mb is not None
        and available_mb >= config.resume_available_mb
        and client_pressure_mb is not None
        and client_pressure_mb < recovery_client_limit
    )

    resume_safe = (
        sample.complete
        and commit_recovered
        and available_mb is not None
        and available_mb >= config.resume_available_mb
        and client_pressure_mb is not None
        and client_pressure_mb < config.client_degrade_mb
    )
    resume_samples = min(state.resume_samples + 1, recovery_samples) if resume_safe else 0

    restart_samples = max(1, int(config.degraded_restart_samples))
    nonpause_samples = 0 if pause_pressure else min(state.nonpause_samples + 1, restart_samples)

    if pause_pressure:
        next_healthy_samples = 0
        desired_workers = 0
        state_name = "paused"
        reason = "system memory pressure reached pause threshold"
        suppress_until_capture = True
        resume_requires_full_window = True
    elif system_pressure or client_pressure:
        next_healthy_samples = 0
        desired_workers = min(actual_workers, degraded_workers)
        state_name = "degraded"
        reason = "system or LeagueClient pressure reached degrade threshold"
        if desired_workers == 0 and (
            not resume_requires_full_window or nonpause_samples >= restart_samples
        ):
            # Degrade-level pressure means one producer is acceptable.  Without
            # this floor an empty fleet can never climb back to one, because
            # only the fully healthy path ever raises the worker count.
            desired_workers = degraded_workers
            resume_requires_full_window = False
            reason = "degrade-level pressure restarts the conservative single-producer fleet"
    elif healthy:
        next_healthy_samples = min(state.healthy_samples + 1, recovery_samples)
        if next_healthy_samples >= recovery_samples:
            desired_workers = normal_workers
            state_name = "healthy"
            reason = "complete healthy sample window reached"
        elif state.resume_requires_full_window:
            desired_workers = 0
            state_name = "recovering_paused"
            reason = "holding the fleet paused until the full recovery window completes"
        elif actual_workers:
            desired_workers = min(actual_workers, normal_workers)
            state_name = "recovering"
            reason = "waiting for consecutive healthy samples before upshift"
        else:
            desired_workers = degraded_workers
            state_name = "recovering"
            reason = "starting conservatively while healthy sample window fills"
    elif resume_safe:
        next_healthy_samples = 0
        desired_workers = (
            degraded_workers if resume_samples >= recovery_samples
            else min(actual_workers, degraded_workers)
        )
        state_name = "resumed_degraded" if desired_workers else "recovering_paused"
        reason = "system recovered; conservative fleet awaits or holds its safe recovery window"
    else:
        # Missing or merely non-pressured counters cannot prove recovery.  Keep
        # the observed fleet exactly where it is and reset the recovery window.
        next_healthy_samples = 0
        desired_workers = min(actual_workers, normal_workers) if actual_workers else 0
        state_name = "unknown"
        reason = "resource or client pressure data incomplete or not recovery-healthy"

    next_state = ResourceGuardState(
        healthy_samples=next_healthy_samples,
        resume_samples=resume_samples,
        initialized=initialized,
        initial_actual_workers=initial_actual_workers,
        suppress_until_capture=suppress_until_capture,
        resume_requires_full_window=(
            resume_requires_full_window
            and next_healthy_samples < recovery_samples
        ),
        last_capture_age_min=last_capture_age,
        nonpause_samples=nonpause_samples,
    )
    decision = ResourceDecision(
        actual_workers=actual_workers,
        desired_workers=desired_workers,
        state=state_name,
        paused=desired_workers == 0 and (pause_pressure or state_name == "paused"),
        capture_suppressed=suppress_until_capture,
        system_pressure=system_pressure,
        client_pressure=client_pressure,
        healthy=healthy,
        healthy_samples=next_healthy_samples,
        reason=reason,
        sample=sample,
        thresholds=config.as_dict(),
    )
    return next_state, decision


class ResourceGuard:
    """Stateful wrapper around :func:`evaluate_resource_guard`."""

    def __init__(self, config: ResourceGuardConfig) -> None:
        self.config = config
        self.state = ResourceGuardState()

    def decide(
        self,
        sample: ResourceSample,
        actual_workers: int,
        client_pressure_mb: float | None,
        latest_capture_age_min: float | None = None,
    ) -> ResourceDecision:
        self.state, decision = evaluate_resource_guard(
            self.state,
            sample,
            actual_workers,
            client_pressure_mb,
            self.config,
            latest_capture_age_min,
        )
        return decision
