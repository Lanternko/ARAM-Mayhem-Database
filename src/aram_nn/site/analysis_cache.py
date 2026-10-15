"""Private, dependency-keyed cache for deterministic pooled site analyses."""
from __future__ import annotations

from functools import wraps
import hashlib
from importlib.metadata import version, PackageNotFoundError
import inspect
import json
import os
from pathlib import Path
import tempfile

from .build_timing import stage


def file_identity(path):
    p = Path(path).resolve()
    st = p.stat()
    return str(p), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino


def source_digest(root):
    h = hashlib.sha256()
    # Include transitive local helpers, rather than guessing which script import
    # is relevant. A source change costs one cold build, never a stale result.
    for folder in ('scripts', 'src/aram_nn'):
        for path in sorted((root / folder).rglob('*.py')):
            h.update(path.relative_to(root).as_posix().encode())
            h.update(path.read_bytes())
    for name in ('numpy', 'scipy', 'scikit-learn', 'polars', 'pyarrow', 'torch'):
        try:
            h.update((name + ':' + str(version(name) or 'unknown')).encode())
        except PackageNotFoundError:
            h.update((name + ':missing').encode())
    return h.hexdigest()


def cached_analysis(kind, payload_fields):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = inspect.signature(function).bind(*args, **kwargs)
            bound.apply_defaults()
            options = dict(bound.arguments)
            data = Path(options.pop('data'))
            out = Path(options.pop('out'))
            payload = Path(options.pop('payload', 'docs/api/tier-list.json'))
            root = Path(inspect.getfile(function)).resolve().parents[1]

            def dependencies():
                champs = json.loads(payload.read_text(encoding='utf-8')).get('champs', {})
                return dict(data=file_identity(data), champs={
                    cid: {key: info.get(key) for key in payload_fields}
                    for cid, info in champs.items()
                })

            with stage(kind + '.cache_lookup'):
                inputs = dependencies()
                key = hashlib.sha256(json.dumps(dict(
                    version=1, kind=kind, inputs=inputs, options=options,
                    source=source_digest(root),
                ), sort_keys=True).encode()).hexdigest()
                cache_dir = Path(os.environ.get('ARAM_SITE_ANALYSIS_CACHE', 'data/cache/site-analysis'))
                entry = cache_dir / (key + '.json')
                try:
                    record = json.loads(entry.read_text(encoding='utf-8'))
                    content = record['content']
                    artifact = json.loads(content)
                    if (record['key'] == key and artifact['kind'] == kind
                            and isinstance(artifact['champs'], dict)
                            and hashlib.sha256(content.encode()).hexdigest() == record['sha256']):
                        with stage(kind + '.cache_hit'):
                            out.parent.mkdir(parents=True, exist_ok=True)
                            out.write_text(content, encoding='utf-8')
                        print(f'[site-cache] {kind} hit', flush=True)
                        return
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            print(f'[site-cache] {kind} miss', flush=True)
            with stage(kind + '.compute'):
                result = function(*args, **kwargs)
            # Never persist a result whose input changed while it was computed.
            if dependencies() != inputs:
                print(f'[site-cache] {kind} input changed; not cached', flush=True)
                return result
            content = out.read_text(encoding='utf-8')
            artifact = json.loads(content)
            if artifact.get('kind') != kind or not isinstance(artifact.get('champs'), dict):
                raise ValueError(f'invalid {kind} artifact')
            cache_dir.mkdir(parents=True, exist_ok=True)
            record = dict(key=key, content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache_dir, delete=False) as f:
                tmp = Path(f.name)
                json.dump(record, f)
            try:
                tmp.replace(entry)
            finally:
                tmp.unlink(missing_ok=True)
            return result
        return wrapped
    return decorate
