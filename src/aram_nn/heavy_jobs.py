"""Serialize background analysis and stop its children under memory pressure.

The OS lock is host-wide, independent of checkout/state paths. Waiting jobs do
not load data. This is a sampled circuit breaker, not an allocation hard limit.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import psutil

from .system_memory import ResourceSample, sample_resources


class ResourcePressure(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    start_available_mb: float = 24576
    start_commit_percent: float = 70
    start_commit_free_mb: float = 24576
    stop_available_mb: float = 4096
    stop_commit_percent: float = 85
    poll_sec: float = 1
    retry_sec: float = 30
    healthy_samples: int = 3


def can_start(sample: ResourceSample, limits: Limits) -> bool:
    return bool(sample.complete
                and sample.available_mb >= limits.start_available_mb
                and sample.commit_percent <= limits.start_commit_percent
                and sample.commit_limit_mb - sample.commit_total_mb >= limits.start_commit_free_mb)


def must_stop(sample: ResourceSample, limits: Limits) -> bool:
    return bool(not sample.complete
                or sample.available_mb <= limits.stop_available_mb
                or sample.commit_percent >= limits.stop_commit_percent)


def guard_directory() -> Path:
    return Path(os.environ.get('ARAM_HEAVY_JOB_DIR') or
                Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AramMeta' / 'heavy-jobs')


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_active = ContextVar('heavy_job', default=None)


class HeavyJob:
    def __init__(self, name: str, directory: Path | None = None, limits: Limits = Limits()):
        self.name = name
        self.directory = directory or guard_directory()
        self.limits = limits

    def event(self, action: str, **fields):
        self.directory.mkdir(parents=True, exist_ok=True)
        row = dict(time=time.time(), action=action, job=self.name,
                   pid=os.getpid(), thresholds=asdict(self.limits), **fields)
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    @contextmanager
    def admitted(self):
        while True:
            with exclusive_lock(self.directory / 'analysis.lock') as acquired:
                if acquired:
                    healthy = 0
                    while healthy < self.limits.healthy_samples:
                        sample = sample_resources()
                        healthy = healthy + 1 if can_start(sample, self.limits) else 0
                        self.event('admission_sample', sample=sample.as_dict(), healthy_samples=healthy)
                        if healthy < self.limits.healthy_samples:
                            time.sleep(self.limits.retry_sec)
                    token = _active.set(self)
                    try:
                        self.event('admitted')
                        yield self
                    finally:
                        _active.reset(token)
                        self.event('released')
                    return
                self.event('queued', reason='another analysis pipeline owns the lock')
            time.sleep(self.limits.retry_sec)


def guarded_pipeline(function):
    """Guard real runners, including direct API calls; injected test runners stay pure."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        runner = kwargs.get('runner')
        if (_active.get() is not None or kwargs.get('check_only')
                or (function.__name__ == 'refresh_models_once' and kwargs.get('dry_run'))
                or (runner is not None and runner is not function.__kwdefaults__.get('runner'))):
            return function(*args, **kwargs)
        with HeavyJob(function.__name__).admitted():
            return function(*args, **kwargs)
    return wrapped


def _stop_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()
    _, alive = psutil.wait_procs(children, timeout=5)
    if alive:
        raise RuntimeError('analysis descendants did not exit; refusing to continue')


# A hidden background git can never answer a credential prompt. On 2026-09-10 an
# expired GitHub token left `git push` parked in git-credential-manager for 19h
# while the publisher looked alive, so git must fail fast and loudly instead.
GIT_TIMEOUT_SEC = 600
GIT_NONINTERACTIVE_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}


def _run_git(command, cwd=None, timeout=None):
    timeout = GIT_TIMEOUT_SEC if timeout is None else timeout
    env = {**os.environ, **GIT_NONINTERACTIVE_ENV}
    # Temp files, not pipes: a surviving credential helper can hold a pipe open
    # and turn communicate() into the very hang this timeout exists to stop.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_tree(process)
            stderr.seek(0)
            detail = stderr.read().decode(errors='replace').strip()
            message = (f'timed out after {timeout:g}s (credential prompt or network hang?)'
                       + (f': {detail}' if detail else ''))
            return subprocess.CompletedProcess(command, 124, '', message)
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(command, process.returncode,
                                           stdout.read().decode(errors='replace'),
                                           stderr.read().decode(errors='replace'))


def run_command(command, cwd=None):
    """Capture output on disk and monitor only analysis children, never git cleanup."""
    if Path(str(command[0])).stem.lower() == 'git':
        return _run_git(command, cwd=cwd)
    job = _active.get()
    if job is None:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                              check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    sample = sample_resources()
    if must_stop(sample, job.limits):
        job.event('deferred', sample=sample.as_dict(), command=list(command))
        raise ResourcePressure('memory pressure before analysis child launch')
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                sample = sample_resources()
                telemetry = []
                try:
                    parent = psutil.Process(process.pid)
                    for child in [parent, *parent.children(recursive=True)]:
                        try:
                            memory = child.memory_info()
                            telemetry.append(dict(pid=child.pid, ppid=child.ppid(),
                                                  created=child.create_time(), command=child.cmdline(),
                                                  rss_mb=memory.rss / 1048576,
                                                  private_mb=getattr(memory, 'private', None) / 1048576
                                                  if hasattr(memory, 'private') else None))
                        except psutil.NoSuchProcess:
                            pass
                except psutil.NoSuchProcess:
                    pass
                job.event('child_sample', command=list(command), children=telemetry,
                          sample=sample.as_dict())
                if must_stop(sample, job.limits):
                    job.event('abort', command=list(command), sample=sample.as_dict())
                    raise ResourcePressure('analysis stopped: system memory pressure or unavailable counters')
                try:
                    process.wait(timeout=job.limits.poll_sec)
                    job.event('child_exit', child_pid=process.pid, returncode=process.returncode)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            _stop_tree(process)
            raise
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(command, process.returncode,
                                           stdout.read().decode(errors='replace'),
                                           stderr.read().decode(errors='replace'))
