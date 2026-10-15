import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from aram_nn.gamedata import patch_predicate
from aram_nn.site import analysis_cache, participant_replay, static_publish
from aram_nn.site.build_timing import publish_report, stage
from aram_nn.site.champion_matrix import champion_matrix
from aram_nn.site.db import insert_public_games


def make_db(path):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE games(queue_id, patch TEXT, blue_champs, red_champs, blue_wins, participants_json)')
    for i, parts in enumerate([None, '', '[]', json.dumps([
        dict(championId=1, teamId=100, augments=[10, 20], items=[100], spells=[4, 6], stats={'damage': 900}),
        dict(championId=6, teamId=200, itemSlots=[200]),
    ])]):
        con.execute('INSERT INTO games VALUES(2400,?,?,?,?,?)',
                    ('16.20.1', '[1,2,3,4,5]', '[6,7,8,9,10]', i % 2, parts))
    con.commit()
    con.close()


@pytest.mark.parametrize('prefix', ['16.20', '16.2', '16.20.', '16.29', '16._', 'ALPHA'])
def test_patch_range_preserves_like_population_and_uses_index(prefix):
    con = sqlite3.connect(':memory:')
    con.executescript('CREATE TABLE games(queue_id INTEGER, patch TEXT); CREATE INDEX scoped ON games(queue_id,patch);')
    patches = ['16.2', '16.20', '16.20.1', '16.200', '16.21', '16.29', '16.290', '16.3', '16.9', 'alpha', 'ALPHA']
    con.executemany('INSERT INTO games VALUES(2400,?)', [(p,) for p in patches])
    clause, params = patch_predicate(prefix)
    actual = con.execute('SELECT patch FROM games WHERE queue_id=2400 AND '+clause, params).fetchall()
    expected = con.execute('SELECT patch FROM games WHERE queue_id=2400 AND patch LIKE ?', [prefix+'%']).fetchall()
    assert actual == expected
    if prefix[0].isdigit() and '_' not in prefix:
        plan = str(con.execute('EXPLAIN QUERY PLAN SELECT patch FROM games WHERE queue_id=2400 AND '+clause, params).fetchall())
        assert 'patch>?' in plan and 'patch<?' in plan


def test_replay_preserves_filters_and_is_bounded_and_build_local(tmp_path, monkeypatch):
    db = tmp_path / 'games.db'
    make_db(db)
    expected = {require: list(participant_replay.iter_rows(db, 2400, '16.20', require=require))
                for require in (None, 'items', 'spells')}
    calls = []
    original = participant_replay._source
    def source(*args, **kwargs):
        calls.append(args)
        yield from original(*args, **kwargs)
    monkeypatch.setattr(participant_replay, '_source', source)
    with participant_replay.ParticipantReplay(db, 2400, '16.20', batch_size=1) as replay:
        assert replay.rows == 4
        for require, rows in expected.items():
            actual = list(participant_replay.iter_rows(db, 2400, '16.20', require=require))
            for row in rows:
                for p in row[1]:
                    p.pop('stats', None)
            assert actual == rows
        # Two consumers can interleave without advancing each other's cursor.
        a = participant_replay.iter_rows(db, 2400, '16.20')
        b = participant_replay.iter_rows(db, 2400, '16.20')
        assert next(a) == next(b)
        assert list(a) == list(b)
        assert len(calls) == 1
        assert list(participant_replay.iter_rows(db, 450, '16.20')) == []
        assert len(calls) == 2
    assert replay.file.closed
    assert participant_replay._active.get() is None


