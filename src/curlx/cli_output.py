"""
Rich-based terminal output formatting for curlx CLI.

Provides beautiful, colored output for HTTP responses including:
- Status code color coding
- JSON syntax highlighting
- Headers as tables
- Response metadata panels
- Timing information

Output is split across two streams: the response body goes to stdout and every
diagnostic (summary line, headers, warnings, errors) goes to stderr, so that
``curlx get URL | jq`` and ``curlx get URL > body.json`` see only the payload.
When stdout is not a terminal the body is written through verbatim instead of
being wrapped in a panel.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


class OutputFormatter:
    """
    Formats HTTP responses for beautiful terminal display.
    """

    def __init__(self, no_color: bool = False, verbose: bool = False) -> None:
        # Inlined rather than bound to a local so the literal type survives for
        # the type checker. No force_terminal: Rich must stay free to detect a
        # pipe and drop ANSI.
        self.console = Console(color_system=None if no_color else "auto")
        self.err_console = Console(color_system=None if no_color else "auto", stderr=True)
        self.verbose = verbose

    @staticmethod
    def _stdout_is_tty() -> bool:
        """True only when stdout is a real terminal, ignoring FORCE_COLOR."""
        try:
            return bool(sys.stdout.isatty())
        except (AttributeError, ValueError):  # detached or closed stream
            return False

    # ------------------------------------------------------------------
    # Status code colors
    # ------------------------------------------------------------------
    def _status_style(self, code: int) -> str:
        if 200 <= code < 300:
            return "bold green"
        elif 300 <= code < 400:
            return "bold yellow"
        elif 400 <= code < 500:
            return "bold red"
        elif 500 <= code < 600:
            return "bold bright_red"
        return "bold white"

    def _status_emoji(self, code: int) -> str:
        if 200 <= code < 300:
            return "✅"
        elif 300 <= code < 400:
            return "🔀"
        elif 400 <= code < 500:
            return "❌"
        elif 500 <= code < 600:
            return "💥"
        return "❓"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def blank_line(self) -> None:
        """Emit a spacer between diagnostic blocks."""
        self.err_console.print()

    def print_request_summary(
        self,
        method: str,
        url: str,
        status_code: int,
        elapsed_ms: float,
        content_length: int | None = None,
    ) -> None:
        """Print a compact summary line of the request/response."""
        emoji = self._status_emoji(status_code)
        style = self._status_style(status_code)

        size_str = ""
        if content_length is not None:
            size_str = f" | {self._format_bytes(content_length)}"

        text = Text.assemble(
            (f"{emoji} ", ""),
            (f"{method.upper()}", "bold cyan"),
            (" → ", "dim"),
            (f"{url}", "cyan underline"),
            ("  ", ""),
            (f"{status_code}", style),
            (f"  {elapsed_ms:.0f}ms{size_str}", "dim"),
        )
        self.err_console.print(text)

    def print_headers(self, headers: dict[str, str], title: str = "Response Headers") -> None:
        """Print headers as a rich table."""
        if not headers:
            return

        table = Table(
            title=title,
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold magenta",
            title_style="bold magenta",
            expand=True,
        )
        table.add_column("Header", style="cyan", no_wrap=True, width=30)
        table.add_column("Value", style="green")

        for key, value in sorted(headers.items()):
            table.add_row(key, str(value))

        self.err_console.print(table)

    def print_body(
        self,
        body: Any,
        *,
        content_type: str | None = None,
        pretty: bool = True,
        title: str = "Response Body",
    ) -> None:
        """
        Print response body with syntax highlighting.
        Supports JSON, HTML, XML, and plain text.

        When stdout is not a terminal the body is written verbatim so it can be
        piped or redirected; panels and highlighting are terminal-only.
        """
        if body is None:
            return

        # Deliberately not `console.is_terminal`: Rich reports True whenever
        # FORCE_COLOR is set, which is common in CI. Whether the body may be
        # decorated depends on where it is actually going, not on whether the
        # user wants colour, so ask the stream itself.
        if not self._stdout_is_tty():
            self._write_raw(body)
            return

        if isinstance(body, bytes):
            # Try decode
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = body.decode("utf-8", errors="replace")
        else:
            text = str(body)

        if not text.strip():
            self.console.print(Panel("[dim]<empty body>[/dim]", title=title))
            return

        # Auto-detect content type
        ct = (content_type or "").lower()

        if "json" in ct or text.strip().startswith(("{", "[")):
            self._print_json(text, pretty=pretty, title=title)
        elif "html" in ct:
            self._print_html(text, title=title)
        elif "xml" in ct:
            self._print_xml(text, title=title)
        else:
            self._print_plain(text, title=title)

    def print_verbose_meta(
        self,
        *,
        request_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        redirects: list | None = None,
        ssl_info: dict[str, Any] | None = None,
    ) -> None:
        """Print verbose metadata panels."""
        if request_headers:
            self.print_headers(request_headers, title="Request Headers")

        if cookies:
            table = Table(title="Cookies", box=box.SIMPLE_HEAD, header_style="bold magenta")
            table.add_column("Name", style="cyan")
            table.add_column("Value", style="green")
            for k, v in cookies.items():
                table.add_row(k, v)
            self.err_console.print(table)

        if redirects:
            table = Table(title="Redirects", box=box.SIMPLE_HEAD, header_style="bold magenta")
            table.add_column("#", style="cyan", width=4)
            table.add_column("URL", style="green")
            for i, url in enumerate(redirects, 1):
                table.add_row(str(i), url)
            self.err_console.print(table)

        if ssl_info:
            table = Table(title="SSL Info", box=box.SIMPLE_HEAD, header_style="bold magenta")
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="green")
            for k, v in ssl_info.items():
                table.add_row(k, str(v))
            self.err_console.print(table)

    def print_error(self, message: str, *, detail: str | None = None) -> None:
        """Print an error message."""
        self.err_console.print(Panel(
            f"[bold red]Error:[/bold red] {message}"
            + (f"\n[dim]{detail}[/dim]" if detail else ""),
            border_style="red",
            title="❌ curlx",
        ))

    def print_unexpected(self, exc: BaseException) -> None:
        """
        Report a failure curlx never anticipated.

        Kept distinct from :meth:`print_error` so that a bug in curlx is not
        presented to the user as a network problem.
        """
        detail = f"{exc}\n"
        if not self.verbose:
            detail += "Re-run with --verbose for a traceback."
        self.err_console.print(Panel(
            f"[bold red]Internal error:[/bold red] {type(exc).__name__}\n"
            f"[dim]{detail}[/dim]"
            "\nThis is a bug in curlx, not a problem with the request.",
            border_style="red",
            title="💥 curlx",
        ))
        if self.verbose:
            self.err_console.print_exception(show_locals=False)

    def print_warning(self, message: str) -> None:
        """Print a non-fatal warning."""
        self.err_console.print(f"[yellow]![/yellow] [dim]{message}[/dim]")

    def print_success(self, message: str) -> None:
        """Print a success message."""
        self.err_console.print(f"[bold green]✓[/bold green] {message}")

    def write_to_file(self, content: str, path: str) -> None:
        """
        Write raw content to a file.

        Lets :class:`OSError` propagate; the CLI turns it into a clean message.
        """
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.print_success(f"Saved to {path}")

    # ------------------------------------------------------------------
    # Private renderers
    # ------------------------------------------------------------------
    def _write_raw(self, body: Any) -> None:
        """Pass the body straight through to stdout, byte for byte where possible."""
        file = self.console.file
        if isinstance(body, bytes):
            buffer = getattr(file, "buffer", None)
            if buffer is not None:
                buffer.write(body)
                buffer.flush()
                return
            text = body.decode("utf-8", errors="replace")
        else:
            text = str(body)
        file.write(text)
        file.flush()

    def _print_json(self, text: str, *, pretty: bool = True, title: str) -> None:
        try:
            data = json.loads(text)
            if pretty:
                json_str = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                json_str = text
            renderable = RichJSON(json_str, highlight=True)
            self.console.print(Panel(renderable, title=title, border_style="green"))
        except json.JSONDecodeError:
            self._print_plain(text, title=title)

    def _print_html(self, text: str, title: str) -> None:
        syntax = Syntax(text, "html", theme="monokai", line_numbers=self.verbose)
        self.console.print(Panel(syntax, title=title, border_style="orange3"))

    def _print_xml(self, text: str, title: str) -> None:
        syntax = Syntax(text, "xml", theme="monokai", line_numbers=self.verbose)
        self.console.print(Panel(syntax, title=title, border_style="orange3"))

    def _print_plain(self, text: str, title: str) -> None:
        self.console.print(Panel(text, title=title, border_style="blue"))

    @staticmethod
    def _format_bytes(num: int) -> str:
        size = float(num)
        for unit in ("B", "KB", "MB", "GB"):
            if abs(size) < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
