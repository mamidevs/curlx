"""
curlx CLI – Terminal HTTP client built on curlx SDK.

Usage:
    curlx get  https://api.example.com/users
    curlx post https://api.example.com/users -H "Content-Type: application/json" --json '{"name":"foo"}'
    curlx get  https://api.example.com/users --impersonate chrome136 --proxy http://proxy:8080 -v
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import typer
from rich import box
from rich.table import Table
from typing_extensions import Annotated

from curlx.cli_output import OutputFormatter
from curlx.client import SyncHttpClient
from curlx.headers import HeaderBuilder

app = typer.Typer(
    name="curlx",
    help="Stealth HTTP client CLI built on curl_cffi",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------
HeaderOption = Annotated[
    Optional[List[str]],
    typer.Option(
        "--header",
        "-H",
        help="Add a header (can be used multiple times). Format: 'Key: Value'",
    ),
]
ProxyOption = Annotated[
    Optional[str],
    typer.Option("--proxy", help="Proxy URL, e.g. http://user:pass@host:8080"),
]
ImpersonateOption = Annotated[
    str,
    typer.Option("--impersonate", "-i", help="Browser fingerprint to impersonate"),
]
TimeoutOption = Annotated[
    float,
    typer.Option("--timeout", "-t", help="Request timeout in seconds"),
]
JsonOption = Annotated[
    Optional[str],
    typer.Option("--json", "-j", help="JSON body string"),
]
DataOption = Annotated[
    Optional[str],
    typer.Option("--data", "-d", help="Raw body string"),
]
OutputOption = Annotated[
    Optional[str],
    typer.Option("--output", "-o", help="Write response body to file"),
]
PrettyOption = Annotated[
    bool,
    typer.Option("--pretty", "-p", help="Pretty-print JSON output"),
]
VerboseOption = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Verbose output (headers, timing, etc.)"),
]
NoColorOption = Annotated[
    bool,
    typer.Option("--no-color", help="Disable colored output"),
]
InsecureOption = Annotated[
    bool,
    typer.Option("--insecure", "-k", help="Disable SSL certificate verification"),
]
RefererOption = Annotated[
    Optional[str],
    typer.Option("--referer", "-e", help="Referer header"),
]
UaOption = Annotated[
    Optional[str],
    typer.Option("--user-agent", "-A", help="Custom User-Agent string"),
]


def _parse_headers(header_list: Optional[List[str]]) -> Dict[str, str]:
    """Parse 'Key: Value' strings into a dict."""
    headers: Dict[str, str] = {}
    if not header_list:
        return headers
    for h in header_list:
        if ":" not in h:
            typer.secho(f"Warning: invalid header format '{h}', expected 'Key: Value'", fg=typer.colors.YELLOW)
            continue
        key, value = h.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _build_client(
    proxy: Optional[str],
    impersonate: str,
    timeout: float,
    insecure: bool,
    headers: Dict[str, str],
) -> SyncHttpClient:
    """Build a SyncHttpClient from CLI arguments."""
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    return SyncHttpClient(
        impersonate=impersonate,
        proxies=proxies,
        timeout=timeout,
        verify=not insecure,
        default_headers=headers,
    )


def _do_request(
    method: str,
    url: str,
    headers: Optional[List[str]],
    proxy: Optional[str],
    impersonate: str,
    timeout: float,
    json_body: Optional[str],
    data_body: Optional[str],
    output: Optional[str],
    pretty: bool,
    verbose: bool,
    no_color: bool,
    insecure: bool,
    referer: Optional[str],
    user_agent: Optional[str],
) -> None:
    """Execute the HTTP request and print formatted output."""
    fmt = OutputFormatter(no_color=no_color, verbose=verbose)

    parsed_headers = _parse_headers(headers)
    if referer:
        parsed_headers["Referer"] = referer
    if user_agent:
        parsed_headers["User-Agent"] = user_agent

    client = _build_client(proxy, impersonate, timeout, insecure, parsed_headers)

    request_kwargs: Dict[str, Any] = {}
    if json_body:
        try:
            request_kwargs["json"] = json.loads(json_body)
        except json.JSONDecodeError as exc:
            fmt.print_error("Invalid JSON body", detail=str(exc))
            raise typer.Exit(code=1)
    elif data_body:
        request_kwargs["data"] = data_body

    start = time.perf_counter()
    try:
        with client:
            resp = client.request(method, url, **request_kwargs)
    except Exception as exc:
        fmt.print_error(f"{type(exc).__name__}", detail=str(exc))
        raise typer.Exit(code=1)

    elapsed_ms = (time.perf_counter() - start) * 1000
    content_length = len(resp.content)

    # Summary
    fmt.print_request_summary(
        method=method,
        url=resp.url,
        status_code=resp.status_code,
        elapsed_ms=elapsed_ms,
        content_length=content_length,
    )

    if verbose:
        fmt.console.print()
        fmt.print_verbose_meta(
            request_headers=parsed_headers,
            cookies=dict(resp.cookies) if resp.cookies else None,
        )
        fmt.print_headers(dict(resp.headers))
        fmt.console.print()

    # Body
    if output:
        fmt.write_to_file(resp.text, output)
    else:
        ct = resp.headers.get("content-type", "")
        fmt.print_body(
            resp.content,
            content_type=ct,
            pretty=pretty,
            title=f"Body ({content_length} bytes)",
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@app.command(rich_help_panel="HTTP Methods")
def get(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    output: OutputOption = None,
    pretty: PrettyOption = True,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a GET request."""
    _do_request(
        "GET", url, header, proxy, impersonate, timeout,
        None, None, output, pretty, verbose, no_color, insecure, referer, user_agent,
    )


