from pathlib import Path

from aram_nn.site import stage_memo


def test_restore_only_for_the_key_that_stored_it(tmp_path):
    cache, out = tmp_path / "cache", tmp_path / "out" / "artifact.json"
    out.parent.mkdir()
    out.write_bytes(b'{"v": 1}\n')
    assert not stage_memo.restore("stage", "k1", out, cache_dir=cache)
    stage_memo.store("stage", "k1", out, cache_dir=cache)
    out.unlink()
    assert not stage_memo.restore("stage", "k2", out, cache_dir=cache)
    assert not out.exists()
    assert stage_memo.restore("stage", "k1", out, cache_dir=cache)
    assert out.read_bytes() == b'{"v": 1}\n'
    assert not stage_memo.restore("other", "k1", out, cache_dir=cache)
    assert [p.name for p in cache.iterdir()] == ["stage.memo"]  # no temp files left


def test_broken_entries_and_unwritable_cache_cost_only_a_recompute(tmp_path):
    out = tmp_path / "artifact.json"
    out.write_bytes(b"x")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "stage.memo").write_bytes(b"k1")  # truncated: key without an artifact
    assert not stage_memo.restore("stage", "k1", out, cache_dir=cache)
    blocked = tmp_path / "file"
    blocked.write_bytes(b"")
    stage_memo.store("stage", "k1", out, cache_dir=blocked)  # must not raise
    assert not stage_memo.restore("stage", "k1", out, cache_dir=blocked)


def test_key_tracks_code_inputs_and_file_identity(tmp_path):
    code, data = tmp_path / "stage.py", tmp_path / "games.parquet"
    code.write_text("A = 1\n")
    data.write_bytes(b"12345")
    def key():
        return stage_memo.stage_key(code=[code], parquet=stage_memo.file_identity(data),
                                    params=[1, 2.5], patches=["16.23"])
    first = key()
    assert key() == first
    code.write_text("A = 2\n")
    second = key()
    assert second != first
    data.write_bytes(b"123456")
    assert key() != second
    assert stage_memo.stage_key(code=[], a=1) != stage_memo.stage_key(code=[], a=2)
