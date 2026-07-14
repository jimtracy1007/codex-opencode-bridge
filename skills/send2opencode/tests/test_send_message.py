from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import threading
from collections.abc import Iterator
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "send_message.py"
SPEC = importlib.util.spec_from_file_location("send2opencode_send_message", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
send_message = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(send_message)


class _OpenCodeHandler(BaseHTTPRequestHandler):
    username = "opencode"
    password = "test-secret"
    project = "/workspace/novel"
    received: list[dict[str, object]] = []

    def _authorized(self) -> bool:
        raw = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return self.headers.get("Authorization") == f"Basic {raw}"

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/session":
            self._json(
                200,
                [
                    {
                        "id": "ses_other",
                        "title": "other",
                        "directory": "/workspace/other",
                        "time": {"created": 1, "updated": 99},
                    },
                    {
                        "id": "ses_old",
                        "title": "old",
                        "directory": self.project,
                        "time": {"created": 1, "updated": 10},
                    },
                    {
                        "id": "ses_current",
                        "title": "current",
                        "directory": self.project,
                        "time": {"created": 2, "updated": 20},
                    },
                ],
            )
            return
        if parsed.path == "/session/status":
            assert parse_qs(parsed.query)["directory"] == [self.project]
            self._json(200, {"ses_current": {"type": "busy"}})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/session":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self.received.append(
                {
                    "query": parse_qs(parsed.query),
                    "payload": payload,
                }
            )
            self._json(
                200,
                {
                    "id": "ses_isolated",
                    "title": payload["title"],
                    "directory": self.project,
                },
            )
            return
        if parsed.path != "/session/ses_current/prompt_async":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.received.append(
            {
                "query": parse_qs(parsed.query),
                "payload": payload,
            }
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return


@pytest.fixture
def opencode_server() -> Iterator[tuple[str, type[_OpenCodeHandler]]]:
    _OpenCodeHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenCodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        host = cast(str, address[0])
        port = cast(int, address[1])
        yield f"http://{host}:{port}", _OpenCodeHandler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_lists_only_project_sessions_newest_first(opencode_server) -> None:
    server_url, _ = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "test-secret")

    sessions = send_message.list_project_sessions(endpoint, "/workspace/novel")

    assert [session["id"] for session in sessions] == ["ses_current", "ses_old"]


def test_sends_prompt_async_to_existing_session(opencode_server) -> None:
    server_url, handler = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "test-secret")

    result = send_message.send_prompt_async(
        endpoint,
        "ses_current",
        "VISIBLE_FROM_CODEX",
        "/workspace/novel",
    )

    assert result == {
        "accepted": True,
        "transport": "http",
        "status_code": 204,
        "session_id": "ses_current",
    }
    assert handler.received == [
        {
            "query": {"directory": ["/workspace/novel"]},
            "payload": {"parts": [{"type": "text", "text": "VISIBLE_FROM_CODEX"}]},
        }
    ]


def test_creates_isolated_project_session_over_http(opencode_server) -> None:
    server_url, handler = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "test-secret")

    session = send_message.create_project_session(
        endpoint,
        "bridge:task_http_001",
        "/workspace/novel",
    )

    assert session["id"] == "ses_isolated"
    assert session["title"] == "bridge:task_http_001"
    assert handler.received == [
        {
            "query": {"directory": ["/workspace/novel"]},
            "payload": {"title": "bridge:task_http_001"},
        }
    ]


def test_reads_project_session_status(opencode_server) -> None:
    server_url, _ = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "test-secret")

    status = send_message.get_session_status(endpoint, "/workspace/novel")

    assert status == {"ses_current": {"type": "busy"}}


def test_allows_anonymous_loopback_server() -> None:
    assert send_message.credentials_from_values("opencode", None, {}) == (
        "opencode",
        "",
    )


def test_discovers_dynamic_opencode_listener_urls() -> None:
    lsof_output = """
OpenCode  79807 jim 29u IPv4 0x1 0t0 TCP 127.0.0.1:56564 (LISTEN)
OpenCode  79807 jim 43u IPv4 0x2 0t0 TCP 127.0.0.1:56582 (LISTEN)
.mimocode 59268 jim 12u IPv4 0x3 0t0 TCP 127.0.0.1:4096 (LISTEN)
"""

    urls = send_message.discover_listener_urls(lsof_output)

    assert urls == ["http://127.0.0.1:56564", "http://127.0.0.1:56582"]


