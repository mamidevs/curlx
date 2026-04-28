"""
Rich-based terminal output formatting for curlx CLI.

Provides beautiful, colored output for HTTP responses including:
- Status code color coding
- JSON syntax highlighting
- Headers as tables
- Response metadata panels
- Timing information
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from rich import box
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


class OutputFormatter:
    """
    Formats HTTP responses for beautiful terminal display.
    """

    def __init__(self, no_color: bool = False, verbose: bool = False) -> None:
        self.console = Console(
            color_system=None if no_color else "auto",
            force_terminal=not no_color,
        )
        self.verbose = verbose
        self._no_color = no_color

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
    def print_request_summary(
        self,
        method: str,
        url: str,
        status_code: int,
        elapsed_ms: float,
        content_length: Optional[int] = None,
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
        self.console.print(text)

    def print_headers(self, headers: Dict[str, str], title: str = "Response Headers") -> None:
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

        self.console.print(table)

    def print_body(
        self,
        body: Any,
        *,
        content_type: Optional[str] = None,
        pretty: bool = True,
        title: str = "Response Body",
    ) -> None:
        """
        Print response body with syntax highlighting.
        Supports JSON, HTML, XML, and plain text.
        """
        if body is None:
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
        request_headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        redirects: Optional[list] = None,
        ssl_info: Optional[Dict[str, Any]] = None,
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
            self.console.print(table)

        if redirects:
            table = Table(title="Redirects", box=box.SIMPLE_HEAD, header_style="bold magenta")
            table.add_column("#", style="cyan", width=4)
            table.add_column("URL", style="green")
            for i, url in enumerate(redirects, 1):
                table.add_row(str(i), url)
            self.console.print(table)

        if ssl_info:
            table = Table(title="SSL Info", box=box.SIMPLE_HEAD, header_style="bold magenta")
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="green")
            for k, v in ssl_info.items():
                table.add_row(k, str(v))
            self.console.print(table)

    def print_error(self, message: str, *, detail: Optional[str] = None) -> None:
        """Print an error message."""
        self.console.print(Panel(
            f"[bold red]Error:[/bold red] {message}"
            + (f"\n[dim]{detail}[/dim]" if detail else ""),
            border_style="red",
            title="❌ curlx",
        ))

    def print_success(self, message: str) -> None:
        """Print a success message."""
        self.console.print(f"[bold green]✓[/bold green] {message}")

    def print_info(self, message: str) -> None:
        """Print an info message."""
        self.console.print(f"[dim]ℹ {message}[/dim]")

    def write_to_file(self, content: str, path: str) -> None:
        """Write raw content to a file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.console.print(f"[dim]💾 Saved to {path}[/dim]")

    # ------------------------------------------------------------------
    # Private renderers
    # ------------------------------------------------------------------
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
        for unit in ("B", "KB", "MB", "GB"):
            if abs(num) < 1024.0:
                return f"{num:.1f} {unit}"
            num /= 1024.0
        return f"{num:.1f} TB"


def get_console(no_color: bool = False) -> Console:
    """Get a pre-configured Rich Console."""
    return Console(
        color_system=None if no_color else "auto",
        force_terminal=not no_color,
    )
