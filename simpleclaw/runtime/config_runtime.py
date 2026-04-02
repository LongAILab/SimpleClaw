"""Shared runtime config loading helpers."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from simpleclaw.config.loader import load_config, set_config_path
from simpleclaw.config.schema import Config


def load_runtime_config(
    console: Console,
    config_path: str | None = None,
    workspace: str | None = None,
) -> Config:
    """Load config and optionally override the active workspace."""
    path_obj = None
    if config_path:
        path_obj = Path(config_path).expanduser().resolve()
        if not path_obj.exists():
            console.print(f"[red]Error: Config file not found: {path_obj}[/red]")
            raise typer.Exit(1)
        set_config_path(path_obj)
        console.print(f"[dim]Using config: {path_obj}[/dim]")

    loaded = load_config(path_obj)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def print_deprecated_memory_window_notice(config: Config, console: Console) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]sclaw onboard[/cyan] to refresh your config template."
        )