def test_unauthorized_error_does_not_expose_password(opencode_server) -> None:
    server_url, _ = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "do-not-leak")

    with pytest.raises(send_message.OpenCodeClientError) as caught:
        send_message.list_project_sessions(endpoint, "/workspace/novel")

    assert "401" in str(caught.value)
    assert "do-not-leak" not in str(caught.value)


def test_rejects_server_without_matching_project_session(opencode_server) -> None:
    server_url, _ = opencode_server
    endpoint = send_message.OpenCodeEndpoint(server_url, "opencode", "test-secret")

    with pytest.raises(send_message.OpenCodeClientError, match="No OpenCode session"):
        send_message.latest_project_session(endpoint, "/workspace/missing")


def _fake_opencode(tmp_path: Path) -> tuple[Path, Path]:
    if not (tmp_path / ".git").exists():
        _git_repo(tmp_path)
    capture = tmp_path / "opencode-argv.json"
    executable = tmp_path / "opencode"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['session', 'list']:\n"
        "    print(json.dumps([\n"
        "      {'id':'ses_other','title':'other','updated':99,'directory':'/other'},\n"
        "      {'id':'ses_current','title':'current','updated':20,'directory':os.environ['FAKE_PROJECT']}\n"
        "    ]))\n"
        "    raise SystemExit(0)\n"
        "Path(os.environ['FAKE_CAPTURE']).write_text(json.dumps(args))\n"
        "print(json.dumps({'type':'text','text':'OPENCODE_VISIBLE_OK'}))\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, capture


def test_cli_transport_reuses_existing_session_without_fork(tmp_path: Path, monkeypatch) -> None:
    executable, capture = _fake_opencode(tmp_path)
    monkeypatch.setenv("FAKE_PROJECT", str(tmp_path))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))

    sessions = send_message.list_cli_sessions(str(executable), tmp_path)
    result = send_message.send_via_cli(
        str(executable),
        "ses_current",
        "VISIBLE_FROM_CODEX",
        tmp_path,
    )

    assert [session["id"] for session in sessions] == ["ses_current"]
    assert result["accepted"] is True
    assert result["exit_code"] == 0
    argv = json.loads(capture.read_text(encoding="utf-8"))
    assert argv == [
        "run",
        "--session",
        "ses_current",
        "--dir",
        str(tmp_path),
        "--format",
        "json",
        "VISIBLE_FROM_CODEX",
    ]
    assert "--fork" not in argv


def test_resolves_explicit_authenticated_http_endpoint(opencode_server) -> None:
    server_url, _ = opencode_server

    endpoint = send_message.resolve_http_endpoint(
        server_url=server_url,
        username="opencode",
        password="test-secret",
        project_path="/workspace/novel",
        env={},
    )

    assert endpoint == send_message.OpenCodeEndpoint(
        server_url,
        "opencode",
        "test-secret",
    )


def test_cli_main_sends_to_latest_project_session(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    executable, capture = _fake_opencode(tmp_path)
    monkeypatch.setenv("FAKE_PROJECT", str(tmp_path))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))

    exit_code = send_message.main(
        [
            "send",
            "DESKTOP_VISIBLE_TEST",
            "--transport",
            "cli",
            "--project-path",
            str(tmp_path),
            "--opencode-executable",
            str(executable),
            "--session-id",
            "ses_current",
            "--codex-thread-id",
            "019f-current-codex",
            "--read-only",
            "--wait",
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["session_id"] == "ses_current"
    assert result["accepted"] is True
    argv = json.loads(capture.read_text(encoding="utf-8"))
    assert "DESKTOP_VISIBLE_TEST" in argv[-1]
    assert "$send2codex" in argv[-1]
    assert ".opencode/skills/send2codex/send_message.py result --task-id" in argv[-1]


def test_cli_main_can_send_passive_notification_without_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable, capture = _fake_opencode(tmp_path)
    monkeypatch.setenv("FAKE_PROJECT", str(tmp_path))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))

    exit_code = send_message.main(
        [
            "send",
            "PASSIVE_NOTIFICATION",
            "--transport",
            "cli",
            "--project-path",
            str(tmp_path),
            "--opencode-executable",
            str(executable),
            "--no-callback",
            "--wait",
            "--json",
        ]
    )

    assert exit_code == 0
    argv = json.loads(capture.read_text(encoding="utf-8"))
    assert argv[-1] == "PASSIVE_NOTIFICATION"


