"""Shell agent tools for VPS management via Telegram chat.

Owner-only tools that let the bot execute shell commands, manage files,
view logs, and control services on the host machine.

Security:
- All tools gated by `prepare_shell_tools` → only available to owners
- Dangerous commands blocked by configurable blacklist
- Command timeout enforced
- Output truncated to prevent flooding
"""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from waku.config import app_config
from waku.logger import logger

from .. import datatype

# ── defaults ────────────────────────────────────────────────────────────────

DEFAULT_BLOCKED_PATTERNS: list[str] = [
    # Destructive
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){",           # fork bomb
    # Credential / secrets exfiltration
    "cat /etc/shadow",
    "passwd",
    # Reboot / shutdown
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    "poweroff",
    "halt",
]

MAX_OUTPUT_LENGTH = 8000  # characters


# ── helpers ─────────────────────────────────────────────────────────────────

def _is_command_blocked(command: str) -> bool:
    """Check if a command matches any blocked pattern."""
    cmd_lower = command.lower().strip()

    # Check custom blocked commands from config
    blocked: list[str] = getattr(app_config, "agent_shell_blocked_commands", [])
    all_blocked = DEFAULT_BLOCKED_PATTERNS + blocked

    for pattern in all_blocked:
        if pattern.lower() in cmd_lower:
            return True

    return False


def _truncate_output(text: str, max_length: int | None = None) -> str:
    """Truncate output to max length."""
    limit = max_length or getattr(
        app_config, "agent_shell_max_output_length", MAX_OUTPUT_LENGTH
    )
    if len(text) > limit:
        return text[:limit] + f"\n\n[... truncated, total {len(text)} chars]"
    return text


