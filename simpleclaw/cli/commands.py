"""CLI commands for simpleclaw."""

import asyncio
import os
import select
import signal
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import print_formatted_text
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import run_in_terminal
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from simpleclaw import __logo__, __version__
from simpleclaw.agent.postprocess import render_postprocess_prompt
from simpleclaw.config.paths import get_workspace_path
from simpleclaw.runtime.agent_factory import make_agent_loop
from simpleclaw.runtime.config_runtime import (
    load_runtime_config,
    print_deprecated_memory_window_notice,
)
from simpleclaw.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="sclaw",
    help=f"{__logo__} simpleclaw - Multi-tenant AI Companion Agent",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from simpleclaw.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} simpleclaw[/cyan]")
    console.print(body)
    console.print()


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(response: str, render_markdown: bool) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} simpleclaw[/cyan]"),
                c.print(Markdown(content) if render_markdown else Text(content)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} simpleclaw v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """simpleclaw - Multi-tenant AI Companion Agent."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize simpleclaw configuration and workspace."""
    from simpleclaw.config.loader import get_config_path, load_config, save_config
    from simpleclaw.config.schema import Config

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    console.print("[dim]Config template now uses `maxTokens` + `contextWindowTokens`; `memoryWindow` is no longer a runtime setting.[/dim]")

    # Create workspace
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} simpleclaw is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.simpleclaw/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]sclaw agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See the README for channel setup.[/dim]")

# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the simpleclaw gateway."""
    from simpleclaw.bus.queue import MessageBus
    from simpleclaw.channels.manager import ChannelManager
    from simpleclaw.config.paths import get_cron_dir
    from simpleclaw.cron.executor import execute_cron_job
    from simpleclaw.cron.service import CronService
    from simpleclaw.cron.types import CronJob
    from simpleclaw.heartbeat.scheduler import HeartbeatScheduler, HeartbeatTarget
    from simpleclaw.runtime.leases import LeaseRepository
    from simpleclaw.session.manager import SessionManager
    from simpleclaw.tenant.state import TenantStateRepository

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = load_runtime_config(console, config_path=config, workspace=workspace)
    print_deprecated_memory_window_notice(config, console)
    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting simpleclaw gateway on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    session_manager = SessionManager(config.workspace_path)
    hb_cfg = config.gateway.heartbeat
    lease_repo = LeaseRepository()
    tenant_state_repo = TenantStateRepository(
        default_heartbeat_enabled=hb_cfg.enabled,
        default_heartbeat_interval_s=hb_cfg.interval_s,
        default_heartbeat_stagger_s=hb_cfg.stagger_s,
    )

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(
        cron_store_path,
        lease_repo=lease_repo,
        enabled=config.gateway.cron.enabled,
        max_concurrency=config.gateway.lanes.cron,
    )

    # Create agent with cron service
    agent = make_agent_loop(
        config=config,
        console=console,
        bus=bus,
        cron_service=cron,
        session_manager=session_manager,
        tenant_state_repo=tenant_state_repo,
        lease_repo=lease_repo,
    )
    cron_agent = (
        make_agent_loop(
            config=config,
            console=console,
            bus=bus,
            cron_service=cron,
            settings=config.agents.cron,
            tenant_state_repo=tenant_state_repo,
            lease_repo=lease_repo,
        )
        if config.agents.cron.enabled
        else agent
    )
    heartbeat_decider = (
        make_agent_loop(
            config=config,
            console=console,
            bus=bus,
            session_manager=session_manager,
            settings=config.agents.heartbeat,
            tenant_state_repo=tenant_state_repo,
            lease_repo=lease_repo,
        )
        if config.agents.heartbeat.enabled
        else agent
    )
    postprocess_agent = (
        make_agent_loop(
            config=config,
            console=console,
            bus=bus,
            cron_service=cron,
            settings=config.agents.postprocess,
            tenant_state_repo=tenant_state_repo,
            lease_repo=lease_repo,
        )
        if config.agents.postprocess.enabled
        else None
    )

    if postprocess_agent is not None:
        postprocess_agent.disable_deferred_postprocess()

        async def run_postprocess(
            runtime,
            channel: str,
            chat_id: str,
            session_key: str,
            tenant_key: str,
            _message_id: str | None,
            actions,
            origin_user_message: str,
            assistant_reply: str,
        ) -> None:
            await postprocess_agent.process_direct(
                render_postprocess_prompt(
                    actions,
                    origin_user_message=origin_user_message,
                    assistant_reply=assistant_reply,
                ),
                session_key=f"postprocess:{session_key}",
                channel=channel,
                chat_id=chat_id,
                tenant_key=tenant_key,
                session_type="postprocess",
                origin_session_key=session_key,
                lane="subagent",
            )

        agent.set_postprocess_runner(run_postprocess)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        return await execute_cron_job(
            agent_loop=cron_agent,
            job=job,
            default_channel="cli",
            publish_outbound=bus.publish_outbound,
        )
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)
 
    async def on_heartbeat_notify(target: HeartbeatTarget, response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from simpleclaw.bus.events import OutboundMessage

        if target.channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(
            OutboundMessage(channel=target.channel, chat_id=target.chat_id, content=response)
        )

    async def run_heartbeat_for_tenant(target: HeartbeatTarget, content: str) -> str:
        """Decide and execute one tenant heartbeat turn."""
        from simpleclaw.agent.tools.message import MessageTool

        action, tasks = await heartbeat_decider.decide_heartbeat(
            content,
            session_key=target.session_key,
            channel=target.channel,
            chat_id=target.chat_id,
            tenant_key=target.tenant_key,
        )
        if action != "run":
            return action

        async def _silent(*_args, **_kwargs):
            pass

        response = await agent.process_direct(
            (
                "[Heartbeat Trigger]\n"
                "This turn was triggered by the periodic heartbeat for the active session.\n\n"
                "## Decided Tasks\n"
                f"{tasks}"
            ),
            session_key=target.session_key,
            channel=target.channel,
            chat_id=target.chat_id,
            tenant_key=target.tenant_key,
            on_progress=_silent,
            extra_system_sections=[
                "# Heartbeat Context\n"
                "The following context was injected by the periodic heartbeat for this active session.\n\n"
                "## HEARTBEAT.md\n"
                f"{content}"
            ],
            lane="heartbeat",
        )
        message_tool = agent.tools.get("message")
        if response and not (
            isinstance(message_tool, MessageTool) and message_tool.sent_in_turn()
        ):
            await on_heartbeat_notify(target, response)
        return "ok"

    heartbeat = HeartbeatScheduler(
        workspace=config.workspace_path,
        tenant_repo=tenant_state_repo,
        lease_repo=lease_repo,
        runner=run_heartbeat_for_tenant,
        poll_interval_s=hb_cfg.poll_interval_s,
        busy_defer_s=hb_cfg.busy_defer_s,
        recent_activity_cooldown_s=hb_cfg.recent_activity_cooldown_s,
        max_concurrency=config.gateway.lanes.heartbeat,
        stagger_s=hb_cfg.stagger_s,
        tenant_filter=hb_cfg.target_tenant_key,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        cron_state = "enabled" if cron_status.get("configured_enabled", True) else "disabled"
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs ({cron_state})")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            if cron_agent is not agent:
                await cron_agent.close_mcp()
            if heartbeat_decider is not agent and heartbeat_decider is not cron_agent:
                await heartbeat_decider.close_mcp()
            if postprocess_agent is not None and postprocess_agent is not agent and postprocess_agent is not cron_agent and postprocess_agent is not heartbeat_decider:
                await postprocess_agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            if cron_agent is not agent:
                cron_agent.stop()
            if heartbeat_decider is not agent and heartbeat_decider is not cron_agent:
                heartbeat_decider.stop()
            if postprocess_agent is not None and postprocess_agent is not agent and postprocess_agent is not cron_agent and postprocess_agent is not heartbeat_decider:
                postprocess_agent.stop()
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def api(
    host: str = typer.Option("127.0.0.1", "--host", help="API bind host"),
    port: int = typer.Option(18791, "--port", "-p", help="API bind port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start a minimal HTTP API for driving the agent."""
    from simpleclaw.api.server import run_api_server

    run_api_server(
        host=host,
        port=port,
        workspace=workspace,
        config=config,
        verbose=verbose,
        console=console,
        logo=__logo__,
    )


@app.command()
def serve(
    role: str = typer.Argument(
        ...,
        help="Runtime role: chat-api | scheduler-service | postprocess-worker | background-worker",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="API bind host (chat-api only)"),
    port: int = typer.Option(18790, "--port", "-p", help="API bind port (chat-api only)"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start one dedicated runtime role for the multi-service deployment."""
    from simpleclaw.api.server import run_api_server_with_runtime
    from simpleclaw.runtime.bootstrap import build_runtime_services, run_scheduler_service

    runtime = build_runtime_services(
        console=console,
        role=role,
        config_path=config,
        workspace=workspace,
    )

    if role == "chat-api":
        run_api_server_with_runtime(
            runtime=runtime,
            host=host,
            port=port,
            verbose=verbose,
            console=console,
            logo=__logo__,
        )
        return

    async def _run() -> None:
        try:
            if role == "scheduler-service":
                await run_scheduler_service(runtime)
            elif role == "postprocess-worker":
                await runtime.run_postprocess_worker()
            elif role == "background-worker":
                await runtime.run_background_worker()
            else:
                raise typer.BadParameter(f"Unsupported runtime role: {role}")
        finally:
            await runtime.close()

    asyncio.run(_run())


@app.command("migrate-runtime-state")
def migrate_runtime_state(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Migrate file-backed runtime state into the configured MySQL tables."""
    from simpleclaw.runtime.bootstrap import load_runtime_config
    from simpleclaw.runtime.migrate import migrate_file_runtime_state_to_mysql

    runtime_config = load_runtime_config(console, config_path=config, workspace=workspace)
    migrate_file_runtime_state_to_mysql(runtime_config, console)


@app.command("dev-up")
def dev_up(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Start local MySQL, Redis, and all runtime roles for development."""
    from simpleclaw.runtime.dev_orchestrator import DevOrchestrator

    DevOrchestrator(console=console, config_path=config, workspace=workspace).up()


@app.command("dev-down")
def dev_down(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Stop services started by `sclaw dev-up`."""
    from simpleclaw.runtime.dev_orchestrator import DevOrchestrator

    DevOrchestrator(console=console, config_path=config, workspace=workspace).down()


@app.command("dev-status")
def dev_status(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Show local development service status."""
    from simpleclaw.runtime.dev_orchestrator import DevOrchestrator

    DevOrchestrator(console=console, config_path=config, workspace=workspace).status()


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show simpleclaw runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from simpleclaw.bus.queue import MessageBus
    from simpleclaw.config.paths import get_cron_dir
    from simpleclaw.cron.service import CronService

    config = load_runtime_config(console, config_path=config, workspace=workspace)
    print_deprecated_memory_window_notice(config, console)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(
        cron_store_path,
        enabled=config.gateway.cron.enabled,
        max_concurrency=config.gateway.lanes.cron,
    )

    if logs:
        logger.enable("simpleclaw")
    else:
        logger.disable("simpleclaw")

    agent_loop = make_agent_loop(
        config=config,
        console=console,
        bus=bus,
        cron_service=cron,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]simpleclaw is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
                
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from simpleclaw.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_line(msg.content)

                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(msg.content, render_markdown=markdown)

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from simpleclaw.channels.registry import discover_channel_names, load_channel_class
    from simpleclaw.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for modname in sorted(discover_channel_names()):
        section = getattr(config.channels, modname, None)
        enabled = section and getattr(section, "enabled", False)
        try:
            cls = load_channel_class(modname)
            display = cls.display_name
        except ImportError:
            display = modname.title()
        table.add_row(
            display,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from simpleclaw.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # simpleclaw/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall simpleclaw")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    from simpleclaw.config.loader import load_config
    from simpleclaw.config.paths import get_runtime_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token
    env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show simpleclaw status."""
    from simpleclaw.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} simpleclaw Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from simpleclaw.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from simpleclaw.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
