import ast
from pathlib import Path

from apps.automation.automated_stream_manager import AutomatedStreamManager


def test_run_progress_tracks_active_stage():
    manager = AutomatedStreamManager()

    manager._start_run_status(forced=True, forced_period_id="period-1")
    manager._update_run_stage("matching", message="Matching streams", current=25, total=100)

    status = manager.get_run_status()

    assert status["active"] is True
    assert status["status"] == "running"
    assert status["stage"] == "matching"
    assert status["stage_label"] == "Matching"
    assert status["current"] == 25
    assert status["total"] == 100
    assert status["percent"] == 25
    assert status["forced"] is True
    assert status["forced_period_id"] == "period-1"
    assert status["stages"][0]["status"] == "completed"
    assert status["stages"][4]["status"] == "running"


def test_skipped_stage_keeps_run_active_until_finish():
    manager = AutomatedStreamManager()

    manager._start_run_status()
    manager._update_run_stage("m3u_refresh", status="skipped", message="No refresh needed")

    status = manager.get_run_status()
    assert status["active"] is True
    assert status["status"] == "running"
    assert status["stages"][2]["status"] == "skipped"

    manager._finish_run_status("completed", "Done")
    status = manager.get_run_status()
    assert status["active"] is False
    assert status["status"] == "completed"
    assert status["message"] == "Done"


def test_automation_duration_start_variables_are_defined_before_use():
    source_path = (
        Path(__file__).resolve().parents[1]
        / "apps"
        / "automation"
        / "automated_stream_manager.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    manager_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutomatedStreamManager"
    )
    run_cycle = next(
        node
        for node in manager_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_automation_cycle"
    )

    assignments = {}
    usages = []

    for node in ast.walk(run_cycle):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_started"):
                    assignments.setdefault(target.id, []).append(node.lineno)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id.endswith("_started"):
                assignments.setdefault(target.id, []).append(node.lineno)
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and isinstance(node.right, ast.Name)
            and node.right.id.endswith("_started")
        ):
            usages.append((node.right.id, node.lineno))

    missing = [
        f"{name} used at line {line}"
        for name, line in usages
        if not any(assigned_line < line for assigned_line in assignments.get(name, []))
    ]

    assert missing == []
