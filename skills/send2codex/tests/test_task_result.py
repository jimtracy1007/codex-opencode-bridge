from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

MODULE_PATH = Path(__file__).parents[1] / "send_message.py"
SPEC = importlib.util.spec_from_file_location("send2codex_send_message", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
send_message = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(send_message)


def _git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Bridge Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "bridge@example.test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_manifest(
    project: Path,
    *,
    task_id: str,
    allowed_files: list[str],
    read_only: bool = False,
    baseline_dirty: dict[str, str] | None = None,
    baseline_scope: dict[str, str] | None = None,
) -> Path:
    path = project / ".opencode/task-bridge" / f"{task_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": task_id,
                "project_path": str(project),
                "codex_thread_id": "019f-pinned-codex",
                "opencode_session_id": "ses_current",
                "baseline_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "baseline_dirty": baseline_dirty or {},
                "baseline_scope": baseline_scope or {},
                "allowed_files": allowed_files,
                "read_only": read_only,
                "acceptance": ["tests pass"],
                "state": "dispatched",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_result_ignores_unchanged_preexisting_dirty_file_and_accepts_allowed_change(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("user dirty\n", encoding="utf-8")
    baseline_dirty = send_message.snapshot_dirty_files(tmp_path)
    _write_manifest(
        tmp_path,
        task_id="task_result_001",
        allowed_files=["src/feature.py"],
        baseline_dirty=baseline_dirty,
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/feature.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = send_message.build_task_result(
        task_id="task_result_001",
        project_path=tmp_path,
        status="completed",
        summary="implemented",
        verification=["pytest: passed"],
        risks=[],
    )

    assert result["contract_valid"] is True
    assert result["changed_files"] == ["src/feature.py"]
    assert result["unexpected_files"] == []


def test_result_marks_scope_violation_from_real_worktree_diff(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _write_manifest(
        tmp_path,
        task_id="task_result_002",
        allowed_files=["src/allowed.py"],
    )
    (tmp_path / "unexpected.py").write_text("BAD = True\n", encoding="utf-8")

    result = send_message.build_task_result(
        task_id="task_result_002",
        project_path=tmp_path,
        status="completed",
        summary="implemented",
        verification=["pytest: passed"],
        risks=[],
    )

    assert result["contract_valid"] is False
    assert result["status"] == "scope_violation"
    assert result["unexpected_files"] == ["unexpected.py"]


def test_result_callback_uses_manifest_pinned_codex_thread(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _git_repo(tmp_path)
    _write_manifest(
        tmp_path,
        task_id="task_result_003",
        allowed_files=[],
        read_only=True,
    )
    observed: dict[str, object] = {}

    def fake_send(text: str, **kwargs: object) -> tuple[bool, str]:
        observed["text"] = text
        observed.update(kwargs)
        return True, "sent:steer:turn-test"

    monkeypatch.setattr(send_message, "send_codex_message", fake_send)

    exit_code = send_message.main(
        [
            "result",
            "--task-id",
            "task_result_003",
            "--project-path",
            str(tmp_path),
            "--status",
            "completed",
            "--summary",
            "read-only check complete",
            "--verification",
            "no files changed",
            "--json",
        ]
    )

    assert exit_code == 0
    assert observed["thread_id"] == "019f-pinned-codex"
    assert "BRIDGE_RESULT" in str(observed["text"])
    assert "task_result_003" in str(observed["text"])
    output = json.loads(capsys.readouterr().out)
    assert output["contract_valid"] is True


def test_unknown_or_stale_task_id_is_rejected(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    try:
        send_message.build_task_result(
            task_id="task_stale_404",
            project_path=tmp_path,
            status="completed",
            summary="old result",
            verification=["pytest: passed"],
            risks=[],
        )
    except RuntimeError as exc:
        assert "manifest unavailable" in str(exc)
    else:
        raise AssertionError("stale task result was accepted")


def test_completed_result_without_verification_is_not_contract_valid(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _write_manifest(
        tmp_path,
        task_id="task_result_004",
        allowed_files=[],
        read_only=True,
    )

    result = send_message.build_task_result(
        task_id="task_result_004",
        project_path=tmp_path,
        status="completed",
        summary="claimed complete",
        verification=[],
        risks=[],
    )

    assert result["contract_valid"] is False
    assert result["status"] == "incomplete_evidence"


def test_result_detects_change_to_allowed_gitignored_file(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".hidden/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore hidden"], cwd=tmp_path, check=True)
    (tmp_path / ".hidden").mkdir()
    hidden = tmp_path / ".hidden/skill.py"
    hidden.write_text("VALUE = 1\n", encoding="utf-8")
    baseline_scope = send_message.snapshot_scope_files(tmp_path, [".hidden/skill.py"])
    _write_manifest(
        tmp_path,
        task_id="task_result_ignored",
        allowed_files=[".hidden/skill.py"],
        baseline_scope=baseline_scope,
    )
    hidden.write_text("VALUE = 2\n", encoding="utf-8")

    result = send_message.build_task_result(
        task_id="task_result_ignored",
        project_path=tmp_path,
        status="completed",
        summary="updated ignored skill",
        verification=["focused tests: passed"],
        risks=[],
    )

    assert result["contract_valid"] is True
    assert result["changed_files"] == [".hidden/skill.py"]


def test_result_rejects_duplicate_callback_after_manifest_is_closed(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    manifest_path = _write_manifest(
        tmp_path,
        task_id="task_result_closed",
        allowed_files=[],
        read_only=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"] = "callback_sent"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        send_message.build_task_result(
            task_id="task_result_closed",
            project_path=tmp_path,
            status="completed",
            summary="duplicate",
            verification=["none"],
            risks=[],
        )
    except RuntimeError as exc:
        assert "not dispatchable" in str(exc)
    else:
        raise AssertionError("duplicate callback was accepted")


def test_scope_fingerprint_ignores_python_bytecode(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    cache = tmp_path / ".opencode/skills/send2codex/__pycache__"
    cache.mkdir(parents=True)
    (cache / "send_message.cpython-312.pyc").write_bytes(b"runtime")
    source = tmp_path / ".opencode/skills/send2codex/send_message.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    dependency = tmp_path / ".opencode/node_modules/pkg/index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("generated\n", encoding="utf-8")

    snapshot = send_message.snapshot_scope_files(tmp_path, [".opencode/**"])

    assert ".opencode/skills/send2codex/send_message.py" in snapshot
    assert not any("__pycache__" in path for path in snapshot)
    assert not any("node_modules" in path for path in snapshot)