def test_replay_closes_failed_projection(tmp_path):
    db = tmp_path / 'games.db'
    make_db(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE games SET participants_json='invalid'")
    replay = participant_replay.ParticipantReplay(db, 2400, '16.20')
    with pytest.raises(ValueError):
        with replay:
            pass
    assert replay.file.closed
    assert participant_replay._active.get() is None


def test_analysis_cache_invalidation_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv('ARAM_SITE_ANALYSIS_CACHE', str(tmp_path/'cache'))
    monkeypatch.setattr(analysis_cache, 'source_digest', lambda root: 'source-v1')
    data = tmp_path/'pool'
    data.write_bytes(b'input')
    payload = tmp_path/'payload.json'
    payload.write_text(json.dumps({'champs': {'1': {'name': 'A', 'comp': {'damage': 1}}}, 'total_games': 1}))
    out = tmp_path/'out.json'
    calls = []
    @analysis_cache.cached_analysis('test', ('name', 'comp'))
    def build(data, payload, out, option=1):
        calls.append(option)
        out.write_text(json.dumps({'kind': 'test', 'champs': {}, 'option': option}))
    build(data, payload, out)
    out.unlink()
    build(data, payload, out)
    assert calls == [1] and out.exists()
    # A growing tier list is irrelevant when the vectors/names and pool match.
    d = json.loads(payload.read_text());d['total_games'] = 2;payload.write_text(json.dumps(d))
    build(data, payload, out)
    assert calls == [1]
    d['champs']['1']['comp']['damage'] = 2;payload.write_text(json.dumps(d))
    build(data, payload, out)
    data.write_bytes(b'new-input')
    build(data, payload, out)
    build(data, payload, out, option=2)
    monkeypatch.setattr(analysis_cache, 'source_digest', lambda root: 'source-v2')
    build(data, payload, out, option=2)
    assert calls == [1, 1, 1, 2, 2]
    for p in (tmp_path/'cache').glob('*.json'):
        p.write_text('corrupted')
    build(data, payload, out, option=2)
    assert calls[-2:] == [2, 2] and len(calls) == 6


def test_sparse_matrix_preserves_mirror_pick_assignment_and_lr_predictions():
    from sklearn.linear_model import LogisticRegression
    blue = [[0,1,2,3,4], [1,2,3,4,5], [0,2,4,6,8], [0,0,1,2,3]] * 10
    red = [[5,6,7,8,9], [0,2,6,7,9], [1,3,5,7,9], [0,0,4,5,6]] * 10
    dataset = SimpleNamespace(blue=blue, red=red)
    class Dataset:
        def __len__(self): return len(blue)
    dataset = Dataset();dataset.blue=blue;dataset.red=red
    dense = np.zeros((len(blue),10))
    for i, (b,r) in enumerate(zip(blue,red)):
        dense[i,b] = 1
        dense[i,r] = -1
    sparse = champion_matrix(dataset,10)
    np.testing.assert_array_equal(sparse.toarray(),dense)
    y = np.arange(len(blue)) % 2
    a = LogisticRegression().fit(dense,y).predict_proba(dense)
    b = LogisticRegression().fit(sparse,y).predict_proba(sparse)
    np.testing.assert_allclose(a,b,rtol=1e-7,atol=1e-7)


def test_timing_report_survives_failure_and_includes_substages(tmp_path):
    path = tmp_path/'timings.json'
    @publish_report
    def fail(*, timings_path):
        with stage('builder.stage'):
            raise RuntimeError('failed build')
    with pytest.raises(RuntimeError):
        fail(timings_path=path)
    rows = json.loads(path.read_text())['stages']
    assert [r['stage'] for r in rows] == ['builder.stage', 'publisher.total']
    assert all(r['seconds'] >= 0 and r['status'] == 'failed' for r in rows)


def test_skipped_publish_never_waits_for_resources(tmp_path, monkeypatch):
    db = tmp_path/'games.db'
    insert_public_games(db, [])
    def forbidden(*args, **kwargs):
        raise AssertionError('skip must not request heavy-job admission')
    monkeypatch.setattr(static_publish, 'site_build_admission', forbidden)
    report = tmp_path/'timings.json'
    report.write_text('previous actual build')
    result = static_publish.publish_static_site_once(db=db, threshold=1000, state_path=tmp_path/'state.json', timings_path=report)
    assert not result['would_publish']
    assert report.read_text() == 'previous actual build'


def test_team_training_range_keeps_scope_and_chronological_order(tmp_path):
    from aram_nn.site.team_score_training import load_team_games
    db = tmp_path/'teams.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE games(game_id, queue_id, patch, created_ms, blue_champs, red_champs, blue_wins)')
        con.executemany('INSERT INTO games VALUES(?,?,?,?,?,?,?)', [
            (str(i), queue, patch, created, '[1,2,3,4,5]', '[6,7,8,9,10]', 1)
            for i, (queue,patch,created) in enumerate([
                (2400,'16.20.1',20), (2400,'16.20.2',10), (450,'16.20.1',1), (2400,'16.19.1',2)])
        ])
    assert [g.created_ms for g in load_team_games(db, queue_id=2400, patch_prefix='16.20')] == [10,20]


def test_failed_timing_write_does_not_mask_success(tmp_path, monkeypatch):
    @publish_report
    def build(*, timings_path):
        with stage('publisher.tier_list'):
            pass
        return {'published': True}
    def deny(*args, **kwargs):
        raise OSError('unwritable metrics path')
    monkeypatch.setattr(type(tmp_path), 'replace', deny)
    assert build(timings_path=tmp_path/'timings.json')['published']


@pytest.mark.parametrize('quantile', [0, 0.333, 0.667, 1])
def test_compact_archetype_quantiles_are_exactly_equal(quantile):
    import random
    from ablation_champ_archetype_persistence import (
        COMP_FIT_DEFS, COMP_STAT_KEYS, archetype_scores, calibrate_thresholds, accumulate,
    )
    from aram_nn.site.archetype_thresholds import score_buffers, thresholds_from_scores, accumulate_scores
    rng = random.Random(7)
    # Include mirror/repeated picks and a champion absent from capability data.
    dataset = SimpleNamespace(blue=[[rng.randrange(12) for _ in range(5)] for _ in range(137)],
                              red=[[rng.randrange(12) for _ in range(5)] for _ in range(137)])
    indices = {i: 100+i for i in range(12)}
    cap = {100+i: {key: rng.choice([0., .25, .5, .75, 1.]) for key in COMP_STAT_KEYS}
           for i in range(11)}
    expected = calibrate_thresholds(dataset, indices, cap, quantile)
    scores = score_buffers(dataset, indices, cap, score=archetype_scores, archetypes=COMP_FIT_DEFS)
    actual = thresholds_from_scores(scores, quantile)
    assert actual == expected
    residual = np.asarray([rng.uniform(-1, 1) for _ in dataset.blue])
    sums, counts, _ = accumulate(dataset, residual, indices, cap, expected)
    actual_sums, actual_counts = accumulate_scores(dataset, residual, scores, actual, len(indices))
    assert actual_sums == sums
    assert actual_counts == counts
    empty = SimpleNamespace(blue=[], red=[])
    empty_scores = score_buffers(empty, indices, cap, score=archetype_scores, archetypes=COMP_FIT_DEFS)
    assert thresholds_from_scores(empty_scores, quantile) == {}
    assert accumulate_scores(empty, [], empty_scores, {}, len(indices)) == ({}, {})
