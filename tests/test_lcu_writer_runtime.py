from __future__ import annotations

import ast
import inspect
from pathlib import Path

from aram_nn.lcu.snowball import run_snowball


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts" / "lcu_collector.py"


def _function_source(name: str) -> str:
    source = COLLECTOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing function: {name}")


def test_production_commands_route_through_writer_supervisor() -> None:
    collect_source = _function_source("collect")
    snowball_source = _function_source("snowball")
    fleet_source = _function_source("snowball_workers")

    assert "WriterSupervisor" in collect_source
    assert "writer_client=client" in collect_source
    assert "_run_snowball_fleet" in snowball_source
    assert "_run_snowball_fleet" in fleet_source
    assert "run_snowball(" not in snowball_source
    assert "subprocess.Popen" not in fleet_source


def test_rpc_producer_target_receives_client_not_database_connection() -> None:
    target_source = _function_source("_snowball_rpc_worker")

    assert "writer_client=writer_client" in target_source
    assert "sqlite3.connect" not in target_source
    assert "_connect_db" not in target_source


def test_runtime_logs_do_not_emit_player_identifiers_or_lcu_response_bodies() -> None:
    snowball = (ROOT / "src" / "aram_nn" / "lcu" / "snowball.py").read_text(encoding="utf-8")
    poller = (ROOT / "src" / "aram_nn" / "lcu" / "poller.py").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts" / "mayhem_lcu_watchdog.py").read_text(encoding="utf-8")

    assert "puuid={puuid[:" not in snowball
    assert "puuid {(puuid or '')[:" not in poller
    assert "connected as {name}" not in poller
    assert "summoner_body_prefix" not in watchdog
    assert "phase_body_prefix" not in watchdog


def test_autorefresh_uses_graceful_fleet_control_not_blocking_run() -> None:
    finder = _function_source("_find_active_snowball_workers")
    stopper = _function_source("_stop_active_snowball_workers")
    launcher = _function_source("_launch_snowball_workers_subprocess")

    assert '"snowball-workers"' in finder
    assert "control_file" in stopper
    assert 'write_text("stop\\n"' in stopper
    assert "cannot safely stop" in stopper
    assert "subprocess.Popen" in launcher
    assert "subprocess.run" not in launcher
    assert "CREATE_NEW_PROCESS_GROUP" in launcher


def _snowball_run_kwargs_keys() -> set[str]:
    tree = ast.parse(COLLECTOR.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_snowball_run_kwargs":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
                    keys = set()
                    for key in stmt.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
                    return keys
    raise AssertionError("missing _snowball_run_kwargs return dict")


def test_snowball_run_kwargs_match_run_snowball_parameters() -> None:
    keys = _snowball_run_kwargs_keys()
    allowed = set(inspect.signature(run_snowball).parameters)
    assert keys <= allowed
    assert "target_queues" in keys
    assert "apex_queues" in keys
    assert "riot_queues" in keys
    assert "riot_tiers" in keys
    assert "riot_divisions" in keys
    assert "queue" not in keys
    assert "apex_queue" not in keys
    snowball_source = _function_source("snowball")
    fleet_source = _function_source("snowball_workers")
    assert "_snowball_run_kwargs(" in snowball_source
    assert "_snowball_run_kwargs(" in fleet_source


def _load_collector():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lcu_collector_under_test", COLLECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_producer_traceback_reaches_its_own_err_file(tmp_path: Path, monkeypatch) -> None:
    """A dying producer must leave evidence behind.

    Between 2026-08-27 and 2026-08-28 sixteen producers died with exitcode=1 and
    wrote nothing at all: the redirect unwinds before multiprocessing prints the
    traceback, and the real stderr is None under pythonw.  The fleet logs just
    stopped mid-crawl.
    """
    import aram_nn.lcu.snowball as snowball_module

    collector = _load_collector()
    stdout_path = tmp_path / "snowball_W02.log"
    stderr_path = tmp_path / "snowball_W02.err"

    def boom(**_kwargs: object) -> None:
        raise RuntimeError("writer pipe went away")

    monkeypatch.setattr(snowball_module, "run_snowball", boom)

    try:
        collector._snowball_rpc_worker(object(), {}, str(stdout_path), str(stderr_path))
    except RuntimeError as exc:
        assert "writer pipe went away" in str(exc)
    else:  # pragma: no cover - the worker must not swallow the failure
        raise AssertionError("the exception must still propagate so the fleet notices")

    captured = stderr_path.read_text(encoding="utf-8")
    assert "Traceback" in captured
    assert "writer pipe went away" in captured


def test_fleet_failure_message_names_the_worker() -> None:
    """pid alone is useless after the fact; the worker id points at the logs."""
    fleet_source = _function_source("_run_snowball_fleet")

    assert "worker_ids[process.pid] = worker_id" in fleet_source
    assert "snowball_{label}.err" in fleet_source


def test_fleet_restarts_a_dead_producer_instead_of_collapsing() -> None:
    """One producer's exit must not take the writer and its siblings with it.

    That behaviour cost 26 fleet launches in the 15 hours after cutover, 12 of
    them inside one 15 minute window, because a flaky LCU kills producers one at
    a time.  Restart is in place, bounded, and never re-seeds.
    """
    fleet_source = _function_source("_run_snowball_fleet")

    # The dead producer is replaced in its own slot.
    assert "processes[idx] = spawn_producer(idx, clients[idx], should_seed=False)" in fleet_source
    # Bounded, so a permanently broken producer cannot hot-loop.
    assert "_PRODUCER_MAX_RESTARTS" in fleet_source
    assert "_PRODUCER_RESTART_BACKOFF_SEC" in fleet_source
    assert "retired" in fleet_source
    # The writer staying fatal is the whole point of the asymmetry.
    assert "single DB writer exited; stopping all producers" in fleet_source
    # A producer exit no longer raises on its own.
    assert "raise RuntimeError(f\"producer failed" not in fleet_source
    # A clean finish is not a failure; only retirement escalates.
    assert "if retired:" in fleet_source