# ── tools ───────────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """Result of a shell command execution."""
    command: str
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def exec_command(
    ctx: RunContext[datatype.ContextDeps],
    command: str,
    working_dir: str | None = None,
    timeout: int | None = None,
) -> CommandResult:
    """Execute a shell command on the VPS and return the result.

    Use this to run any Linux shell command. The output (stdout + stderr)
    will be captured and returned.

    IMPORTANT: This tool is only available to the bot owner.

    Args:
        command: The shell command to execute (e.g. "ls -la /var/log").
        working_dir: Working directory for the command. Defaults to home dir.
        timeout: Max seconds to wait (default from config, usually 30).

    Returns:
        CommandResult with return_code, stdout, stderr, timed_out flag.
    """
    if not command or not command.strip():
        raise ModelRetry("Command cannot be empty.")

    if _is_command_blocked(command):
        raise ModelRetry(
            f"Command blocked by security policy: {command!r}. "
            "This command matches a dangerous pattern."
        )

    cmd_timeout = timeout or getattr(app_config, "agent_shell_timeout", 30)
    cwd = working_dir or getattr(
        app_config, "agent_shell_working_dir", str(Path.home())
    )

    logger.info(
        f"[ShellAgent] user={ctx.deps.user_id} exec: {command!r} "
        f"cwd={cwd} timeout={cmd_timeout}s"
    )

    timed_out = False
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ},
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=cmd_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            timed_out = True
            stdout_bytes = b""
            stderr_bytes = f"Command timed out after {cmd_timeout}s".encode()

        stdout = _truncate_output(stdout_bytes.decode("utf-8", errors="replace"))
        stderr = _truncate_output(stderr_bytes.decode("utf-8", errors="replace"))
        return_code = proc.returncode or -1

        logger.info(
            f"[ShellAgent] exec done: rc={return_code} "
            f"stdout={len(stdout)}c stderr={len(stderr)}c timed_out={timed_out}"
        )

        return CommandResult(
            command=command,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

    except Exception as e:
        logger.error(f"[ShellAgent] exec error: {e.__class__.__name__}: {e}")
        return CommandResult(
            command=command,
            return_code=-1,
            stdout="",
            stderr=f"{e.__class__.__name__}: {e}",
        )


async def shell_read_file(
    ctx: RunContext[datatype.ContextDeps],
    path: str,
    start_line: int = 1,
    max_lines: int = 200,
    encoding: str = "utf-8",
) -> str:
    """Read a file from the VPS filesystem.

    Use this to read config files, logs, scripts, etc.

    Args:
        path: Absolute path to the file (e.g. "/etc/nginx/nginx.conf").
        start_line: Start reading from this line (1-indexed, default 1).
        max_lines: Maximum lines to read (1-2000, default 200).
        encoding: File encoding (default utf-8).

    Returns:
        File contents with line numbers, or an error message.
    """
    if not path or not path.strip():
        raise ModelRetry("Path cannot be empty.")

    if max_lines < 1 or max_lines > 2000:
        raise ModelRetry("max_lines must be between 1 and 2000.")

    if start_line < 1:
        raise ModelRetry("start_line must be >= 1.")

    file_path = Path(path).resolve()
    logger.info(f"[ShellAgent] user={ctx.deps.user_id} read_file: {file_path}")

    if not file_path.exists():
        return f"File not found: {path}"

    if not file_path.is_file():
        return f"Not a file: {path} (is it a directory?)"

    # Check file size (skip very large binary files)
    try:
        size = file_path.stat().st_size
        if size > 10 * 1024 * 1024:  # 10MB
            return f"File too large: {size:,} bytes ({size / 1024 / 1024:.1f} MB). Use exec_command with head/tail instead."
    except OSError as e:
        return f"Cannot stat file: {e}"

    try:
        content = await asyncio.to_thread(
            file_path.read_text, encoding=encoding, errors="replace"
        )
        lines = content.splitlines()
        total = len(lines)

        end_line = min(start_line - 1 + max_lines, total)
        selected = lines[start_line - 1 : end_line]

        parts = [f"File: {path} ({total} lines, {size:,} bytes)"]
        if start_line > 1 or end_line < total:
            parts.append(f"Showing lines {start_line}-{end_line} of {total}")
        parts.append("")

        for i, line in enumerate(selected, start=start_line):
            parts.append(f"{i:>5}: {line}")

        if end_line < total:
            parts.append(f"\n... {total - end_line} more lines remaining")

        return _truncate_output("\n".join(parts))

    except Exception as e:
        logger.error(f"[ShellAgent] read_file error: {e}")
        return f"Error reading file: {e.__class__.__name__}: {e}"


async def shell_write_file(
    ctx: RunContext[datatype.ContextDeps],
    path: str,
    content: str,
    create_dirs: bool = False,
    append: bool = False,
    encoding: str = "utf-8",
) -> str:
    """Write content to a file on the VPS.

    Use this to create config files, scripts, etc.

    Args:
        path: Absolute path to the file.
        content: The content to write.
        create_dirs: If True, create parent directories if they don't exist.
        append: If True, append to the file instead of overwriting.
        encoding: File encoding (default utf-8).

    Returns:
        Success/error message.
    """
    if not path or not path.strip():
        raise ModelRetry("Path cannot be empty.")

    file_path = Path(path).resolve()
    logger.info(
        f"[ShellAgent] user={ctx.deps.user_id} write_file: {file_path} "
        f"append={append} create_dirs={create_dirs} size={len(content)}c"
    )

    try:
        if create_dirs:
            file_path.parent.mkdir(parents=True, exist_ok=True)

        if not file_path.parent.exists():
            return f"Parent directory does not exist: {file_path.parent}. Set create_dirs=True to create it."

        mode = "a" if append else "w"

        def _write():
            with open(file_path, mode, encoding=encoding) as f:
                f.write(content)
            return file_path.stat().st_size

        size = await asyncio.to_thread(_write)
        action = "Appended to" if append else "Written to"
        return f"{action} {path} successfully ({size:,} bytes total)."

    except Exception as e:
        logger.error(f"[ShellAgent] write_file error: {e}")
        return f"Error writing file: {e.__class__.__name__}: {e}"


async def shell_list_dir(
    ctx: RunContext[datatype.ContextDeps],
    path: str = ".",
    show_hidden: bool = False,
    max_entries: int = 100,
) -> str:
    """List files and directories in a path on the VPS.

    Args:
        path: Directory path (default: current directory).
        show_hidden: Show hidden files (starting with .) (default: False).
        max_entries: Maximum entries to show (default: 100).

    Returns:
        Directory listing with file types and sizes.
    """
    dir_path = Path(path).resolve()
    logger.info(f"[ShellAgent] user={ctx.deps.user_id} list_dir: {dir_path}")

    if not dir_path.exists():
        return f"Path not found: {path}"

    if not dir_path.is_dir():
        return f"Not a directory: {path}"

    try:
        entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"Directory: {dir_path}\n"]
        count = 0

        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            count += 1
            if count > max_entries:
                lines.append(f"\n... and {len(list(dir_path.iterdir())) - max_entries} more entries")
                break

            if entry.is_dir():
                lines.append(f"  📁 {entry.name}/")
            elif entry.is_symlink():
                target = entry.resolve() if entry.exists() else "broken"
                lines.append(f"  🔗 {entry.name} -> {target}")
            else:
                try:
                    size = entry.stat().st_size
                    if size >= 1024 * 1024:
                        size_str = f"{size / 1024 / 1024:.1f}M"
                    elif size >= 1024:
                        size_str = f"{size / 1024:.1f}K"
                    else:
                        size_str = f"{size}B"
                    lines.append(f"  📄 {entry.name}  ({size_str})")
                except OSError:
                    lines.append(f"  📄 {entry.name}")

        lines.append(f"\nTotal: {count} entries")
        return "\n".join(lines)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        logger.error(f"[ShellAgent] list_dir error: {e}")
        return f"Error: {e.__class__.__name__}: {e}"


@dataclass
class SystemInfo:
    """System information summary."""
    hostname: str
    os_info: str
    uptime: str
    cpu_count: int
    memory: str
    disk: str
    load_avg: str


