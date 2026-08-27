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