def test_cli_main_dispatches_without_waiting_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable, _ = _fake_opencode(tmp_path)
    monkeypatch.setenv("FAKE_PROJECT", str(tmp_path))
    observed: dict[str, object] = {}

    def fake_dispatch(
        opencode_executable: str,
        session_id: str,
        text: str,
        project_path: str | Path,
    ) -> dict[str, object]:
        observed.update(
            executable=opencode_executable,
            session_id=session_id,
            text=text,
            project_path=project_path,
        )
        return {"accepted": True, "transport": "cli", "detached": True}

    monkeypatch.setattr(send_message, "dispatch_via_cli", fake_dispatch)

    exit_code = send_message.main(
        [
            "send",
            "ASYNC_TASK",
            "--transport",
            "cli",
            "--project-path",
            str(tmp_path),
            "--opencode-executable",
            str(executable),
            "--session-id",
            "ses_current",
            "--codex-thread-id",
            "019f-current-codex",
            "--read-only",
            "--json",
        ]
    )

    assert exit_code == 0
    assert observed["session_id"] == "ses_current"
    assert "ASYNC_TASK" in str(observed["text"])


def test_cli_main_creates_isolated_session_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable, _ = _fake_opencode(tmp_path)
    observed: dict[str, object] = {}

    def fake_new_session(
        opencode_executable: str,
        task_id: str,
        text: str,
        project_path: str | Path,
    ) -> dict[str, object]:
        observed.update(
            executable=opencode_executable,
            task_id=task_id,
            text=text,
            project_path=project_path,
        )
        return {"accepted": True, "transport": "cli", "detached": True}

    monkeypatch.setattr(send_message, "dispatch_new_session_via_cli", fake_new_session)

    exit_code = send_message.main(
        [
            "send",
            "ISOLATED_TASK",
            "--transport",
            "cli",
            "--project-path",
            str(tmp_path),
            "--opencode-executable",
            str(executable),
            "--task-id",
            "task_isolated_001",
            "--codex-thread-id",
            "019f-current-codex",
            "--read-only",
            "--json",
        ]
    )

    assert exit_code == 0
    assert observed["task_id"] == "task_isolated_001"
    assert "ISOLATED_TASK" in str(observed["text"])


