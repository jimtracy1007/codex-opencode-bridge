#!/usr/bin/env python3
"""Send messages to Codex or OpenCode session for cross-agent collaboration."""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}$")


def _codex_ipc_socket_path() -> Path:
    """Get the default Codex IPC socket path."""
    return Path(tempfile.gettempdir()) / "codex-ipc" / f"ipc-{os.getuid()}.sock"


def _encode_ipc_frame(message: dict[str, object]) -> bytes:
    """Encode a message as an IPC frame."""
    raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _read_ipc_frame(sock: socket.socket) -> dict[str, object]:
    """Read a single IPC frame from socket."""
    size_raw = _recv_exact(sock, 4)
    size = struct.unpack("<I", size_raw)[0]
    return json.loads(_recv_exact(sock, size).decode("utf-8"))


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Receive exactly `size` bytes from socket."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Codex IPC socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _opencode_auth_header_from_env(env: dict[str, str] | None = None) -> str:
    """Build Basic Auth header for OpenCode from environment."""
    raw_env = env or os.environ
    username = str(raw_env.get("OPENCODE_SERVER_USERNAME") or "").strip()
    password = str(raw_env.get("OPENCODE_SERVER_PASSWORD") or "")
    if not username or not password:
        return ""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _detect_opencode_listener_ports() -> list[int]:
    """Detect OpenCode listener ports from lsof."""
    try:
        output = subprocess.check_output(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    ports: list[int] = []
    seen: set[int] = set()
    for line in output.splitlines():
        if "OpenCode" not in line:
            continue
        for token in line.split():
            if token.startswith("127.0.0.1:"):
                _, _, port_text = token.rpartition(":")
            elif token.startswith("localhost:"):
                _, _, port_text = token.rpartition(":")
            else:
                continue
            try:
                port = int(port_text)
            except ValueError:
                continue
            if port not in seen:
                seen.add(port)
                ports.append(port)
    return ports


def _opencode_port_from_env_or_process(env: dict[str, str]) -> int:
    """Get OpenCode port from env or detect from running processes."""
    explicit_port = str(env.get("OPENCODE_PORT") or "").strip()
    if explicit_port:
        return int(explicit_port)
    ports = _detect_opencode_listener_ports()
    return ports[0] if ports else 4096


def _detect_opencode_session(port: int, *, env: dict[str, str] | None = None) -> str:
    """Detect active OpenCode session from API."""
    try:
        headers: dict[str, str] = {}
        auth_header = _opencode_auth_header_from_env(env)
        if auth_header:
            headers["Authorization"] = auth_header
        request = Request(f"http://127.0.0.1:{port}/session", headers=headers)
        with urlopen(request, timeout=3) as resp:
            if resp.status != 200:
                return ""
            sessions = json.loads(resp.read().decode("utf-8"))
            if not isinstance(sessions, list) or not sessions:
                return ""
            for session in reversed(sessions):
                sid = str(session.get("id") or "").strip()
                if sid.startswith("ses_"):
                    return sid
            return ""
    except Exception:
        return ""


def _codex_turn_id_from_response(response: dict[str, object]) -> str:
    """Extract turn ID from Codex IPC response."""
    result = response.get("result")
    if not isinstance(result, dict):
        return ""
    nested = result.get("result")
    if not isinstance(nested, dict):
        return ""
    turn = nested.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn.get("id") or "")
    return str(nested.get("turnId") or "")


