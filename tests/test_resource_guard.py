from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import aram_nn.resource_guard as guard


def _sample(
    *,
    available_mb: float = 8192.0,
    commit_percent: float = 50.0,
    complete: bool = True,
) -> guard.ResourceSample:
    if not complete:
        return guard.ResourceSample(
            available_mb=available_mb,
            commit_total_mb=None,
            commit_limit_mb=None,
            commit_percent=None,
            commit_error="missing commit counters",
        )
    limit = 1000.0
    return guard.ResourceSample(
        available_mb=available_mb,
        commit_total_mb=limit * commit_percent / 100.0,
        commit_limit_mb=limit,
        commit_percent=commit_percent,
    )


def _controller() -> guard.ResourceGuard:
    return guard.ResourceGuard(
        guard.ResourceGuardConfig(normal_workers=2, degraded_workers=1)
    )


def test_get_performance_info_converts_size_t_pages_to_mb(monkeypatch) -> None:
    class FakeGetPerformanceInfo:
        argtypes = None
        restype = None

        def __call__(self, pointer, size):
            info = pointer._obj
            assert size == ctypes.sizeof(info)
            info.PageSize = 4096
            info.CommitTotal = 256 * 1024
            info.CommitLimit = 512 * 1024
            return 1

    class FakePsapi:
        GetPerformanceInfo = FakeGetPerformanceInfo()

    monkeypatch.setattr(guard.os, "name", "nt")
    monkeypatch.setattr(
        guard.ctypes,
        "WinDLL",
        lambda *args, **kwargs: FakePsapi(),
        raising=False,
    )

    total_mb, limit_mb, error = guard.windows_commit_memory()

    assert total_mb == 1024.0
    assert limit_mb == 2048.0
    assert error is None


def test_get_performance_info_failure_keeps_commit_unknown(monkeypatch) -> None:
    class FakeGetPerformanceInfo:
        argtypes = None
        restype = None

        def __call__(self, pointer, size):
            return 0

    class FakePsapi:
        GetPerformanceInfo = FakeGetPerformanceInfo()

    monkeypatch.setattr(guard.os, "name", "nt")
    monkeypatch.setattr(
        guard.ctypes,
        "WinDLL",
        lambda *args, **kwargs: FakePsapi(),
        raising=False,
    )
    monkeypatch.setattr(guard.ctypes, "get_last_error", lambda: 5, raising=False)

    total_mb, limit_mb, error = guard.windows_commit_memory()

    assert total_mb is None
    assert limit_mb is None
    assert "winerror=5" in (error or "")


def test_sample_resources_reports_available_ram_and_commit(monkeypatch) -> None:
    monkeypatch.setattr(
        guard.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=4096 * 1024 * 1024),
    )
    monkeypatch.setattr(
        guard,
        "windows_commit_memory",
        lambda: (800.0, 1000.0, None),
    )

    sample = guard.sample_resources()

    assert sample.available_mb == 4096.0
    assert sample.commit_total_mb == 800.0
    assert sample.commit_limit_mb == 1000.0
    assert sample.commit_percent == 80.0
    assert sample.complete is True


def test_eighty_percent_commit_downshifts_to_degraded() -> None:
    decision = _controller().decide(_sample(commit_percent=80.0), 2, 1000.0)

    assert decision.desired_workers == 1
    assert decision.state == "degraded"
    assert decision.system_pressure is True


def test_ninety_percent_commit_pauses_workers() -> None:
    controller = _controller()
    decision = controller.decide(
        _sample(commit_percent=90.0), 2, 1000.0, latest_capture_age_min=1.0
    )

    assert decision.desired_workers == 0
    assert decision.paused is True
    assert decision.capture_suppressed is True


def test_resume_after_pause_waits_for_three_healthy_samples() -> None:
    controller = _controller()
    controller.decide(_sample(commit_percent=90.0), 2, 1000.0, 1.0)

    first = controller.decide(_sample(), 0, 1000.0, 12.0)
    second = controller.decide(_sample(), 0, 1000.0, 12.0)
    third = controller.decide(_sample(), 0, 1000.0, 12.0)

    assert first.desired_workers == 0
    assert second.desired_workers == 0
    assert third.desired_workers == 2


def test_pause_capture_suppression_clears_when_capture_age_drops() -> None:
    controller = _controller()
    controller.decide(_sample(commit_percent=90.0), 2, 1000.0, 1.0)
    controller.decide(_sample(commit_percent=90.0), 0, 1000.0, 120.0)

    decision = controller.decide(_sample(), 0, 1000.0, 0.2)

    assert decision.capture_suppressed is False


def test_empty_fleet_after_restart_suppresses_stale_capture_drought() -> None:
    decision = _controller().decide(_sample(), 0, 1000.0, 120.0)

    assert decision.capture_suppressed is True


def test_recovery_requires_three_samples_and_pressure_resets_window() -> None:
    controller = _controller()

    first = controller.decide(_sample(), 1, 1000.0)
    second = controller.decide(_sample(), 1, 1000.0)
    pressured = controller.decide(_sample(commit_percent=80.0), 1, 1000.0)
    after_reset = controller.decide(_sample(), 1, 1000.0)
    final = controller.decide(_sample(), 1, 1000.0)
    recovered = controller.decide(_sample(), 1, 1000.0)

    assert first.desired_workers == 1 and first.healthy_samples == 1
    assert second.desired_workers == 1 and second.healthy_samples == 2
    assert pressured.healthy_samples == 0
    assert after_reset.healthy_samples == 1
    assert final.healthy_samples == 2
    assert recovered.desired_workers == 2


