from pathlib import Path
import subprocess
import sys

import pytest

from aram_nn import heavy_jobs as jobs
from aram_nn.system_memory import ResourceSample


HEALTHY = ResourceSample(40000, 30000, 100000, 30)
PRESSURE = ResourceSample(3000, 86000, 100000, 86)


def test_admission_requires_headroom_and_complete_counters():
    limits = jobs.Limits()
    assert jobs.can_start(HEALTHY, limits)
    assert not jobs.can_start(ResourceSample(40000, 60000, 80000, 75), limits)
    assert not jobs.can_start(ResourceSample(20000, 30000, 100000, 30), limits)
    assert not jobs.can_start(ResourceSample(40000, 10000, 30000, 33), limits)
    assert not jobs.can_start(ResourceSample(40000, None, None, None), limits)
    assert jobs.must_stop(PRESSURE, limits)
    assert jobs.must_stop(ResourceSample(None, None, None, None), limits)


def test_os_lock_excludes_other_process_and_releases(tmp_path):
    lock = tmp_path / 'analysis.lock'
    code = ('from pathlib import Path; from aram_nn.heavy_jobs import exclusive_lock; '
            'import sys\nwith exclusive_lock(Path(sys.argv[1])) as acquired: print(acquired)')
    with jobs.exclusive_lock(lock) as acquired:
        assert acquired
        result = subprocess.check_output([sys.executable, '-c', code, str(lock)], text=True)
        assert result.strip() == 'False'
    assert subprocess.check_output([sys.executable, '-c', code, str(lock)], text=True).strip() == 'True'


def test_waiting_for_headroom_does_not_hold_lock(monkeypatch, tmp_path):
    n = {'i': 0}

    def sample():
        n['i'] += 1
        return PRESSURE if n['i'] < 3 else HEALTHY

    monkeypatch.setattr(jobs, 'sample_resources', sample)
    lock_free_during_wait = []

    def fake_sleep(_):
        with jobs.exclusive_lock(tmp_path / 'analysis.lock') as acquired:
            lock_free_during_wait.append(acquired)

    monkeypatch.setattr(jobs.time, 'sleep', fake_sleep)
    job = jobs.HeavyJob('test', tmp_path, jobs.Limits(healthy_samples=1))
    with job.admitted():
        pass
    assert lock_free_during_wait and all(lock_free_during_wait)


def test_waits_for_consecutive_healthy_samples(monkeypatch, tmp_path):
    samples = iter([HEALTHY, PRESSURE, HEALTHY, HEALTHY, HEALTHY, HEALTHY])
    monkeypatch.setattr(jobs, 'sample_resources', lambda: next(samples))
    monkeypatch.setattr(jobs.time, 'sleep', lambda _: None)
    job = jobs.HeavyJob('test', tmp_path)
    with job.admitted():
        assert jobs._active.get() is job
    assert jobs._active.get() is None
    events = (tmp_path / 'events.jsonl').read_text()
    assert events.count('admission_sample') == 5


def test_pressure_kills_running_child_and_releases_lock(monkeypatch, tmp_path):
    samples = iter([HEALTHY, HEALTHY, HEALTHY, PRESSURE])
    monkeypatch.setattr(jobs, 'sample_resources', lambda: next(samples))
    job = jobs.HeavyJob('test', tmp_path, jobs.Limits(healthy_samples=1))
    launched = []
    original = jobs.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(jobs.subprocess, 'Popen', launch)
    with pytest.raises(jobs.ResourcePressure):
        with job.admitted():
            jobs.run_command([sys.executable, '-c', 'import time; time.sleep(60)'])
    assert launched[0].poll() is not None
    with jobs.exclusive_lock(tmp_path / 'analysis.lock') as acquired:
        assert acquired
    assert 'abort' in (tmp_path / 'events.jsonl').read_text()


def test_resource_abort_is_fatal_for_optional_radar(monkeypatch):
    from aram_nn.site import static_publish
    monkeypatch.setattr(static_publish, 'resolve_comp_fit_parquet', lambda _: Path('data.parquet'))
    def runner(command):
        raise jobs.ResourcePressure('pressure')
    with pytest.raises(jobs.ResourcePressure):
        static_publish.build_champ_archetype_fit(runner=runner)
    with pytest.raises(jobs.ResourcePressure):
        static_publish.build_champ_empirical_axes(runner=runner)
    with pytest.raises(jobs.ResourcePressure):
        static_publish.build_classic_page(runner=runner)


def test_real_command_output_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, 'sample_resources', lambda: HEALTHY)
    with jobs.HeavyJob('test', tmp_path, jobs.Limits(healthy_samples=1)).admitted():
        result = jobs.run_command([sys.executable, '-c', "import sys; print('hello'); sys.exit(7)"])
    assert result.returncode == 7
    assert result.stdout.strip() == 'hello'


def test_pipeline_reentrancy_and_check_only(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, 'guard_directory', lambda: tmp_path)
    monkeypatch.setattr(jobs, 'sample_resources', lambda: HEALTHY)
    monkeypatch.setattr(jobs.time, 'sleep', lambda _: None)
    def real_runner(command):
        pass
    @jobs.guarded_pipeline
    def pipeline(*, runner=real_runner, check_only=False, nested=False):
        if check_only:
            assert jobs._active.get() is None
        else:
            assert jobs._active.get() is not None
        if nested:
            pipeline()
    pipeline(check_only=True)
    assert not tmp_path.joinpath('events.jsonl').exists()
    pipeline(nested=True)
    assert tmp_path.joinpath('events.jsonl').read_text().count('"action": "admitted"') == 1


