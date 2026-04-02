"""Local development service orchestration."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console
from rich.table import Table

from simpleclaw.config.paths import get_data_dir
from simpleclaw.runtime.bootstrap import load_runtime_config
from simpleclaw.utils.helpers import ensure_dir


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 20.0, interval_s: float = 0.2) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid), encoding="utf-8")


def _remove_pid(path: Path) -> None:
    if path.exists():
        path.unlink()


def _find_process_by_substring(needle: str) -> int | None:
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or needle not in line:
            continue
        try:
            pid_text, _command = line.split(None, 1)
            pid = int(pid_text)
        except Exception:
            continue
        if pid != os.getpid():
            return pid
    return None


@dataclass(frozen=True, slots=True)
class ManagedService:
    name: str
    pid_file: Path
    log_file: Path
    port: int | None = None
    process_hint: str | None = None


class DevOrchestrator:
    """Start and stop local infrastructure and runtime roles."""

    def __init__(
        self,
        *,
        console: Console,
        config_path: str | None,
        workspace: str | None,
    ) -> None:
        self.console = console
        self.config = load_runtime_config(console, config_path=config_path, workspace=workspace)
        self.config_path = str(Path(config_path).expanduser().resolve()) if config_path else None
        self.workspace = workspace
        self.root = ensure_dir(get_data_dir() / "services" / "dev")
        self.logs_dir = ensure_dir(self.root / "logs")
        self.mysql_dir = ensure_dir(self.root / "mysql")
        self.redis_dir = ensure_dir(self.root / "redis")
        self.mysql_data_dir = ensure_dir(self.mysql_dir / "data")
        self.mysql_tmp_dir = ensure_dir(self.mysql_dir / "tmp")
        self.redis_data_dir = ensure_dir(self.redis_dir / "data")
        self.mysql_socket = self.mysql_dir / "mysql.sock"
        self.mysql_pid_file = self.mysql_dir / "mysql.pid"
        self.redis_pid_file = self.redis_dir / "redis.pid"

        self.services: list[ManagedService] = [
            ManagedService(
                name="mysql",
                pid_file=self.mysql_pid_file,
                log_file=self.logs_dir / "mysql.log",
                port=3306,
                process_hint=str(self.mysql_data_dir),
            ),
            ManagedService(
                name="redis",
                pid_file=self.redis_pid_file,
                log_file=self.logs_dir / "redis.log",
                port=6379,
                process_hint="redis-server --bind 127.0.0.1 --port 6379",
            ),
            ManagedService(
                name="chat-api",
                pid_file=self.root / "chat-api.pid",
                log_file=self.logs_dir / "chat-api.log",
                port=18790,
                process_hint="sclaw serve chat-api",
            ),
            ManagedService(
                name="scheduler-service",
                pid_file=self.root / "scheduler-service.pid",
                log_file=self.logs_dir / "scheduler-service.log",
                process_hint="sclaw serve scheduler-service",
            ),
            ManagedService(
                name="postprocess-worker",
                pid_file=self.root / "postprocess-worker.pid",
                log_file=self.logs_dir / "postprocess-worker.log",
                process_hint="sclaw serve postprocess-worker",
            ),
            ManagedService(
                name="background-worker",
                pid_file=self.root / "background-worker.pid",
                log_file=self.logs_dir / "background-worker.log",
                process_hint="sclaw serve background-worker",
            ),
        ]

    def _service(self, name: str) -> ManagedService:
        for service in self.services:
            if service.name == name:
                return service
        raise KeyError(name)

    def _managed_pid(self, service: ManagedService) -> int | None:
        pid = _read_pid(service.pid_file)
        if pid is None:
            return None
        if _is_pid_running(pid):
            return pid
        _remove_pid(service.pid_file)
        return None

    def _external_pid(self, service: ManagedService) -> int | None:
        if service.process_hint:
            pid = _find_process_by_substring(service.process_hint)
            if pid is not None:
                return pid
        if service.name == "mysql" and service.process_hint:
            return _find_process_by_substring("mysqld")
        return None

    def _status(self, service: ManagedService) -> tuple[str, int | None]:
        managed = self._managed_pid(service)
        if managed is not None:
            return ("managed-running", managed)
        external = self._external_pid(service)
        if external is not None:
            return ("external-running", external)
        return ("stopped", None)

    def _spawn(self, args: list[str], *, log_file: Path, env: dict[str, str] | None = None) -> int:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Reset stale dev logs on each fresh service start.
        handle = log_file.open("w", encoding="utf-8")
        process = subprocess.Popen(
            args,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
            start_new_session=True,
            env=env or os.environ.copy(),
        )
        handle.close()
        return process.pid

    def _mysql_bin(self) -> str:
        path = str(Path(sys.executable).resolve().parent / "mysqld")
        if not Path(path).exists():
            raise typer.BadParameter("Current environment does not contain mysqld")
        return path

    def _redis_bin(self) -> str:
        path = str(Path(sys.executable).resolve().parent / "redis-server")
        if not Path(path).exists():
            raise typer.BadParameter("Current environment does not contain redis-server")
        return path

    def _init_mysql_if_needed(self) -> None:
        mysql_system_dir = self.mysql_data_dir / "mysql"
        if mysql_system_dir.exists():
            return
        args = [
            self._mysql_bin(),
            "--initialize-insecure",
            f"--basedir={Path(sys.executable).resolve().parent.parent}",
            f"--datadir={self.mysql_data_dir}",
            f"--lc-messages-dir={Path(sys.executable).resolve().parent.parent / 'share' / 'mysql'}",
        ]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        with (self.logs_dir / "mysql-init.log").open("w", encoding="utf-8") as handle:
            handle.write(result.stdout)
            handle.write(result.stderr)
        if result.returncode != 0:
            raise RuntimeError("MySQL initialization failed. Check mysql-init.log")

    def start_mysql(self) -> None:
        service = self._service("mysql")
        status, pid = self._status(service)
        if status != "stopped":
            self.console.print(f"[dim]mysql already running ({status}, pid={pid})[/dim]")
            return
        self._init_mysql_if_needed()
        args = [
            self._mysql_bin(),
            "--console",
            f"--basedir={Path(sys.executable).resolve().parent.parent}",
            f"--datadir={self.mysql_data_dir}",
            f"--socket={self.mysql_socket}",
            f"--pid-file={self.mysql_pid_file}",
            "--bind-address=127.0.0.1",
            "--port=3306",
            f"--lc-messages-dir={Path(sys.executable).resolve().parent.parent / 'share' / 'mysql'}",
            f"--tmpdir={self.mysql_tmp_dir}",
        ]
        pid = self._spawn(args, log_file=service.log_file)
        _write_pid(service.pid_file, pid)
        if not _wait_until(lambda: _is_port_open("127.0.0.1", 3306), timeout_s=20):
            raise RuntimeError("MySQL did not start successfully")
        import pymysql

        conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="", autocommit=True)
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS simpleclaw CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.close()
        self.console.print("[green]✓[/green] mysql started")

    def start_redis(self) -> None:
        service = self._service("redis")
        status, pid = self._status(service)
        if status != "stopped":
            self.console.print(f"[dim]redis already running ({status}, pid={pid})[/dim]")
            return
        args = [
            self._redis_bin(),
            "--bind",
            "127.0.0.1",
            "--port",
            "6379",
            "--dir",
            str(self.redis_data_dir),
            "--appendonly",
            "yes",
        ]
        pid = self._spawn(args, log_file=service.log_file)
        _write_pid(service.pid_file, pid)
        if not _wait_until(lambda: _is_port_open("127.0.0.1", 6379), timeout_s=10):
            raise RuntimeError("Redis did not start successfully")
        self.console.print("[green]✓[/green] redis started")

    def _role_args(self, role: str) -> list[str]:
        args = [sys.executable, "-m", "simpleclaw", "serve", role]
        if role == "chat-api":
            args.extend(["--host", "127.0.0.1", "--port", "18790"])
        if self.config_path:
            args.extend(["-c", self.config_path])
        if self.workspace:
            args.extend(["-w", self.workspace])
        return args

    def start_role(self, role: str) -> None:
        service = self._service(role)
        status, pid = self._status(service)
        if status != "stopped":
            self.console.print(f"[dim]{role} already running ({status}, pid={pid})[/dim]")
            return
        pid = self._spawn(self._role_args(role), log_file=service.log_file)
        _write_pid(service.pid_file, pid)
        if service.port is not None:
            if not _wait_until(lambda: _is_port_open("127.0.0.1", service.port or 0), timeout_s=15):
                raise RuntimeError(f"{role} did not start successfully")
        else:
            if not _wait_until(lambda: _is_pid_running(pid), timeout_s=5):
                raise RuntimeError(f"{role} did not stay alive")
        self.console.print(f"[green]✓[/green] {role} started")

    def up(self) -> None:
        self.start_mysql()
        self.start_redis()
        for role in ("chat-api", "scheduler-service", "postprocess-worker", "background-worker"):
            self.start_role(role)

    def stop_service(self, name: str) -> None:
        service = self._service(name)
        pid = self._managed_pid(service)
        if pid is None:
            self.console.print(f"[dim]{name} not managed by dev-up[/dim]")
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            _remove_pid(service.pid_file)
            return
        stopped = _wait_until(lambda: not _is_pid_running(pid), timeout_s=10)
        if not stopped:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            _wait_until(lambda: not _is_pid_running(pid), timeout_s=3)
        _remove_pid(service.pid_file)
        self.console.print(f"[green]✓[/green] {name} stopped")

    def down(self) -> None:
        for name in ("background-worker", "postprocess-worker", "scheduler-service", "chat-api", "redis", "mysql"):
            self.stop_service(name)

    def status(self) -> None:
        table = Table(title="simpleclaw dev services")
        table.add_column("Service")
        table.add_column("State")
        table.add_column("PID")
        table.add_column("Log")
        for service in self.services:
            state, pid = self._status(service)
            table.add_row(
                service.name,
                state,
                str(pid or "-"),
                str(service.log_file),
            )
        self.console.print(table)

