#!/usr/bin/env python3
"""Send tasks to an existing authenticated OpenCode session."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Mapping, NamedTuple
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class OpenCodeClientError(RuntimeError):
    """Raised when the OpenCode transport contract cannot be satisfied."""


class OpenCodeEndpoint(NamedTuple):
    base_url: str
    username: str
    password: str


_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}$")


def credentials_from_values(
    username: str | None,
    password: str | None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    source = env if env is not None else os.environ
    resolved_username = username or source.get("OPENCODE_SERVER_USERNAME") or "opencode"
    resolved_password = password or source.get("OPENCODE_SERVER_PASSWORD") or ""
    return resolved_username, resolved_password


def _request(
    endpoint: OpenCodeEndpoint,
    path: str,
    *,
    method: str = "GET",
    query: Mapping[str, str] | None = None,
    payload: object | None = None,
) -> tuple[int, object | None]:
    url = f"{endpoint.base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    headers: dict[str, str] = {}
    if endpoint.password:
        token = base64.b64encode(
            f"{endpoint.username}:{endpoint.password}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise OpenCodeClientError(f"OpenCode HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise OpenCodeClientError(f"OpenCode connection failed: {exc.reason}") from exc
    if not body:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenCodeClientError("OpenCode returned invalid JSON") from exc


def _updated_at(session: dict[str, object]) -> int:
    time_value = session.get("time")
    if not isinstance(time_value, dict):
        return 0
    updated = time_value.get("updated")
    return int(updated) if isinstance(updated, (int, float)) else 0


def discover_listener_urls(lsof_output: str) -> list[str]:
    """Extract dynamic loopback listeners owned by OpenCode Desktop."""
    urls: list[str] = []
    for line in lsof_output.splitlines():
        fields = line.split()
        if not fields or fields[0] != "OpenCode":
            continue
        for field in fields:
            if not field.startswith("127.0.0.1:"):
                continue
            _, _, port = field.rpartition(":")
            if port.isdigit():
                url = f"http://127.0.0.1:{port}"
                if url not in urls:
                    urls.append(url)
    return urls


def _desktop_listener_urls() -> list[str]:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeClientError(f"OpenCode listener discovery failed: {exc}") from exc
    return discover_listener_urls(completed.stdout)


def resolve_http_endpoint(
    *,
    server_url: str | None,
    username: str | None,
    password: str | None,
    project_path: str | Path,
    env: Mapping[str, str] | None = None,
    listener_urls: list[str] | None = None,
) -> OpenCodeEndpoint:
    """Resolve and authenticate an OpenCode HTTP endpoint without caching its port."""
    source = env if env is not None else os.environ
    resolved_username, resolved_password = credentials_from_values(
        username,
        password,
        source,
    )
    explicit_url = (
        server_url
        or source.get("OPENCODE_SERVER_URL")
        or "http://127.0.0.1:4096"
    )
    candidates = [explicit_url]
    for candidate in candidates:
        if not candidate:
            continue
        endpoint = OpenCodeEndpoint(
            candidate.rstrip("/"),
            resolved_username,
            resolved_password,
        )
        try:
            list_project_sessions(endpoint, project_path)
        except OpenCodeClientError:
            continue
        return endpoint
    raise OpenCodeClientError(
        f"No authenticated OpenCode API endpoint found among {len(candidates)} candidate(s)"
    )


def list_project_sessions(
    endpoint: OpenCodeEndpoint,
    project_path: str | Path,
) -> list[dict[str, object]]:
    project = str(Path(project_path).resolve())
    _, payload = _request(endpoint, "/session", query={"directory": project})
    if not isinstance(payload, list):
        raise OpenCodeClientError("OpenCode /session response must be a list")
    sessions = [
        session
        for session in payload
        if isinstance(session, dict)
        and isinstance(session.get("directory"), str)
        and str(Path(str(session["directory"])).resolve()) == project
    ]
    sessions.sort(key=_updated_at, reverse=True)
    return sessions


def latest_project_session(
    endpoint: OpenCodeEndpoint,
    project_path: str | Path,
) -> dict[str, object]:
    sessions = list_project_sessions(endpoint, project_path)
    if not sessions:
        raise OpenCodeClientError(
            f"No OpenCode session found for project: {Path(project_path).resolve()}"
        )
    return sessions[0]


def create_project_session(
    endpoint: OpenCodeEndpoint,
    title: str,
    project_path: str | Path,
) -> dict[str, object]:
    project = str(Path(project_path).resolve())
    _, payload = _request(
        endpoint,
        "/session",
        method="POST",
        query={"directory": project},
        payload={"title": title},
    )
    if not isinstance(payload, dict):
        raise OpenCodeClientError("OpenCode session creation response must be an object")
    _session_id(payload)
    return payload


def list_cli_sessions(
    opencode_executable: str,
    project_path: str | Path,
) -> list[dict[str, object]]:
    project = Path(project_path).resolve()
    try:
        completed = subprocess.run(
            [opencode_executable, "session", "list", "--format", "json"],
            cwd=str(project),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeClientError(f"OpenCode session discovery failed: {exc}") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OpenCodeClientError("OpenCode session list returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise OpenCodeClientError("OpenCode session list must be a list")
    sessions = [
        session
        for session in payload
        if isinstance(session, dict)
        and session.get("directory") == str(project)
    ]
    sessions.sort(
        key=lambda session: int(session.get("updated", 0))
        if isinstance(session.get("updated"), (int, float))
        else 0,
        reverse=True,
    )
    return sessions


def send_via_cli(
    opencode_executable: str,
    session_id: str,
    text: str,
    project_path: str | Path,
) -> dict[str, object]:
    project = Path(project_path).resolve()
    command = _cli_send_command(opencode_executable, session_id, text, project)
    try:
        completed = subprocess.run(
            command,
            cwd=str(project),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OpenCodeClientError(f"OpenCode CLI failed to start: {exc}") from exc
    return {
        "accepted": completed.returncode == 0,
        "transport": "cli",
        "session_id": session_id,
        "exit_code": completed.returncode,
        "output": completed.stdout[-12000:],
        "error": completed.stderr[-4000:],
    }


def _cli_send_command(
    opencode_executable: str,
    session_id: str,
    text: str,
    project_path: str | Path,
) -> list[str]:
    return [
        opencode_executable,
        "run",
        "--session",
        session_id,
        "--dir",
        str(Path(project_path).resolve()),
        "--format",
        "json",
        text,
    ]


def dispatch_via_cli(
    opencode_executable: str,
    session_id: str,
    text: str,
    project_path: str | Path,
) -> dict[str, object]:
    """Start an OpenCode task without blocking; completion returns via send2codex."""
    project = Path(project_path).resolve()
    command = _cli_send_command(opencode_executable, session_id, text, project)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise OpenCodeClientError(f"OpenCode CLI failed to start: {exc}") from exc
    return {
        "accepted": True,
        "transport": "cli",
        "session_id": session_id,
        "detached": True,
        "pid": process.pid,
    }


def _cli_new_session_command(
    opencode_executable: str,
    task_id: str,
    text: str,
    project_path: str | Path,
) -> list[str]:
    return [
        opencode_executable,
        "run",
        "--title",
        f"bridge:{task_id}",
        "--dir",
        str(Path(project_path).resolve()),
        "--format",
        "json",
        text,
    ]


def send_new_session_via_cli(
    opencode_executable: str,
    task_id: str,
    text: str,
    project_path: str | Path,
) -> dict[str, object]:
    project = Path(project_path).resolve()
    try:
        completed = subprocess.run(
            _cli_new_session_command(opencode_executable, task_id, text, project),
            cwd=str(project),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OpenCodeClientError(f"OpenCode CLI failed to start: {exc}") from exc
    return {
        "accepted": completed.returncode == 0,
        "transport": "cli",
        "session_title": f"bridge:{task_id}",
        "exit_code": completed.returncode,
        "output": completed.stdout[-12000:],
        "error": completed.stderr[-4000:],
    }


def dispatch_new_session_via_cli(
    opencode_executable: str,
    task_id: str,
    text: str,
    project_path: str | Path,
) -> dict[str, object]:
    project = Path(project_path).resolve()
    try:
        process = subprocess.Popen(
            _cli_new_session_command(opencode_executable, task_id, text, project),
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise OpenCodeClientError(f"OpenCode CLI failed to start: {exc}") from exc
    return {
        "accepted": True,
        "transport": "cli",
        "session_title": f"bridge:{task_id}",
        "detached": True,
        "pid": process.pid,
    }


def send_prompt_async(
    endpoint: OpenCodeEndpoint,
    session_id: str,
    text: str,
    project_path: str | Path,
) -> dict[str, object]:
    project = str(Path(project_path).resolve())
    status, _ = _request(
        endpoint,
        f"/session/{session_id}/prompt_async",
        method="POST",
        query={"directory": project},
        payload={"parts": [{"type": "text", "text": text}]},
    )
    return {
        "accepted": status == 204,
        "transport": "http",
        "status_code": status,
        "session_id": session_id,
    }


def get_session_status(
    endpoint: OpenCodeEndpoint,
    project_path: str | Path,
) -> dict[str, object]:
    project = str(Path(project_path).resolve())
    _, payload = _request(endpoint, "/session/status", query={"directory": project})
    if not isinstance(payload, dict):
        raise OpenCodeClientError("OpenCode /session/status response must be an object")
    return payload


def _session_id(session: dict[str, object]) -> str:
    value = session.get("id")
    if not isinstance(value, str) or not value:
        raise OpenCodeClientError("OpenCode session is missing a valid id")
    return value


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
        raise OpenCodeClientError(f"Git baseline capture failed: {exc}") from exc
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


def _normalize_allowed_files(allowed_files: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in allowed_files:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.strip() in {"", "."}:
            raise OpenCodeClientError("Allowed files must be repo-relative paths or globs")
        normalized.append(path.as_posix())
    return sorted(set(normalized))


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:16]}"


def _validate_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise OpenCodeClientError(
            "task_id must be 8-80 characters using letters, digits, '_' or '-'"
        )
    return task_id


def _latest_codex_thread(project_path: str | Path) -> str:
    project = Path(project_path).resolve()
    sessions_dir = Path.home() / ".codex" / "sessions"
    candidates: list[tuple[float, str]] = []
    for rollout in sessions_dir.rglob("rollout-*.jsonl") if sessions_dir.exists() else []:
        try:
            first_line = rollout.open(encoding="utf-8").readline()
            record = json.loads(first_line)
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            cwd = payload.get("cwd")
            thread_id = payload.get("session_id") or payload.get("id")
            if (
                isinstance(cwd, str)
                and Path(cwd).resolve() == project
                and isinstance(thread_id, str)
                and thread_id
            ):
                candidates.append((rollout.stat().st_mtime, thread_id))
        except (OSError, json.JSONDecodeError):
            continue
    if not candidates:
        raise OpenCodeClientError(f"No Codex App task found for project: {project}")
    candidates.sort(reverse=True)
    return candidates[0][1]


def create_task_manifest(
    *,
    project_path: str | Path,
    task_id: str,
    codex_thread_id: str,
    opencode_session_id: str,
    allowed_files: list[str],
    read_only: bool,
    acceptance: list[str],
) -> dict[str, object]:
    project = Path(project_path).resolve()
    validated_task_id = _validate_task_id(task_id)
    normalized_allowed_files = _normalize_allowed_files(allowed_files)
    manifest_path = project / ".opencode" / "task-bridge" / f"{validated_task_id}.json"
    if manifest_path.exists():
        raise OpenCodeClientError(f"Task manifest already exists: {validated_task_id}")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "task_id": validated_task_id,
        "project_path": str(project),
        "codex_thread_id": codex_thread_id,
        "opencode_session_id": opencode_session_id,
        "baseline_head": _git_output(project, "rev-parse", "HEAD"),
        "baseline_dirty": snapshot_dirty_files(project),
        "baseline_scope": snapshot_scope_files(
            project,
            [".codex/**", ".opencode/**"] if read_only else normalized_allowed_files,
        ),
        "allowed_files": normalized_allowed_files,
        "read_only": read_only,
        "acceptance": acceptance,
        "state": "dispatched",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_task_envelope(message: str, manifest: Mapping[str, object]) -> str:
    task_id = str(manifest["task_id"])
    allowed = manifest.get("allowed_files")
    allowed_text = ", ".join(str(item) for item in allowed) if isinstance(allowed, list) else ""
    if bool(manifest.get("read_only")):
        allowed_text = "READ_ONLY (no file changes allowed)"
    acceptance = manifest.get("acceptance")
    acceptance_text = "; ".join(str(item) for item in acceptance) if isinstance(acceptance, list) else ""
    return (
        "BRIDGE_TASK\n"
        f"TASK_ID: {task_id}\n"
        f"BASELINE_HEAD: {manifest['baseline_head']}\n"
        f"ALLOWED_FILES: {allowed_text}\n"
        f"ACCEPTANCE: {acceptance_text or 'Report commands actually run and their results'}\n\n"
        "只执行本 TASK_ID 的任务；忽略会话中更早的未完成指令。不要提交代码。"
        "不得修改允许范围之外的文件，不得覆盖派发前已有改动。\n\n"
        f"TASK:\n{message.rstrip()}\n\n"
        "完成或受阻后必须调用项目内 $send2codex skill，执行：\n"
        "python .opencode/skills/send2codex/send_message.py result "
        f"--task-id {task_id} --status completed --summary \"<结果摘要>\" "
        "--verification \"<实际运行的验证及结果>\" --risk \"<剩余风险；无则写 none>\"\n"
        "不要自行选择 Codex thread；回传脚本会使用 manifest 中固定的 thread。"
    )


def _callback_envelope(message: str) -> str:
    return (
        f"{message.rstrip()}\n\n"
        "完成后必须使用项目内的 $send2codex skill，把最终结果回传到当前 "
        "Codex App 会话。请执行：\n"
        "python .opencode/skills/send2codex/send_message.py send --target codex "
        "\"<用简洁中文填写结果、改动文件、验证与剩余风险>\"\n"
        "回传成功后，再在 OpenCode 会话中给出同样的最终摘要。"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send tasks to an existing OpenCode project session without forking it."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--transport", choices=("cli", "http"), default="http")
        subparser.add_argument("--project-path", default=str(Path.cwd()))
        subparser.add_argument("--session-id")
        subparser.add_argument("--server-url")
        subparser.add_argument("--username")
        subparser.add_argument("--password")
        subparser.add_argument(
            "--opencode-executable",
            default=shutil.which("opencode") or "opencode",
        )
        subparser.add_argument("--json", action="store_true")

    send = subparsers.add_parser("send", help="Send a message to an existing session")
    send.add_argument("message")
    send.add_argument(
        "--callback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require OpenCode to return its result through the project send2codex skill",
    )
    send.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the OpenCode CLI process instead of relying on callback delivery",
    )
    send.add_argument("--task-id")
    send.add_argument("--codex-thread-id")
    send.add_argument("--allow-file", action="append", default=[])
    send.add_argument("--read-only", action="store_true")
    send.add_argument("--acceptance", action="append", default=[])
    add_common(send)

    for name, help_text in (
        ("sessions", "List project sessions"),
        ("latest", "Show the latest project session"),
        ("status", "Read HTTP session status"),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        add_common(command_parser)
    return parser


def _http_endpoint(args: argparse.Namespace) -> OpenCodeEndpoint:
    return resolve_http_endpoint(
        server_url=args.server_url,
        username=args.username,
        password=args.password,
        project_path=args.project_path,
    )


def _run_command(args: argparse.Namespace) -> object:
    if args.command == "status":
        if args.transport != "http":
            raise OpenCodeClientError("status requires --transport http")
        return get_session_status(_http_endpoint(args), args.project_path)

    if args.command == "send" and args.callback:
        if args.read_only and args.allow_file:
            raise OpenCodeClientError("Choose --read-only or --allow-file, not both")
        if not args.read_only and not args.allow_file:
            raise OpenCodeClientError(
                "Callback tasks require at least one --allow-file or --read-only"
            )
        args.allow_file = _normalize_allowed_files(args.allow_file)

    if args.transport == "cli":
        if args.command in {"sessions", "latest"}:
            sessions = list_cli_sessions(args.opencode_executable, args.project_path)
        else:
            sessions = []
        if args.command == "sessions":
            return sessions
        if args.command == "latest":
            if not sessions:
                raise OpenCodeClientError(
                    f"No OpenCode session found for project: {Path(args.project_path).resolve()}"
                )
            return sessions[0]
        task_id = args.task_id or _new_task_id()
        isolated_session = bool(args.callback and not args.session_id)
        if isolated_session:
            session_id = f"new:{task_id}"
        elif args.session_id:
            session_id = args.session_id
        else:
            sessions = list_cli_sessions(args.opencode_executable, args.project_path)
            if not sessions:
                raise OpenCodeClientError(
                    f"No OpenCode session found for project: {Path(args.project_path).resolve()}"
                )
            session_id = _session_id(sessions[0])
        result_metadata: dict[str, object] = {}
        if args.callback:
            codex_thread_id = (
                args.codex_thread_id
                or os.environ.get("CODEX_THREAD_ID")
                or _latest_codex_thread(args.project_path)
            )
            manifest = create_task_manifest(
                project_path=args.project_path,
                task_id=task_id,
                codex_thread_id=codex_thread_id,
                opencode_session_id=session_id,
                allowed_files=args.allow_file,
                read_only=args.read_only,
                acceptance=args.acceptance,
            )
            message = build_task_envelope(args.message, manifest)
            result_metadata = {
                "task_id": manifest["task_id"],
                "baseline_head": manifest["baseline_head"],
                "codex_thread_id": codex_thread_id,
                "manifest_path": str(
                    Path(args.project_path).resolve()
                    / ".opencode/task-bridge"
                    / f"{manifest['task_id']}.json"
                ),
            }
        else:
            message = args.message
        if isolated_session:
            isolated_sender = send_new_session_via_cli if args.wait else dispatch_new_session_via_cli
            result = isolated_sender(
                args.opencode_executable,
                task_id,
                message,
                args.project_path,
            )
        else:
            sender = send_via_cli if args.wait else dispatch_via_cli
            result = sender(
                args.opencode_executable,
                session_id,
                message,
                args.project_path,
            )
        result.update(result_metadata)
        return result

    endpoint = _http_endpoint(args)
    sessions = list_project_sessions(endpoint, args.project_path)
    if args.command == "sessions":
        return sessions
    if args.command == "latest":
        if not sessions:
            raise OpenCodeClientError(
                f"No OpenCode session found for project: {Path(args.project_path).resolve()}"
            )
        return sessions[0]
    task_id = args.task_id or _new_task_id()
    if args.callback and not args.session_id:
        session = create_project_session(
            endpoint,
            f"bridge:{task_id}",
            args.project_path,
        )
        session_id = _session_id(session)
    elif args.session_id:
        session_id = args.session_id
    elif sessions:
        session_id = _session_id(sessions[0])
    else:
        raise OpenCodeClientError(
            f"No OpenCode session found for project: {Path(args.project_path).resolve()}"
        )
    result_metadata = {}
    if args.callback:
        codex_thread_id = (
            args.codex_thread_id
            or os.environ.get("CODEX_THREAD_ID")
            or _latest_codex_thread(args.project_path)
        )
        manifest = create_task_manifest(
            project_path=args.project_path,
            task_id=task_id,
            codex_thread_id=codex_thread_id,
            opencode_session_id=session_id,
            allowed_files=args.allow_file,
            read_only=args.read_only,
            acceptance=args.acceptance,
        )
        message = build_task_envelope(args.message, manifest)
        result_metadata = {
            "task_id": manifest["task_id"],
            "baseline_head": manifest["baseline_head"],
            "codex_thread_id": codex_thread_id,
        }
    else:
        message = args.message
    result = send_prompt_async(endpoint, session_id, message, args.project_path)
    result.update(result_metadata)
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run_command(args)
    except OpenCodeClientError as exc:
        if args.json:
            print(json.dumps({"accepted": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"send2opencode: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