async def system_info(
    ctx: RunContext[datatype.ContextDeps],
) -> SystemInfo | str:
    """Get system information of the VPS.

    Returns hostname, OS, uptime, CPU, memory, disk usage, and load average.
    """
    logger.info(f"[ShellAgent] user={ctx.deps.user_id} system_info")

    try:
        # Gather info via shell commands (most portable)
        commands = {
            "hostname": "hostname",
            "os_info": "cat /etc/os-release 2>/dev/null | head -2 || uname -a",
            "uptime": "uptime -p 2>/dev/null || uptime",
            "memory": "free -h 2>/dev/null | head -3 || echo 'N/A'",
            "disk": "df -h / 2>/dev/null | tail -1 || echo 'N/A'",
            "load": "cat /proc/loadavg 2>/dev/null || echo 'N/A'",
        }

        results = {}
        for key, cmd in commands.items():
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                results[key] = stdout.decode("utf-8", errors="replace").strip()
            except Exception:
                results[key] = "N/A"

        return SystemInfo(
            hostname=results.get("hostname", "N/A"),
            os_info=results.get("os_info", "N/A"),
            uptime=results.get("uptime", "N/A"),
            cpu_count=os.cpu_count() or 0,
            memory=results.get("memory", "N/A"),
            disk=results.get("disk", "N/A"),
            load_avg=results.get("load", "N/A"),
        )

    except Exception as e:
        logger.error(f"[ShellAgent] system_info error: {e}")
        return f"Error: {e.__class__.__name__}: {e}"


async def view_logs(
    ctx: RunContext[datatype.ContextDeps],
    service: str | None = None,
    log_file: str | None = None,
    lines: int = 50,
    grep_pattern: str | None = None,
) -> str:
    """View logs from a systemd service or a log file.

    Use either `service` (for journalctl) or `log_file` (for a specific file).

    Args:
        service: Systemd service name (e.g. "nginx", "docker"). Uses journalctl.
        log_file: Path to a log file (e.g. "/var/log/syslog").
        lines: Number of recent lines to show (default: 50, max: 500).
        grep_pattern: Optional pattern to filter log lines.

    Returns:
        Recent log lines.
    """
    if not service and not log_file:
        raise ModelRetry("Provide either 'service' (systemd) or 'log_file' (file path).")

    if lines < 1 or lines > 500:
        raise ModelRetry("lines must be between 1 and 500.")

    logger.info(
        f"[ShellAgent] user={ctx.deps.user_id} view_logs: "
        f"service={service} file={log_file} lines={lines} grep={grep_pattern}"
    )

    if service:
        cmd = f"journalctl -u {shlex.quote(service)} --no-pager -n {lines} --output=short-iso"
    else:
        cmd = f"tail -n {lines} {shlex.quote(log_file)}"

    if grep_pattern:
        cmd += f" | grep --color=never -i {shlex.quote(grep_pattern)}"

    timeout = getattr(app_config, "agent_shell_timeout", 30)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if not output.strip() and err.strip():
            return f"No output. stderr: {_truncate_output(err)}"

        source = f"service={service}" if service else f"file={log_file}"
        header = f"Logs ({source}, last {lines} lines):\n"
        return header + _truncate_output(output)

    except Exception as e:
        logger.error(f"[ShellAgent] view_logs error: {e}")
        return f"Error: {e.__class__.__name__}: {e}"


async def manage_service(
    ctx: RunContext[datatype.ContextDeps],
    service: str,
    action: str = "status",
) -> str:
    """Manage a systemd service (or check Docker container).

    Args:
        service: Service name (e.g. "nginx", "docker", "postgresql").
        action: One of: "status", "start", "stop", "restart", "logs".

    Returns:
        Command output.
    """
    allowed_actions = {"status", "start", "stop", "restart", "logs", "enable", "disable"}
    if action not in allowed_actions:
        raise ModelRetry(f"action must be one of: {', '.join(sorted(allowed_actions))}")

    if not service or not service.strip():
        raise ModelRetry("Service name cannot be empty.")

    # Sanitize service name
    safe_service = shlex.quote(service.strip())

    logger.info(
        f"[ShellAgent] user={ctx.deps.user_id} manage_service: "
        f"{action} {service}"
    )

    if action == "logs":
        return await view_logs(ctx, service=service, lines=50)

    cmd = f"systemctl {action} {safe_service}"
    if action == "status":
        cmd += " --no-pager"

    timeout = getattr(app_config, "agent_shell_timeout", 30)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        result_parts = [f"$ {cmd}", f"Exit code: {proc.returncode}"]
        if output.strip():
            result_parts.append(f"\n{output.strip()}")
        if err.strip():
            result_parts.append(f"\nStderr:\n{err.strip()}")

        return _truncate_output("\n".join(result_parts))

    except Exception as e:
        logger.error(f"[ShellAgent] manage_service error: {e}")
        return f"Error: {e.__class__.__name__}: {e}"


__all__ = [
    "exec_command",
    "shell_read_file",
    "shell_write_file",
    "shell_list_dir",
    "system_info",
    "view_logs",
    "manage_service",
]