def test_stop_tree_kills_descendant(tmp_path):
    import psutil
    import time
    pidfile = tmp_path / 'child.pid'
    code = ("import subprocess, sys, pathlib, time; "
            "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    process = subprocess.Popen([sys.executable, '-c', code, str(pidfile)])
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        child = psutil.Process(int(pidfile.read_text()))
        jobs._stop_tree(process)
        assert not child.is_running()
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            jobs._stop_tree(process)


def test_git_timeout_kills_tree_and_reports(tmp_path):
    # Child spawns a grandchild that inherits stdio, like a stuck credential helper.
    code = ('import subprocess, sys, time; '
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('waiting for credentials', file=sys.stderr, flush=True); time.sleep(60)")
    started = __import__('time').monotonic()
    result = jobs._run_git([sys.executable, '-c', code], timeout=2)
    assert __import__('time').monotonic() - started < 20
    assert result.returncode == 124
    assert 'timed out after 2s' in result.stderr
    assert 'waiting for credentials' in result.stderr


def test_site_build_floor_clears_what_the_default_floor_rejects():
    # A real sample from the publisher host, 2026-09-12 14:30. The default floor
    # rejected every one of 3,088 such samples, and `must_stop` fired on them too,
    # so the site build could neither start nor launch a child.
    host = ResourceSample(12620, 43754, 50648, 86)
    assert not jobs.can_start(host, jobs.Limits())
    assert jobs.must_stop(host, jobs.Limits())
    assert jobs.can_start(host, jobs.SITE_BUILD_LIMITS)
    assert not jobs.must_stop(host, jobs.SITE_BUILD_LIMITS)


def test_publisher_runs_on_the_site_build_floor(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(jobs, 'guard_directory', lambda: tmp_path)
    monkeypatch.setattr(jobs, 'sample_resources', lambda: ResourceSample(12620, 43754, 50648, 86))
    # The host sample clears the site-build floor every time, so the gate sleeps
    # only between the consecutive samples it needs. A gate that fell back to the
    # default floor would sleep forever instead of waiting a bounded few times.
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= jobs.SITE_BUILD_LIMITS.healthy_samples:
            pytest.fail('gate rejected the host sample')

    monkeypatch.setattr(jobs.time, 'sleep', fake_sleep)

    @jobs.guarded_pipeline
    def publish_static_site_once(*, runner=lambda command: None):
        return 'published'

    assert publish_static_site_once() == 'published'
    admitted = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()
                if '"admitted"' in line]
    assert len(admitted) == 1
    assert admitted[0]['thresholds']['start_available_mb'] == jobs.SITE_BUILD_LIMITS.start_available_mb


def test_starved_gate_warns_on_the_callers_own_log(monkeypatch, tmp_path, capsys):
    samples = iter([PRESSURE, PRESSURE, PRESSURE, HEALTHY, HEALTHY])
    monkeypatch.setattr(jobs, 'sample_resources', lambda: next(samples))
    monkeypatch.setattr(jobs.time, 'sleep', lambda _: None)
    limits = jobs.Limits(healthy_samples=1, retry_sec=30, wait_warn_sec=60)
    with jobs.HeavyJob('publish_static_site_once', tmp_path, limits).admitted():
        pass
    warnings = [line for line in capsys.readouterr().err.splitlines() if '[heavy-job]' in line]
    assert len(warnings) == 1
    assert 'publish_static_site_once has waited 1min' in warnings[0]
    assert f'available={PRESSURE.available_mb}MB (need 24576)' in warnings[0]


def test_git_runs_noninteractive(monkeypatch):
    seen = {}
    original = jobs.subprocess.Popen
    def launch(command, **kwargs):
        seen.update(kwargs['env'])
        return original([sys.executable, '-c', "print('ok')"], **kwargs)
    monkeypatch.setattr(jobs.subprocess, 'Popen', launch)
    result = jobs.run_command(['git', 'push', 'origin', 'HEAD:main'])
    assert result.returncode == 0 and result.stdout.strip() == 'ok'
    assert seen['GIT_TERMINAL_PROMPT'] == '0'
    assert seen['GCM_INTERACTIVE'] == 'never'


def test_model_refresh_floor_keeps_commit_reserve():
    limits = jobs.MODEL_REFRESH_LIMITS
    assert jobs.can_start(ResourceSample(11000, 43000, 50308, 85.5), limits)
    assert not jobs.can_start(ResourceSample(11000, 45000, 50308, 89.5), limits)
    assert jobs.must_stop(ResourceSample(10000, 48500, 50308, 96.4), limits)
    assert jobs.must_stop(ResourceSample(10000, 19000, 20000, 95), limits)
    assert not jobs.must_stop(ResourceSample(10000, 46000, 50308, 91.4), limits)


def test_admission_rechecks_after_lock(monkeypatch, tmp_path):
    samples = iter([HEALTHY, PRESSURE, HEALTHY, HEALTHY])
    monkeypatch.setattr(jobs, 'sample_resources', lambda: next(samples))
    with jobs.HeavyJob('test', tmp_path, jobs.Limits(healthy_samples=1)).admitted():
        pass
    assert 'admission_recheck_failed' in (tmp_path / 'events.jsonl').read_text()
