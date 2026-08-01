import json
import os
import subprocess
import sys
from pathlib import Path


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "vem", "--house", os.fspath(root / "house"), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def test_lifecycle(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    made = run(tmp_path, "create", "--name", "demo", os.fspath(first / ".venv"))
    assert made.returncode == 0, made.stderr
    assert (first / ".venv").is_symlink()

    status = run(tmp_path, "status", "--json")
    assert status.returncode == 0, status.stderr
    report = json.loads(status.stdout)
    env_id = report["environments"][0]["id"]
    assert report["environments"][0]["state"] == "ok"

    linked = run(tmp_path, "link", "--id", env_id, os.fspath(second / ".venv"))
    assert linked.returncode == 0, linked.stderr
    moved = run(tmp_path, "move", os.fspath(second / ".venv"), os.fspath(second / "env"))
    assert moved.returncode == 0, moved.stderr
    assert (second / "env").is_symlink()

    assert run(tmp_path, "unlink", os.fspath(first / ".venv")).returncode == 0
    assert run(tmp_path, "unlink", os.fspath(second / "env")).returncode == 0
    assert run(tmp_path, "remove", "--name", "demo").returncode == 0
    assert run(tmp_path, "status", "--json").returncode == 0


def test_doctor_rebuilds_registry(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert run(tmp_path, "create", "--name", "demo", os.fspath(project / ".venv")).returncode == 0
    (tmp_path / "house" / "registry.json").write_text("broken", encoding="utf-8")
    result = run(tmp_path, "doctor", "--rebuild-registry", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_rejects_unsafe_names(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = run(tmp_path, "create", "--name", "../bad", os.fspath(project / ".venv"))
    assert result.returncode == 2
