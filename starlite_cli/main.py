from __future__ import annotations

import importlib
import inspect
from functools import wraps
from os import getenv
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, TypeVar, cast

import rich
import rich.tree
from click import ClickException, Command, Group, argument, group, option, style
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from typing_extensions import ParamSpec

if TYPE_CHECKING:
    from starlite import Starlite  # noqa: TC004
    from starlite.middleware.session.base import ServerSideBackend


P = ParamSpec("P")
T = TypeVar("T")

console = Console()


class _StarliteCLIException(ClickException):
    def __init__(self, message: str) -> None:
        super().__init__(style(message, fg="red"))


def _load_app_from_path(app_path: str) -> Starlite:
    module_path, app_name = app_path.split(":")
    module = importlib.import_module(module_path)
    return cast(Starlite, getattr(module, app_name))


def _autodiscover_app() -> tuple[str, Starlite]:
    from starlite import Starlite

    if app_path := getenv("STARLITE_APP"):
        console.print(f"Using starlite app from env: [bright_blue]{app_path!r}")
        return app_path, _load_app_from_path(app_path)

    cwd = Path().cwd()
    for name in ["asgi.py", "app.py", "application.py"]:
        path = cwd / name
        if not path.exists():
            continue

        module = importlib.import_module(path.stem)
        for attr, value in module.__dict__.items():
            if isinstance(value, Starlite):
                app_string = f"{path.stem}:{attr}"
                console.print(f"Using Starlite app from [bright_blue]{path}:{attr}")
                return app_string, value

    raise _StarliteCLIException("Could not find a Starlite app")


def _inject_app(func: Callable[P, T]) -> Callable[P, T]:
    """Inject the app instance into a `Command`"""
    if "app" not in inspect.signature(func).parameters:
        return func

    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        kwargs["app"] = _autodiscover_app()[1]
        return func(*args, **kwargs)

    return wrapped


def _wrap_commands(commands: Iterable[Command]) -> None:
    for command in commands:
        if isinstance(command, Group):
            _wrap_commands(command.commands.values())
        elif command.callback:
            command.callback = _inject_app(command.callback)


def _format_is_enabled(value: Any) -> str:
    """Return a coloured string `"Enabled" if `value` is truthy, else "Disabled"."""
    if value:
        return "[green]Enabled[/]"
    return "[red]Disabled[/]"


def _show_app_info(app: Starlite) -> None:
    """Display basic information about the application and its configuration."""
    from starlite import DefineMiddleware
    from starlite.utils import get_name

    table = Table(show_header=False)
    table.add_column("title", style="cyan")
    table.add_column("value", style="bright_blue")

    table.add_row("Debug mode", _format_is_enabled(app.debug))
    table.add_row("CORS", _format_is_enabled(app.cors_config))
    table.add_row("CSRF", _format_is_enabled(app.csrf_config))
    if app.allowed_hosts:
        allowed_hosts = app.allowed_hosts

        table.add_row("Allowed hosts", ", ".join(allowed_hosts.allowed_hosts))

    table.add_row("Request caching", _format_is_enabled(app.cache))
    openapi_enabled = _format_is_enabled(app.openapi_config)
    if app.openapi_config:
        openapi_enabled += f" path=[yellow]{app.openapi_config.openapi_controller.path}"
    table.add_row("OpenAPI", openapi_enabled)

    table.add_row("Compression", app.compression_config.backend if app.compression_config else "[red]Disabled")

    if app.template_engine:
        table.add_row("Template engine", type(app.template_engine).__name__)

    if app.static_files_config:
        static_files_configs = app.static_files_config
        if not isinstance(static_files_configs, list):
            static_files_configs = [static_files_configs]
        static_files_info = []
        for static_files in static_files_configs:
            static_files_info.append(
                f"path=[yellow]{static_files.path}[/] dirs=[yellow]{', '.join(map(str, static_files.directories))}[/] "
                f"html_mode={_format_is_enabled(static_files.html_mode)}",
            )
        table.add_row("Static files", "\n".join(static_files_info))

    if app.plugins:
        plugin_names = [type(plugin).__name__ for plugin in app.plugins]
        table.add_row("Plugins", ", ".join(plugin_names))

    middlewares = []
    for middleware in app.middleware:
        if isinstance(middleware, DefineMiddleware):
            middleware = middleware.middleware
        middlewares.append(get_name(middleware))
    if middlewares:
        table.add_row("Middlewares", ", ".join(middlewares))

    console.print(table)


