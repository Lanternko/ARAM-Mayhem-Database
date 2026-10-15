"""Bounded, build-local replay of projected participants, parsed exactly once.

The pickle stream is created and consumed only inside this context; it is never
loaded from a caller-supplied or persistent file. No player identifiers or stats
are retained. Each iterator holds one batch, rather than the whole patch in RAM.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path
import pickle
import tempfile

from aram_nn.gamedata import _connect_ro, _where
from .build_timing import stage

_active = ContextVar('site_participant_replay', default=None)
FIELDS = ('championId', 'teamId', 'augments', 'items', 'itemSlots', 'spells')


def _source(db, queue_id, patch_prefix, *, teams=True, require=None):
    where, params = _where(queue_id, patch_prefix)
    con = _connect_ro(db)
    try:
        columns = 'blue_champs, red_champs' if teams else 'NULL, NULL'
        for blue, red, won, raw in con.execute(
            'SELECT ' + columns + ', blue_wins, participants_json FROM games' + where, params
        ):
            # Preserve the old SQL LIKE population, including mixed-schema games.
            text = (raw or '').lower()
            items = '"items"' in text or '"itemslots"' in text
            spells = '"spells"' in text
            if (require == 'items' and not items) or (require == 'spells' and not spells):
                continue
            parts = None if raw is None else json.loads(raw) if raw else []
            yield blue, red, won, parts, items, spells, bool(raw)
    finally:
        con.close()


class ParticipantReplay:
    def __init__(self, db, queue_id, patch_prefix, batch_size=512):
        self.scope = (Path(db).resolve(), queue_id, patch_prefix or None)
        self.batch_size = batch_size
        self.file = None
        self.rows = 0
        self.participant_games = 0

    def __enter__(self):
        self.file = tempfile.TemporaryFile()
        try:
            with stage('builder.participant_projection'):
                batch = []
                for blue, red, won, parts, items, spells, observable in _source(*self.scope):
                    projected = None if parts is None else [
                        {key: p[key] for key in FIELDS if key in p} for p in parts
                    ]
                    batch.append((blue, red, won, projected, items, spells, observable))
                    self.rows += 1
                    self.participant_games += observable
                    if len(batch) >= self.batch_size:
                        pickle.dump(batch, self.file, protocol=pickle.HIGHEST_PROTOCOL)
                        batch.clear()
                if batch:
                    pickle.dump(batch, self.file, protocol=pickle.HIGHEST_PROTOCOL)
                print(f'[tierlist] projected {self.rows:,} games into {self.file.tell():,} replay bytes', flush=True)
            self.token = _active.set(self)
            return self
        except BaseException:
            self.file.close()
            raise

    def __exit__(self, *exc):
        _active.reset(self.token)
        self.file.close()

    def iterate(self):
        offset = 0
        while True:
            self.file.seek(offset)
            try:
                batch = pickle.load(self.file)
            except EOFError:
                return
            offset = self.file.tell()
            yield from batch


def iter_rows(db, queue_id, patch_prefix, *, require=None, teams=False):
    replay = _active.get()
    scope = (Path(db).resolve(), queue_id, patch_prefix or None)
    source = replay.iterate() if replay is not None and replay.scope == scope else _source(*scope, teams=teams, require=require)
    try:
        for blue, red, won, parts, items, spells, observable in source:
            if (require == 'items' and not items) or (require == 'spells' and not spells):
                continue
            if teams:
                yield blue, red, won, parts
            elif parts is not None:
                yield won, parts
    finally:
        source.close()


def cached_counts(db, queue_id, patch_prefix):
    replay = _active.get()
    if replay is not None and replay.scope == (Path(db).resolve(), queue_id, patch_prefix or None):
        return replay.rows, replay.participant_games
    return None