def send_codex_message(
    text: str,
    *,
    thread_id: str | None = None,
    socket_path: str | Path | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    """Send a message to a Codex thread via IPC.
    
    Args:
        text: Message text to send
        thread_id: Codex thread ID (defaults to CODEX_THREAD_ID env)
        socket_path: Path to IPC socket (defaults to auto-detect)
        cwd: Working directory context (defaults to CWD)
        timeout_seconds: Timeout for IPC operations
        
    Returns:
        Tuple of (success, status_message)
    """
    if not thread_id:
        thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        return False, "missing_thread_id"
    
    if not socket_path:
        socket_path = _codex_ipc_socket_path()
    socket_path = Path(socket_path)
    
    if not socket_path.exists():
        return False, f"socket_missing:{socket_path}"
    
    if not cwd:
        cwd = os.getcwd()

    def request(
        sock: socket.socket,
        *,
        client_id: str,
        method: str,
        params: dict[str, object],
        version: int = 0,
    ) -> dict[str, object]:
        request_id = str(uuid.uuid4())
        sock.sendall(
            _encode_ipc_frame(
                {
                    "type": "request",
                    "requestId": request_id,
                    "sourceClientId": client_id,
                    "version": version,
                    "method": method,
                    "params": params,
                }
            )
        )
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            message = _read_ipc_frame(sock)
            if message.get("type") == "client-discovery-request":
                sock.sendall(
                    _encode_ipc_frame(
                        {
                            "type": "client-discovery-response",
                            "requestId": message.get("requestId"),
                            "response": {"canHandle": False},
                        }
                    )
                )
                continue
            if message.get("type") != "response" or message.get("requestId") != request_id:
                continue
            if message.get("resultType") == "error":
                raise RuntimeError(str(message.get("error") or "Codex IPC request failed"))
            return message
        raise TimeoutError(f"Timed out waiting for Codex IPC {method}")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.connect(str(socket_path))
            init = request(
                sock,
                client_id="initializing-client",
                method="initialize",
                params={"clientType": "send2codex"},
            )
            init_result = init.get("result")
            client_id = str(
                (init_result.get("clientId") if isinstance(init_result, dict) else "")
                or "send2codex"
            )
            cwd = str(cwd)
            input_payload = [{"type": "text", "text": text, "text_elements": []}]
            try:
                response = request(
                    sock,
                    client_id=client_id,
                    method="thread-follower-steer-turn",
                    version=1,
                    params={
                        "conversationId": thread_id,
                        "input": input_payload,
                        "attachments": [],
                        "restoreMessage": {
                            "cwd": cwd,
                            "context": {
                                "workspaceRoots": [cwd],
                                "collaborationMode": None,
                            },
                            "responsesapiClientMetadata": None,
                        },
                    },
                )
                return True, f"sent:steer:{_codex_turn_id_from_response(response) or thread_id}"
            except Exception as steer_exc:
                response = request(
                    sock,
                    client_id=client_id,
                    method="thread-follower-start-turn",
                    version=1,
                    params={
                        "conversationId": thread_id,
                        "turnStartParams": {
                            "threadId": thread_id,
                            "cwd": cwd,
                            "input": input_payload,
                            "attachments": [],
                        },
                    },
                )
                return True, (
                    f"sent:start_turn:{_codex_turn_id_from_response(response) or thread_id}; "
                    f"fallback_from={steer_exc}"
                )
    except Exception as exc:
        return False, f"failed:{exc}"


def send_opencode_message(
    text: str,
    *,
    session_id: str | None = None,
    port: int | None = None,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    """Send a message to an OpenCode session via HTTP API.
    
    Args:
        text: Message text to send
        session_id: OpenCode session ID (defaults to OPENCODE_SESSION_ID env or auto-detect)
        port: OpenCode server port (defaults to OPENCODE_PORT env or auto-detect)
        timeout_seconds: Timeout for HTTP request
        
    Returns:
        Tuple of (success, status_message)
    """
    env = os.environ
    
    if not session_id:
        session_id = env.get("OPENCODE_SESSION_ID", "").strip()
    
    disabled = env.get("MUSEWRITER_PROBE_NOTIFY_OPENCODE", "").strip().lower() in {"0", "false", "no"}
    if disabled:
        return False, "disabled"
    
    if not port:
        explicit_port = env.get("OPENCODE_PORT", "").strip()
        if explicit_port:
            port = int(explicit_port)
        else:
            ports = _detect_opencode_listener_ports()
            port = ports[0] if ports else 4096
    
    if not session_id:
        session_id = _detect_opencode_session(port)
    
    if not session_id:
        return False, "missing_session_id"
    
    base_url = f"http://127.0.0.1:{port}"
    url = f"{base_url}/session/{session_id}/prompt_async"
    
    payload = json.dumps(
        {"parts": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    
    try:
        headers = {"Content-Type": "application/json"}
        auth_header = _opencode_auth_header_from_env()
        if auth_header:
            headers["Authorization"] = auth_header
        request = Request(url, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=timeout_seconds) as response:
            if 200 <= response.status < 300:
                return True, f"sent:prompt_async:{response.status}"
            return False, f"http_{response.status}"
    except Exception as exc:
        return False, f"failed:{exc}"


def send_message(
    text: str,
    *,
    target: str = "auto",
    thread_id: str | None = None,
    session_id: str | None = None,
    socket_path: str | Path | None = None,
    port: int | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    """Send a message to Codex or OpenCode.
    
    Args:
        text: Message text to send
        target: Target type - "codex", "opencode", or "auto" (try codex first, then opencode)
        thread_id: Codex thread ID (for codex target)
        session_id: OpenCode session ID (for opencode target)
        socket_path: Codex IPC socket path (for codex target)
        port: OpenCode server port (for opencode target)
        cwd: Working directory context (for codex target)
        timeout_seconds: Timeout for operations
        
    Returns:
        Tuple of (success, status_message)
    """
    if target == "codex":
        return send_codex_message(
            text,
            thread_id=thread_id,
            socket_path=socket_path,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    elif target == "opencode":
        return send_opencode_message(
            text,
            session_id=session_id,
            port=port,
            timeout_seconds=timeout_seconds,
        )
    else:  # auto
        # Try codex first
        codex_sent, codex_status = send_codex_message(
            text,
            thread_id=thread_id,
            socket_path=socket_path,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        if codex_sent:
            return codex_sent, codex_status
        
        # Fall back to opencode
        return send_opencode_message(
            text,
            session_id=session_id,
            port=port,
            timeout_seconds=timeout_seconds,
        )


def _numeric_file_mtime(item: dict[str, object]) -> float:
    value = item.get("file_mtime")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _scan_codex_rollout_files(
    project_path: str | Path | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Scan Codex rollout files for active sessions.
    
    Args:
        project_path: Project path to filter by (defaults to CWD)
        limit: Maximum number of sessions to return
        
    Returns:
        List of session dictionaries with id, title, cwd, timestamp, file_path
    """
    if not project_path:
        project_path = os.getcwd()
    project_path = str(Path(project_path).resolve())
    
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return []
    
    sessions: list[dict[str, object]] = []
    
    # Scan all rollout-*.jsonl files
    for rollout_file in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            # Read first line (session_meta)
            with open(rollout_file, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line:
                    continue
                
                meta = json.loads(first_line)
                if meta.get("type") != "session_meta":
                    continue
                
                payload = meta.get("payload", {})
                cwd = payload.get("cwd", "")
                
                # Filter by project path
                if cwd and Path(cwd).resolve() == Path(project_path).resolve():
                    session_id = payload.get("session_id") or payload.get("id", "")
                    timestamp = meta.get("timestamp", "")
                    
                    sessions.append({
                        "id": session_id,
                        "title": payload.get("title", ""),
                        "cwd": cwd,
                        "timestamp": timestamp,
                        "file_path": str(rollout_file),
                        "file_mtime": rollout_file.stat().st_mtime,
                    })
        except (json.JSONDecodeError, OSError):
            continue
    
    # Sort by file modification time (most recent first)
    sessions.sort(key=_numeric_file_mtime, reverse=True)
    
    return sessions[:limit]


def get_codex_sessions(
    project_path: str | Path | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Get recent Codex sessions for a project.
    
    Priority: Scan rollout files first, then fall back to SQLite database.
    
    Args:
        project_path: Project path to filter by (defaults to CWD)
        limit: Maximum number of sessions to return
        
    Returns:
        List of session dictionaries with id, title, cwd, created_at_ms, updated_at_ms
    """
    # Priority 1: Scan rollout files
    rollout_sessions = _scan_codex_rollout_files(project_path, limit)
    if rollout_sessions:
        return [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "cwd": s.get("cwd", ""),
                "created_at_ms": 0,
                "updated_at_ms": int(_numeric_file_mtime(s) * 1000),
                "source": "rollout",
            }
            for s in rollout_sessions
        ]
    
    # Priority 2: Fall back to SQLite database
    import sqlite3
    
    if not project_path:
        project_path = os.getcwd()
    project_path = str(Path(project_path).resolve())
    
    db_path = Path.home() / ".codex" / "sqlite" / "state_5.sqlite"
    if not db_path.exists():
        return []
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get sessions for this project path
        cursor.execute(
            """
            SELECT id, title, cwd, created_at_ms, updated_at_ms 
            FROM threads 
            WHERE cwd = ? AND archived = 0
            ORDER BY updated_at_ms DESC 
            LIMIT ?
            """,
            (project_path, limit)
        )
        
        sessions = []
        for row in cursor.fetchall():
            sessions.append({
                "id": row["id"],
                "title": row["title"],
                "cwd": row["cwd"],
                "created_at_ms": row["created_at_ms"],
                "updated_at_ms": row["updated_at_ms"],
                "source": "sqlite",
            })
        
        conn.close()
        return sessions
    except Exception as e:
        print(f"Error reading Codex database: {e}", file=sys.stderr)
        return []


def get_latest_codex_session(
    project_path: str | Path | None = None,
) -> str | None:
    """Get the latest Codex session ID for a project.
    
    Priority: Scan rollout files first, then fall back to SQLite database.
    
    Args:
        project_path: Project path to filter by (defaults to CWD)
        
    Returns:
        Latest session ID or None if not found
    """
    sessions = get_codex_sessions(project_path, limit=1)
    session_id = sessions[0].get("id") if sessions else None
    return session_id if isinstance(session_id, str) else None


def get_mimocode_sessions(
    project_path: str | Path | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Get recent MiMoCode sessions for a project.
    
    Args:
        project_path: Project path to filter by (defaults to CWD)
        limit: Maximum number of sessions to return
        
    Returns:
        List of session dictionaries with id, title, time_created, time_updated
    """
    import sqlite3
    
    if not project_path:
        project_path = os.getcwd()
    project_path = str(Path(project_path).resolve())
    
    db_path = Path.home() / ".local" / "share" / "mimocode" / "mimocode.db"
    if not db_path.exists():
        return []
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # First find project ID
        cursor.execute(
            "SELECT id FROM project WHERE worktree = ?",
            (project_path,)
        )
        project_row = cursor.fetchone()
        if not project_row:
            conn.close()
            return []
        
        project_id = project_row["id"]
        
        # Get sessions for this project
        cursor.execute(
            """
            SELECT id, title, time_created, time_updated 
            FROM session 
            WHERE project_id = ? 
            ORDER BY time_updated DESC 
            LIMIT ?
            """,
            (project_id, limit)
        )
        
        sessions = []
        for row in cursor.fetchall():
            sessions.append({
                "id": row["id"],
                "title": row["title"],
                "time_created": row["time_created"],
                "time_updated": row["time_updated"],
            })
        
        conn.close()
        return sessions
    except Exception as e:
        print(f"Error reading MiMoCode database: {e}", file=sys.stderr)
        return []


def get_latest_mimocode_session(
    project_path: str | Path | None = None,
) -> str | None:
    """Get the latest MiMoCode session ID for a project.
    
    Args:
        project_path: Project path to filter by (defaults to CWD)
        
    Returns:
        Latest session ID or None if not found
    """
    sessions = get_mimocode_sessions(project_path, limit=1)
    session_id = sessions[0].get("id") if sessions else None
    return session_id if isinstance(session_id, str) else None


def _git_output(project_path: str | Path, *args: str) -> str:
    project = Path(project_path).resolve()
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(project),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Git result verification failed: {exc}") from exc
    return completed.stdout.rstrip("\n")


def _dirty_paths(project_path: str | Path) -> list[str]:
    output = _git_output(project_path, "status", "--porcelain=v1", "--untracked-files=all")
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(".opencode/task-bridge/"):
            continue
        paths.append(path)
    return sorted(set(paths))


def _path_fingerprint(project_path: str | Path, relative_path: str) -> str:
    path = Path(project_path).resolve() / relative_path
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_dirty_files(project_path: str | Path) -> dict[str, str]:
    return {
        path: _path_fingerprint(project_path, path)
        for path in _dirty_paths(project_path)
    }


def snapshot_scope_files(
    project_path: str | Path,
    patterns: list[str],
) -> dict[str, str]:
    project = Path(project_path).resolve()
    files: set[Path] = set()
    for pattern in patterns:
        for candidate in project.glob(pattern):
            if candidate.is_file():
                files.add(candidate)
            elif candidate.is_dir():
                files.update(path for path in candidate.rglob("*") if path.is_file())
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
        if not {"__pycache__", "node_modules", ".git", ".venv"}.intersection(path.parts)
        and path.suffix != ".pyc"
        and not path.relative_to(project).as_posix().startswith(".opencode/task-bridge/")
    }


def _load_task_manifest(task_id: str, project_path: str | Path) -> tuple[Path, dict[str, object]]:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise RuntimeError("Invalid task_id")
    project = Path(project_path).resolve()
    path = project / ".opencode" / "task-bridge" / f"{task_id}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Task manifest unavailable: {task_id}") from exc
    if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
        raise RuntimeError(f"Task manifest identity mismatch: {task_id}")
    recorded_project = manifest.get("project_path")
    if not isinstance(recorded_project, str) or Path(recorded_project).resolve() != project:
        raise RuntimeError(f"Task manifest project mismatch: {task_id}")
    if manifest.get("state") != "dispatched":
        raise RuntimeError(
            f"Task manifest is not dispatchable: {task_id}:{manifest.get('state')}"
        )
    return path, manifest


def _changed_files_since_manifest(
    project_path: str | Path,
    manifest: dict[str, object],
) -> tuple[str, list[str]]:
    project = Path(project_path).resolve()
    current_head = _git_output(project, "rev-parse", "HEAD")
    baseline_head = manifest.get("baseline_head")
    baseline_dirty_raw = manifest.get("baseline_dirty")
    baseline_dirty = (
        {str(key): str(value) for key, value in baseline_dirty_raw.items()}
        if isinstance(baseline_dirty_raw, dict)
        else {}
    )
    current_dirty = snapshot_dirty_files(project)
    changed = {
        path
        for path in set(baseline_dirty) | set(current_dirty)
        if baseline_dirty.get(path) != current_dirty.get(path)
    }
    baseline_scope_raw = manifest.get("baseline_scope")
    baseline_scope = (
        {str(key): str(value) for key, value in baseline_scope_raw.items()}
        if isinstance(baseline_scope_raw, dict)
        else {}
    )
    allowed_raw = manifest.get("allowed_files")
    allowed_files = [str(item) for item in allowed_raw] if isinstance(allowed_raw, list) else []
    scope_patterns = [".codex/**", ".opencode/**"] if bool(manifest.get("read_only")) else allowed_files
    current_scope = snapshot_scope_files(project, scope_patterns)
    changed.update(
        path
        for path in set(baseline_scope) | set(current_scope)
        if baseline_scope.get(path) != current_scope.get(path)
    )
    if isinstance(baseline_head, str) and baseline_head and baseline_head != current_head:
        committed = _git_output(project, "diff", "--name-only", f"{baseline_head}..{current_head}")
        changed.update(path for path in committed.splitlines() if path)
    return current_head, sorted(changed)


def _path_allowed(path: str, allowed_files: list[str]) -> bool:
    for allowed in allowed_files:
        normalized = allowed.rstrip("/")
        if path == normalized or path.startswith(f"{normalized}/") or fnmatch.fnmatch(path, allowed):
            return True
    return False


def build_task_result(
    *,
    task_id: str,
    project_path: str | Path,
    status: str,
    summary: str,
    verification: list[str],
    risks: list[str],
) -> dict[str, object]:
    _, manifest = _load_task_manifest(task_id, project_path)
    current_head, changed_files = _changed_files_since_manifest(project_path, manifest)
    allowed_raw = manifest.get("allowed_files")
    allowed_files = [str(item) for item in allowed_raw] if isinstance(allowed_raw, list) else []
    read_only = bool(manifest.get("read_only"))
    unexpected_files = (
        changed_files
        if read_only
        else [path for path in changed_files if not _path_allowed(path, allowed_files)]
    )
    contract_valid = not unexpected_files and (status != "completed" or bool(verification))
    effective_status = status
    if unexpected_files:
        effective_status = "scope_violation"
    elif status == "completed" and not verification:
        effective_status = "incomplete_evidence"
    return {
        "type": "BRIDGE_RESULT",
        "task_id": task_id,
        "status": effective_status,
        "contract_valid": contract_valid,
        "project_path": str(Path(project_path).resolve()),
        "codex_thread_id": manifest.get("codex_thread_id"),
        "opencode_session_id": manifest.get("opencode_session_id"),
        "baseline_head": manifest.get("baseline_head"),
        "current_head": current_head,
        "allowed_files": allowed_files,
        "read_only": read_only,
        "changed_files": changed_files,
        "unexpected_files": unexpected_files,
        "acceptance": manifest.get("acceptance", []),
        "summary": summary,
        "verification": verification,
        "remaining_risks": risks,
    }


def _render_task_result(result: dict[str, object]) -> str:
    return "BRIDGE_RESULT\n" + json.dumps(result, ensure_ascii=False, indent=2)


def _record_task_callback(
    manifest_path: Path,
    manifest: dict[str, object],
    result: dict[str, object],
    callback_status: str,
) -> None:
    manifest["state"] = "callback_sent"
    manifest["callback_status"] = callback_status
    manifest["result"] = result
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send messages to Codex or OpenCode for cross-agent collaboration."
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Send command
    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument(
        "message",
        help="Message text to send"
    )
    send_parser.add_argument(
        "--target",
        choices=["auto", "codex", "opencode"],
        default="auto",
        help="Target: codex, opencode, or auto (default: auto)"
    )
    send_parser.add_argument(
        "--thread-id",
        default=None,
        help="Codex thread ID (defaults to CODEX_THREAD_ID env or auto-detect)"
    )
    send_parser.add_argument(
        "--session-id",
        default=None,
        help="OpenCode session ID (defaults to OPENCODE_SESSION_ID env or auto-detect)"
    )
    send_parser.add_argument(
        "--socket-path",
        default=None,
        help="Codex IPC socket path (defaults to auto-detect)"
    )
    send_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="OpenCode server port (defaults to OPENCODE_PORT env or auto-detect)"
    )
    send_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory context for Codex (defaults to CWD)"
    )
    send_parser.add_argument(
        "--project-path",
        default=None,
        help="Project path for auto-detecting sessions (defaults to CWD)"
    )
    send_parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Timeout in seconds (default: 20)"
    )
    send_parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    result_parser = subparsers.add_parser(
        "result",
        help="Return a manifest-bound task result to its pinned Codex task",
    )
    result_parser.add_argument("--task-id", required=True)
    result_parser.add_argument("--project-path", default=None)
    result_parser.add_argument(
        "--status",
        choices=["completed", "blocked", "failed"],
        required=True,
    )
    result_parser.add_argument("--summary", required=True)
    result_parser.add_argument("--verification", action="append", default=[])
    result_parser.add_argument("--risk", action="append", default=[])
    result_parser.add_argument("--timeout", type=float, default=20.0)
    result_parser.add_argument("--json", action="store_true")
    
    # Codex sessions command
    codex_sessions_parser = subparsers.add_parser("codex-sessions", help="List recent Codex sessions")
    codex_sessions_parser.add_argument(
        "--project-path",
        default=None,
        help="Project path (defaults to CWD)"
    )
    codex_sessions_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of sessions to return (default: 10)"
    )
    codex_sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )
    
    # Latest Codex session command
    codex_latest_parser = subparsers.add_parser("codex-latest", help="Get latest Codex session ID")
    codex_latest_parser.add_argument(
        "--project-path",
        default=None,
        help="Project path (defaults to CWD)"
    )
    codex_latest_parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )
    
    # MiMoCode sessions command
    mimocode_sessions_parser = subparsers.add_parser("mimocode-sessions", help="List recent MiMoCode sessions")
    mimocode_sessions_parser.add_argument(
        "--project-path",
        default=None,
        help="Project path (defaults to CWD)"
    )
    mimocode_sessions_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of sessions to return (default: 10)"
    )
    mimocode_sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )
    
    # Latest MiMoCode session command
    mimocode_latest_parser = subparsers.add_parser("mimocode-latest", help="Get latest MiMoCode session ID")
    mimocode_latest_parser.add_argument(
        "--project-path",
        default=None,
        help="Project path (defaults to CWD)"
    )
    mimocode_latest_parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )
    
    args = parser.parse_args(argv)
    
    if args.command == "result":
        project_path = Path(args.project_path or os.getcwd()).resolve()
        try:
            manifest_path, manifest = _load_task_manifest(args.task_id, project_path)
            result = build_task_result(
                task_id=args.task_id,
                project_path=project_path,
                status=args.status,
                summary=args.summary,
                verification=args.verification,
                risks=args.risk,
            )
            thread_id = result.get("codex_thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Task manifest is missing pinned codex_thread_id")
            success, callback_status = send_codex_message(
                _render_task_result(result),
                thread_id=thread_id,
                cwd=project_path,
                timeout_seconds=args.timeout,
            )
            if success:
                _record_task_callback(manifest_path, manifest, result, callback_status)
            output = {**result, "success": success, "callback_status": callback_status}
        except RuntimeError as exc:
            output = {
                "type": "BRIDGE_RESULT",
                "task_id": args.task_id,
                "success": False,
                "contract_valid": False,
                "error": str(exc),
            }
            success = False
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif success:
            print(f"sent:{args.task_id}:{output['status']}")
        else:
            print(f"failed:{args.task_id}:{output.get('error') or output.get('callback_status')}", file=sys.stderr)
        return 0 if success else 1
    if args.command == "codex-sessions":
        sessions = get_codex_sessions(args.project_path, args.limit)
        if args.json:
            print(json.dumps(sessions, ensure_ascii=False, indent=2))
        else:
            if sessions:
                print(f"Recent Codex sessions for {args.project_path or os.getcwd()}:")
                for session in sessions:
                    print(f"  {session['id']}: {session['title']}")
            else:
                print("No Codex sessions found")
        return 0
    elif args.command == "codex-latest":
        session_id = get_latest_codex_session(args.project_path)
        if args.json:
            print(json.dumps({"session_id": session_id}, ensure_ascii=False, indent=2))
        else:
            if session_id:
                print(session_id)
            else:
                print("No Codex session found", file=sys.stderr)
        return 0 if session_id else 1
    elif args.command == "mimocode-sessions":
        sessions = get_mimocode_sessions(args.project_path, args.limit)
        if args.json:
            print(json.dumps(sessions, ensure_ascii=False, indent=2))
        else:
            if sessions:
                print(f"Recent MiMoCode sessions for {args.project_path or os.getcwd()}:")
                for session in sessions:
                    print(f"  {session['id']}: {session['title']}")
            else:
                print("No MiMoCode sessions found")
        return 0
    elif args.command == "mimocode-latest":
        session_id = get_latest_mimocode_session(args.project_path)
        if args.json:
            print(json.dumps({"session_id": session_id}, ensure_ascii=False, indent=2))
        else:
            if session_id:
                print(session_id)
            else:
                print("No MiMoCode session found", file=sys.stderr)
        return 0 if session_id else 1
    elif args.command == "send":
        # Auto-detect thread_id if not provided
        thread_id = args.thread_id
        if not thread_id and args.target in ["codex", "auto"]:
            thread_id = get_latest_codex_session(args.project_path)
        
        success, status = send_message(
            args.message,
            target=args.target,
            thread_id=thread_id,
            session_id=args.session_id,
            socket_path=args.socket_path,
            port=args.port,
            cwd=args.cwd,
            timeout_seconds=args.timeout,
        )
        
        if args.json:
            result = {
                "success": success,
                "status": status,
                "message": args.message,
                "target": args.target,
                "thread_id": thread_id,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if success:
                print(f"✓ {status}")
            else:
                print(f"✗ {status}", file=sys.stderr)
        
        return 0 if success else 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