@group()
def cli() -> None:
    """Starlite CLI."""
    try:
        # we only want to run this if starlite is installed in the current environment
        # since we could also be running a bootstrapping command, which does not require
        # starlite to be installed
        from starlite.utils.cli import CLI_INIT_CALLBACKS

        for callback in CLI_INIT_CALLBACKS:
            callback(cli)
        _wrap_commands(cli.commands.values())

    except ImportError:
        pass


@cli.command(name="info")
def info_command(app: Starlite) -> None:
    """Show information about the detected Starlite app."""
    _show_app_info(app)


@cli.command()
@option("-r", "--reload", help="Reload server on changes", default=False)
@option("-p", "--port", help="Serve under this port", type=int, default=8000)
@option("--host", help="Server under this host", default="127.0.0.1")
@option("--app", "app_path", help="Starlite app to run")
def run(reload: bool, port: int, host: str, app_path: str | None) -> None:
    """Run a Starlite app.

    The app can be either passed as a module path in the form of <module name>.<submodule>:<app instance>, or set as an
    environment variable STARLITE_APP with the same format. If none of those is given, the app will be automatically
    discovered from a file with one of the canonical names: app.py, asgi.py or application.py
    """
    try:
        import uvicorn
    except ImportError:
        raise _StarliteCLIException("Uvicorn needs to be installed to run an app")  # pylint: disable=W0707

    if not app_path:
        app_path, app = _autodiscover_app()
    else:
        app = _load_app_from_path(app_path)

    _show_app_info(app)

    console.rule("[yellow]Starting server process", align="left")

    uvicorn.run(app_path, reload=reload, host=host, port=port)


@cli.command()
def routes(app: Starlite) -> None:
    """Display information about the application's routes."""
    from starlite import HTTPRoute, WebSocketRoute
    from starlite.utils.helpers import unwrap_partial

    tree = rich.tree.Tree("", hide_root=True)

    for route in sorted(app.routes, key=lambda r: r.path):
        if isinstance(route, HTTPRoute):
            branch = tree.add(f"[green]{route.path}[/green] (HTTP)")
            for handler in route.route_handlers:
                handler_info = [
                    f"[blue]{handler.name or handler.handler_name}[/blue]",
                ]

                if inspect.iscoroutinefunction(unwrap_partial(handler.fn.value)):
                    handler_info.append("[magenta]async[/magenta]")
                else:
                    handler_info.append("[yellow]sync[/yellow]")

                handler_info.append(f'[cyan]{", ".join(sorted(handler.http_methods))}[/cyan]')

                if len(handler.paths) > 1:
                    for path in handler.paths:
                        branch.add(" ".join([f"[green]{path}[green]", *handler_info]))
                else:
                    branch.add(" ".join(handler_info))

        else:
            if isinstance(route, WebSocketRoute):
                route_type = "WS"
            else:
                route_type = "ASGI"
            branch = tree.add(f"[green]{route.path}[/green] ({route_type})")
            branch.add(f"[blue]{route.route_handler.name or route.route_handler.handler_name}[/blue]")

    console.print(tree)


def _get_session_backend(app: Starlite) -> ServerSideBackend:
    from starlite import DefineMiddleware
    from starlite.middleware.session.base import ServerSideBackend, SessionMiddleware
    from starlite.utils import is_class_and_subclass

    for middleware in app.middleware:
        if isinstance(middleware, DefineMiddleware):
            if not is_class_and_subclass(middleware.middleware, SessionMiddleware):
                continue
            backend = middleware.kwargs["backend"]
            if not isinstance(backend, ServerSideBackend):
                raise _StarliteCLIException("Only server-side backends are supported")
            return backend
    raise _StarliteCLIException("Session middleware not installed")


@cli.group()
def sessions() -> None:
    """Manage server-side sessions."""


@sessions.command("delete")
@argument("session-id")
def delete_session(session_id: str, app: Starlite) -> None:
    """Delete a specific session."""
    import anyio

    backend = _get_session_backend(app)
    if not anyio.run(backend.get, session_id):
        console.print(f"[red]Session {session_id!r} not found")
        return

    if Confirm.ask(f"Delete session {session_id!r}?"):
        anyio.run(backend.delete, session_id)
        console.print(f"[green]Deleted session {session_id!r}")


@sessions.command("clear")
def clear_sessions(app: Starlite) -> None:
    """Delete all sessions."""
    import anyio

    backend = _get_session_backend(app)

    if Confirm.ask("[red]Delete all active sessions?"):
        anyio.run(backend.delete_all)
        console.print("[green]All active sessions deleted")