def test_missing_counter_resets_recovery_and_does_not_upshift() -> None:
    controller = _controller()
    controller.decide(_sample(), 1, 1000.0)

    decision = controller.decide(_sample(complete=False), 1, 1000.0)

    assert decision.desired_workers == 1
    assert decision.healthy_samples == 0
    assert decision.state == "unknown"


def test_high_client_pressure_stays_degraded_even_above_restart_threshold() -> None:
    decision = _controller().decide(_sample(), 2, 6000.0)

    assert decision.desired_workers == 1
    assert decision.client_pressure is True


def test_same_worker_count_does_not_request_rebuild() -> None:
    decision = _controller().decide(_sample(), 2, 1000.0)

    assert decision.desired_workers == 2
    assert decision.actual_workers == decision.desired_workers


def test_available_ram_alone_restricts_workers_even_without_commit_counter() -> None:
    for available, expected in [(3072.0, 1), (1536.0, 0)]:
        for complete in (True, False):
            decision = _controller().decide(
                _sample(available_mb=available, complete=complete), 2, 1000.0
            )
            assert decision.desired_workers == expected


def test_resume_one_worker_in_client_hysteresis_band():
    controller = guard.ResourceGuard(guard.ResourceGuardConfig(
        normal_workers=2, degraded_workers=1, client_degrade_mb=3900))
    assert controller.decide(_sample(commit_percent=97), 2, 3544, 0).desired_workers == 0
    for i in range(2):
        assert controller.decide(_sample(), 0, 3685, i + 1).desired_workers == 0
    decision = controller.decide(_sample(), 0, 3685, 3)
    assert decision.desired_workers == 1
    assert decision.capture_suppressed
    assert not decision.healthy
    assert controller.decide(_sample(), 1, 3685, 0).capture_suppressed is False
    for i in range(3):
        decision = controller.decide(_sample(), 1, 3400, 0)
    assert decision.desired_workers == 2


def test_conservative_resume_window_resets_on_missing_or_pressured_sample():
    for bad_sample, client in [(_sample(complete=False), 3685),
                               (_sample(commit_percent=85), 3685),
                               (_sample(), 3950)]:
        controller = guard.ResourceGuard(guard.ResourceGuardConfig(
            normal_workers=2, degraded_workers=1, client_degrade_mb=3900))
        controller.decide(_sample(commit_percent=97), 2, 3544, 0)
        controller.decide(_sample(), 0, 3685, 1)
        assert controller.decide(bad_sample, 0, client, 2).desired_workers == 0
        for i in range(2):
            assert controller.decide(_sample(), 0, 3685, i + 3).desired_workers == 0
        assert controller.decide(_sample(), 0, 3685, 5).desired_workers == 1


def _headroom_sample(
    *, commit_total_mb: float, commit_limit_mb: float = 38133.0, available_mb: float = 12000.0
) -> guard.ResourceSample:
    return guard.ResourceSample(
        available_mb=available_mb,
        commit_total_mb=commit_total_mb,
        commit_limit_mb=commit_limit_mb,
        commit_percent=round(commit_total_mb / commit_limit_mb * 100.0, 1),
    )


def test_wide_absolute_commit_headroom_is_not_pressure() -> None:
    controller = _controller()
    # 87.9% commit, but 4.6 GB still uncommitted and 12 GB RAM free.
    sample = _headroom_sample(commit_total_mb=33528.0)

    for _ in range(3):
        decision = controller.decide(sample, 2, 1000.0, 1.0)

    assert decision.system_pressure is False
    assert decision.desired_workers == 2
    assert decision.state == "healthy"


def test_thin_commit_headroom_degrades_without_pausing() -> None:
    # 90.6% commit, 3.6 GB headroom: over the pause percent but not thin
    # enough in absolute terms to justify stopping collection outright.
    decision = _controller().decide(
        _headroom_sample(commit_total_mb=34539.0), 2, 1000.0, 1.0
    )

    assert decision.system_pressure is True
    assert decision.paused is False
    assert decision.desired_workers == 1


def test_very_thin_commit_headroom_still_pauses() -> None:
    decision = _controller().decide(
        _headroom_sample(commit_total_mb=36500.0), 2, 1000.0, 1.0
    )

    assert decision.paused is True
    assert decision.desired_workers == 0


def test_degraded_fleet_restarts_one_worker_instead_of_ratcheting_to_zero() -> None:
    controller = _controller()
    controller.decide(_sample(commit_percent=90.0), 2, 1000.0, 1.0)

    held = [
        controller.decide(_sample(commit_percent=85.0), 0, 1000.0, 10.0)
        for _ in range(9)
    ]
    restarted = controller.decide(_sample(commit_percent=85.0), 0, 1000.0, 10.0)

    assert [d.desired_workers for d in held] == [0] * 9
    assert restarted.desired_workers == 1
    assert restarted.state == "degraded"


def test_degraded_restart_hold_resets_on_every_pause() -> None:
    controller = _controller()
    controller.decide(_sample(commit_percent=90.0), 2, 1000.0, 1.0)
    for _ in range(9):
        controller.decide(_sample(commit_percent=85.0), 0, 1000.0, 10.0)

    controller.decide(_sample(commit_percent=95.0), 0, 1000.0, 10.0)
    decision = controller.decide(_sample(commit_percent=85.0), 0, 1000.0, 10.0)

    assert decision.desired_workers == 0


def test_degraded_fleet_restarts_immediately_when_no_pause_preceded_it() -> None:
    decision = _controller().decide(_sample(commit_percent=85.0), 0, 1000.0, 10.0)

    assert decision.desired_workers == 1
    assert decision.state == "degraded"
