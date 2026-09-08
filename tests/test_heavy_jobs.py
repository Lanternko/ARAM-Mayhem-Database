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


def test_waits_for_consecutive_healthy_samples(monkeypatch, tmp_path):
    samples = iter([HEALTHY, PRESSURE, HEALTHY, HEALTHY, HEALTHY])
    monkeypatch.setattr(jobs, 'sample_resources', lambda: next(samples))
    monkeypatch.setattr(jobs.time, 'sleep', lambda _: None)
    job = jobs.HeavyJob('test', tmp_path)
    with job.admitted():
        assert jobs._active.get() is job
    assert jobs._active.get() is None
    events = (tmp_path / 'events.jsonl').read_text()
    assert events.count('admission_sample') == 5


def test_pressure_kills_running_child_and_releases_lock(monkeypatch, tmp_path):
    samples = iter([HEALTHY, HEALTHY, PRESSURE])
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