def test_default_transport_uses_http_without_cli_fallback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    endpoint = send_message.OpenCodeEndpoint(
        "http://127.0.0.1:4096",
        "opencode",
        "",
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(send_message, "_http_endpoint", lambda args: endpoint)
    monkeypatch.setattr(
        send_message,
        "list_project_sessions",
        lambda endpoint, project_path: [
            {"id": "ses_desktop", "directory": str(tmp_path)}
        ],
    )

    def fake_send_prompt_async(
        endpoint: object,
        session_id: str,
        text: str,
        project_path: str | Path,
    ) -> dict[str, object]:
        observed.update(
            endpoint=endpoint,
            session_id=session_id,
            text=text,
            project_path=project_path,
        )
        return {
            "accepted": True,
            "transport": "http",
            "session_id": session_id,
        }

    monkeypatch.setattr(send_message, "send_prompt_async", fake_send_prompt_async)
    monkeypatch.setattr(
        send_message,
        "dispatch_via_cli",
        lambda *args: pytest.fail("default transport must not invoke OpenCode CLI"),
    )

    exit_code = send_message.main(
        [
            "send",
            "DESKTOP_HTTP_ONLY",
            "--project-path",
            str(tmp_path),
            "--session-id",
            "ses_desktop",
            "--no-callback",
            "--json",
        ]
    )

    assert exit_code == 0
    assert observed["endpoint"] == endpoint
    assert observed["session_id"] == "ses_desktop"
    assert observed["text"] == "DESKTOP_HTTP_ONLY"
    assert json.loads(capsys.readouterr().out)["transport"] == "http"


def test_default_http_endpoint_is_fixed_loopback_without_credentials(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_sessions(endpoint: object, project_path: str | Path) -> list[dict[str, object]]:
        observed.update(endpoint=endpoint, project_path=project_path)
        return [{"id": "ses_desktop", "directory": str(project_path)}]

    monkeypatch.setattr(send_message, "list_project_sessions", fake_sessions)

    endpoint = send_message.resolve_http_endpoint(
        server_url=None,
        username=None,
        password=None,
        project_path="/workspace/novel",
        env={},
        listener_urls=[],
    )

    assert endpoint == send_message.OpenCodeEndpoint(
        "http://127.0.0.1:4096",
        "opencode",
        "",
    )
    assert observed["endpoint"] == endpoint


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Bridge Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "bridge@example.test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)


def test_task_manifest_records_pinned_thread_scope_and_dirty_fingerprints(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("user dirty\n", encoding="utf-8")

    manifest = send_message.create_task_manifest(
        project_path=tmp_path,
        task_id="task_contract_001",
        codex_thread_id="019f-current-codex",
        opencode_session_id="ses_current",
        allowed_files=["src/feature.py"],
        read_only=False,
        acceptance=["focused tests pass", "ruff clean"],
    )

    assert manifest["task_id"] == "task_contract_001"
    assert manifest["codex_thread_id"] == "019f-current-codex"
    assert manifest["opencode_session_id"] == "ses_current"
    assert manifest["allowed_files"] == ["src/feature.py"]
    assert manifest["acceptance"] == ["focused tests pass", "ruff clean"]
    assert "tracked.txt" in manifest["baseline_dirty"]
    assert (tmp_path / ".opencode/task-bridge/task_contract_001.json").exists()


def test_task_manifest_fingerprints_allowed_gitignored_files(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".hidden/\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden/skill.py").write_text("VALUE = 1\n", encoding="utf-8")

    manifest = send_message.create_task_manifest(
        project_path=tmp_path,
        task_id="task_contract_ignored",
        codex_thread_id="019f-current-codex",
        opencode_session_id="new:task_contract_ignored",
        allowed_files=[".hidden/skill.py"],
        read_only=False,
        acceptance=["ignored file is supervised"],
    )

    assert ".hidden/skill.py" in manifest["baseline_scope"]


def test_callback_envelope_carries_task_identity_and_exact_result_command() -> None:
    manifest = {
        "task_id": "task_contract_002",
        "codex_thread_id": "019f-current-codex",
        "baseline_head": "abc123",
        "allowed_files": ["src/feature.py"],
        "read_only": False,
        "acceptance": ["tests pass"],
    }

    envelope = send_message.build_task_envelope("Implement feature", manifest)

    assert "TASK_ID: task_contract_002" in envelope
    assert "BASELINE_HEAD: abc123" in envelope
    assert "ALLOWED_FILES: src/feature.py" in envelope
    assert "ACCEPTANCE: tests pass" in envelope
    assert "send_message.py result --task-id task_contract_002" in envelope
    assert "--thread-id" not in envelope


def test_callback_task_requires_explicit_scope(tmp_path: Path, capsys) -> None:
    _git_repo(tmp_path)

    exit_code = send_message.main(
        [
            "send",
            "UNSCOPED_TASK",
            "--project-path",
            str(tmp_path),
            "--session-id",
            "ses_current",
            "--codex-thread-id",
            "019f-current-codex",
            "--json",
        ]
    )

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert "--allow-file or --read-only" in result["error"]


def test_callback_task_rejects_scope_outside_project(tmp_path: Path, capsys) -> None:
    _git_repo(tmp_path)

    exit_code = send_message.main(
        [
            "send",
            "ESCAPING_TASK",
            "--project-path",
            str(tmp_path),
            "--session-id",
            "ses_current",
            "--codex-thread-id",
            "019f-current-codex",
            "--allow-file",
            "../outside.py",
            "--json",
        ]
    )

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert "repo-relative" in result["error"]
