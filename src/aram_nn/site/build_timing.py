"""Structured build timings shared by the publisher and its Python children."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
import sys
import time

PREFIX = '[site-timing] '
_report = ContextVar('site_timing', default=None)
_parent = ContextVar('site_timing_parent', default=None)


@contextmanager
def stage(name):
    started = time.perf_counter()
    parent = _parent.get()
    token = _parent.set(name)
    status = 'ok'
    try:
        yield
    except BaseException:
        status = 'failed'
        raise
    finally:
        row = dict(stage=name, seconds=round(time.perf_counter() - started, 6), status=status, parent=parent)
        _parent.reset(token)
        report = _report.get()
        if report is not None:
            report.append(row)
        print(PREFIX + json.dumps(row), flush=True)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def collect_child_timings(stdout):
    report = _report.get()
    for line in stdout.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            row = json.loads(line[len(PREFIX):])
            if not isinstance(row['stage'], str) or not isinstance(row['seconds'], (int, float)):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if row.get('parent') is None:
            row['parent'] = _parent.get()
        if report is not None:
            report.append(row)
        print(PREFIX + json.dumps(row), flush=True)


def publish_report(function):
    """Outer wrapper includes admission wait; recursive disposable builds share it."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _report.get() is not None:
            return function(*args, **kwargs)
        rows = []
        token = _report.set(rows)
        started = time.perf_counter()
        started_unix = time.time()
        result = None
        status = 'failed'
        try:
            result = function(*args, **kwargs)
            status = 'ok'
            return result
        finally:
            rows.append(dict(stage='publisher.total', seconds=round(time.perf_counter()-started, 6), status=status))
            payload = dict(version=1, started_unix=started_unix, status=status,
                           semantics='Parent stage durations include their children; do not sum nested stages.',
                           queue_id=kwargs.get('queue_id', 2400), patch_prefix=kwargs.get('patch_prefix'), stages=rows)
            if isinstance(result, dict):
                result['timings'] = payload
            path = kwargs.get('timings_path')
            try:
                # Polling below the growth threshold must not replace the last
                # actual build with a misleading sub-second "publish" report.
                if path and (status == 'failed' or any(r['stage'] == 'publisher.tier_list' for r in rows)):
                    path = Path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(path.suffix + '.tmp')
                    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
                    tmp.replace(path)
                    with path.with_suffix('.jsonl').open('a', encoding='utf-8') as history:
                        history.write(json.dumps(payload) + '\n')
            except OSError as exc:
                # A metrics disk error cannot turn a successful Git push into a
                # failed publish or hide the original build exception.
                print(f'[site-timing] report write failed: {type(exc).__name__}', file=sys.stderr, flush=True)
            finally:
                print(PREFIX + json.dumps(rows[-1]), flush=True)
                _report.reset(token)
    return wrapped