@app.command(rich_help_panel="HTTP Methods")
def post(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    json_body: JsonOption = None,
    data: DataOption = None,
    output: OutputOption = None,
    pretty: PrettyOption = True,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a POST request."""
    _do_request(
        "POST", url, header, proxy, impersonate, timeout,
        json_body, data, output, pretty, verbose, no_color, insecure, referer, user_agent,
    )


@app.command(rich_help_panel="HTTP Methods")
def put(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    json_body: JsonOption = None,
    data: DataOption = None,
    output: OutputOption = None,
    pretty: PrettyOption = True,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a PUT request."""
    _do_request(
        "PUT", url, header, proxy, impersonate, timeout,
        json_body, data, output, pretty, verbose, no_color, insecure, referer, user_agent,
    )


@app.command(rich_help_panel="HTTP Methods")
def patch(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    json_body: JsonOption = None,
    data: DataOption = None,
    output: OutputOption = None,
    pretty: PrettyOption = True,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a PATCH request."""
    _do_request(
        "PATCH", url, header, proxy, impersonate, timeout,
        json_body, data, output, pretty, verbose, no_color, insecure, referer, user_agent,
    )


@app.command(rich_help_panel="HTTP Methods")
def delete(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    json_body: JsonOption = None,
    data: DataOption = None,
    output: OutputOption = None,
    pretty: PrettyOption = True,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a DELETE request."""
    _do_request(
        "DELETE", url, header, proxy, impersonate, timeout,
        json_body, data, output, pretty, verbose, no_color, insecure, referer, user_agent,
    )


@app.command(rich_help_panel="HTTP Methods")
def head(
    url: Annotated[str, typer.Argument(help="Target URL")],
    header: HeaderOption = None,
    proxy: ProxyOption = None,
    impersonate: ImpersonateOption = "chrome136",
    timeout: TimeoutOption = 30.0,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
    insecure: InsecureOption = False,
    referer: RefererOption = None,
    user_agent: UaOption = None,
) -> None:
    """Send a HEAD request (headers only)."""
    _do_request(
        "HEAD", url, header, proxy, impersonate, timeout,
        None, None, None, False, verbose, no_color, insecure, referer, user_agent,
    )


# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------
@app.command(rich_help_panel="Utilities")
def profiles() -> None:
    """List supported browser impersonation profiles."""
    from curlx.fingerprint import list_profiles

    fmt = OutputFormatter()
    table = Table(title="Browser Profiles", box=box.ROUNDED)
    table.add_column("Profile Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="green")

    for name in list_profiles():
        table.add_row(name, f"Impersonate {name}")

    fmt.console.print(table)


@app.command(rich_help_panel="Utilities")
def user_agents(
    browser: Annotated[str, typer.Argument(help="Browser name (chrome, firefox, safari, edge)")] = "chrome",
) -> None:
    """Show sample User-Agent strings."""
    from curlx.headers import rotate_user_agent

    fmt = OutputFormatter()
    table = Table(title=f"Sample User-Agents ({browser})", box=box.ROUNDED)
    table.add_column("#", style="cyan", width=4)
    table.add_column("User-Agent", style="green")

    for i in range(1, 4):
        ua = rotate_user_agent(browser)
        table.add_row(str(i), ua)

    fmt.console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    app()


if __name__ == "__main__":
    main()
