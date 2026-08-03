"""
curlx CLI - Terminal HTTP client built on curlx SDK.

Usage:
    curlx get  https://api.example.com/users
    curlx post https://api.example.com/users -H "Content-Type: application/json" \
        --json '{"name":"foo"}'
    curlx get  https://api.example.com/users --impersonate chrome136 --proxy http://proxy:8080 -v

The response body goes to stdout and everything else to stderr, so the body can
be piped or redirected on its own. See ``ExitCode`` for the exit-code contract.
"""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated, Any

import typer
from rich import box
from rich.table import Table

from curlx.cli_output import OutputFormatter
from curlx.client import SyncHttpClient
from curlx.exceptions import (
    CurlxError,
    HttpStatusError,
    ProfileNotFoundError,
    TransportError,
)

# Track the SDK default (_BaseClient.__init__). "chrome" is the alias for the
# latest Chrome, so the CLI does not go stale - and therefore start advertising an
# outdated fingerprint - every time curl_cffi adds a version.
DEFAULT_IMPERSONATE = "chrome"
DEFAULT_TIMEOUT = 30.0


class ExitCode(IntEnum):
    """
    Process exit codes.

    Code 2 is deliberately absent: Click reserves it for command-line parsing
    errors and raises it before any curlx code runs.
    """

    OK = 0
    USAGE = 1
    NETWORK = 3
    HTTP_ERROR = 4
    OUTPUT_WRITE = 5
    INTERNAL = 6


EXIT_CODE_HELP = (
    "[bold]Exit codes:[/bold] "
    "[cyan]0[/cyan] success · "
    "[cyan]1[/cyan] usage/validation error · "
    "[cyan]2[/cyan] command-line parsing error · "
    "[cyan]3[/cyan] network or transport failure · "
    "[cyan]4[/cyan] HTTP 4xx/5xx with --fail · "
    "[cyan]5[/cyan] could not write --output file · "
    "[cyan]6[/cyan] unexpected internal error"
)

app = typer.Typer(
    name="curlx",
    help="Stealth HTTP client CLI built on curl_cffi",
    epilog=EXIT_CODE_HELP,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------
UrlArgument = Annotated[str, typer.Argument(help="Target URL")]
HeaderOption = Annotated[
    list[str] | None,
    typer.Option(
        "--header",
        "-H",
        help="Add a header (repeatable). Format: 'Key: Value', or 'Key;' to send it empty",
    ),
]
ProxyOption = Annotated[
    str | None,
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
    str | None,
    typer.Option("--json", "-j", help="JSON body string"),
]
DataOption = Annotated[
    str | None,
    typer.Option("--data", "-d", help="Raw body string"),
]
OutputOption = Annotated[
    str | None,
    typer.Option("--output", "-o", help="Write response body to file"),
]
PrettyOption = Annotated[
    bool,
    typer.Option(
        "--pretty/--no-pretty",
        "-p/-P",
        help="Pretty-print JSON output (interactive terminal only; piped output is verbatim)",
    ),
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
    str | None,
    typer.Option("--referer", "-e", help="Referer header"),
]
UaOption = Annotated[
    str | None,
    typer.Option("--user-agent", "-A", help="Custom User-Agent string"),
]
FailOption = Annotated[
    bool,
    typer.Option("--fail", "-f", help="Exit with code 4 when the response is 4xx or 5xx"),
]


@dataclass(frozen=True)
class RequestOptions:
    """
    Everything a verb command collects.

    Bundling these keeps ``_do_request`` off a 15-argument positional signature,
    where inserting a parameter silently misaligns every call site.
    """

    method: str
    url: str
    headers: list[str] | None = None
    proxy: str | None = None
    impersonate: str = DEFAULT_IMPERSONATE
    timeout: float = DEFAULT_TIMEOUT
    json_body: str | None = None
    data_body: str | None = None
    output: str | None = None
    pretty: bool = True
    verbose: bool = False
    no_color: bool = False
    insecure: bool = False
    referer: str | None = None
    user_agent: str | None = None
    fail: bool = False


def _parse_headers(header_list: list[str] | None, fmt: OutputFormatter) -> dict[str, str]:
    """
    Parse curl-style ``-H`` arguments into a dict.

    ``Key: Value`` sets a header and ``Key;`` sends it with an empty value, as in
    curl. The client takes a dict, so a repeated key can only be last-wins —
    warn rather than drop the earlier value silently.
    """
    headers: dict[str, str] = {}
    seen: dict[str, str] = {}  # lowercased name -> spelling already stored
    for raw in header_list or []:
        if ":" in raw:
            name, value = raw.split(":", 1)
        elif raw.endswith(";"):
            name, value = raw[:-1], ""
        else:
            fmt.print_warning(
                f"Ignoring malformed header {raw!r}, expected 'Key: Value' or 'Key;'"
            )
            continue

        name, value = name.strip(), value.strip()
        if not name:
            fmt.print_warning(f"Ignoring header {raw!r} with an empty name")
            continue

        lowered = name.lower()
        if lowered in seen:
            fmt.print_warning(f"Duplicate header {name!r} overrides the earlier value")
            headers.pop(seen[lowered], None)
        seen[lowered] = name
        headers[name] = value
    return headers


def _profile_suggestion(name: str) -> str:
    """Build a 'did you mean' hint for an unknown impersonation profile."""
    from curlx.fingerprint import list_profiles

    profiles = list_profiles()
    close = difflib.get_close_matches(name, profiles, n=3, cutoff=0.4)
    if close:
        return "Did you mean: " + ", ".join(close) + "?  (curlx profiles lists them all)"
    return f"{len(profiles)} profiles are available; run 'curlx profiles' to list them."


def _is_known_profile(name: str) -> bool:
    from curlx.fingerprint import list_profiles

    return name in list_profiles()


def _build_client(
    opts: RequestOptions,
    headers: dict[str, str],
    fmt: OutputFormatter,
) -> SyncHttpClient:
    """Build a SyncHttpClient from CLI arguments, reporting a bad profile cleanly."""
    proxies = None
    if opts.proxy:
        proxies = {"http": opts.proxy, "https": opts.proxy}

    try:
        return SyncHttpClient(
            impersonate=opts.impersonate,
            proxies=proxies,
            timeout=opts.timeout,
            verify=not opts.insecure,
            default_headers=headers,
        )
    except (ProfileNotFoundError, ValueError) as exc:
        # fingerprint.get_profile still raises a bare ValueError, so accept both.
        # A ValueError raised for any other reason is not a profile problem and
        # must not be mislabelled as one.
        if _is_known_profile(opts.impersonate):
            raise
        fmt.print_error(
            f"Unknown impersonation profile: {opts.impersonate!r}",
            detail=_profile_suggestion(opts.impersonate),
        )
        raise typer.Exit(code=ExitCode.USAGE) from exc


def _do_request(opts: RequestOptions, fmt: OutputFormatter) -> None:
    """Execute the HTTP request and print formatted output."""
    parsed_headers = _parse_headers(opts.headers, fmt)
    if opts.referer:
        parsed_headers["Referer"] = opts.referer
    if opts.user_agent:
        parsed_headers["User-Agent"] = opts.user_agent

    client = _build_client(opts, parsed_headers, fmt)

    request_kwargs: dict[str, Any] = {}
    if opts.json_body:
        try:
            request_kwargs["json"] = json.loads(opts.json_body)
        except json.JSONDecodeError as exc:
            fmt.print_error("Invalid JSON body", detail=str(exc))
            raise typer.Exit(code=ExitCode.USAGE) from exc
    elif opts.data_body:
        request_kwargs["data"] = opts.data_body

    start = time.perf_counter()
    try:
        with client:
            resp = client.request(opts.method, opts.url, **request_kwargs)
    except HttpStatusError as exc:
        fmt.print_error(f"HTTP {exc.status_code}", detail=str(exc))
        raise typer.Exit(code=ExitCode.HTTP_ERROR) from exc
    except TransportError as exc:
        fmt.print_error(f"Request failed: {type(exc).__name__}", detail=str(exc))
        raise typer.Exit(code=ExitCode.NETWORK) from exc
    except CurlxError as exc:
        # Retry exhaustion, open circuit breaker, exhausted proxy pool, ...
        fmt.print_error(f"{type(exc).__name__}", detail=str(exc))
        raise typer.Exit(code=ExitCode.NETWORK) from exc

    elapsed_ms = (time.perf_counter() - start) * 1000
    content_length = len(resp.content)

    # Summary
    fmt.print_request_summary(
        method=opts.method,
        url=resp.url,
        status_code=resp.status_code,
        elapsed_ms=elapsed_ms,
        content_length=content_length,
    )

    if opts.verbose:
        fmt.blank_line()
        fmt.print_verbose_meta(
            request_headers=parsed_headers,
            cookies=dict(resp.cookies) if resp.cookies else None,
        )
        # curl_cffi's Headers can yield None values; normalize for display.
        fmt.print_headers({k: v or "" for k, v in resp.headers.items()})
        fmt.blank_line()

    # Body
    if opts.output:
        try:
            fmt.write_to_file(resp.text, opts.output)
        except OSError as exc:
            fmt.print_error(f"Cannot write to {opts.output!r}", detail=str(exc))
            raise typer.Exit(code=ExitCode.OUTPUT_WRITE) from exc
    else:
        ct = resp.headers.get("content-type", "")
        fmt.print_body(
            resp.content,
            content_type=ct,
            pretty=opts.pretty,
            title=f"Body ({content_length} bytes)",
        )

    if opts.fail and resp.status_code >= 400:
        fmt.print_error(
            f"HTTP {resp.status_code} and --fail was given",
            detail=f"{opts.method} {resp.url}",
        )
        raise typer.Exit(code=ExitCode.HTTP_ERROR)


def _dispatch(opts: RequestOptions) -> None:
    """Run a request, turning any failure into a clean message and an exit code."""
    fmt = OutputFormatter(no_color=opts.no_color, verbose=opts.verbose)
    try:
        _do_request(opts, fmt)
    except typer.Exit:
        raise
    except Exception as exc:
        fmt.print_unexpected(exc)
        raise typer.Exit(code=ExitCode.INTERNAL) from exc


# ---------------------------------------------------------------------------
# Commands
#
# The verbs share one parameter block per shape, built by the factories below,
# so adding an option means editing one signature instead of six. Each factory
# returns a real function with real annotations, so every command still has its
# own introspectable --help.
# ---------------------------------------------------------------------------
def _make_bodyless_command(method: str) -> Callable[..., None]:
    """Build the command function for a verb that sends no request body."""

    def command(
        url: UrlArgument,
        header: HeaderOption = None,
        proxy: ProxyOption = None,
        impersonate: ImpersonateOption = DEFAULT_IMPERSONATE,
        timeout: TimeoutOption = DEFAULT_TIMEOUT,
        output: OutputOption = None,
        pretty: PrettyOption = True,
        verbose: VerboseOption = False,
        no_color: NoColorOption = False,
        insecure: InsecureOption = False,
        referer: RefererOption = None,
        user_agent: UaOption = None,
        fail: FailOption = False,
    ) -> None:
        _dispatch(
            RequestOptions(
                method=method,
                url=url,
                headers=header,
                proxy=proxy,
                impersonate=impersonate,
                timeout=timeout,
                output=output,
                pretty=pretty,
                verbose=verbose,
                no_color=no_color,
                insecure=insecure,
                referer=referer,
                user_agent=user_agent,
                fail=fail,
            )
        )

    command.__name__ = method.lower()
    command.__doc__ = f"Send a {method} request."
    return command


def _make_body_command(method: str) -> Callable[..., None]:
    """Build the command function for a verb that can carry a request body."""

    def command(
        url: UrlArgument,
        header: HeaderOption = None,
        proxy: ProxyOption = None,
        impersonate: ImpersonateOption = DEFAULT_IMPERSONATE,
        timeout: TimeoutOption = DEFAULT_TIMEOUT,
        json_body: JsonOption = None,
        data: DataOption = None,
        output: OutputOption = None,
        pretty: PrettyOption = True,
        verbose: VerboseOption = False,
        no_color: NoColorOption = False,
        insecure: InsecureOption = False,
        referer: RefererOption = None,
        user_agent: UaOption = None,
        fail: FailOption = False,
    ) -> None:
        _dispatch(
            RequestOptions(
                method=method,
                url=url,
                headers=header,
                proxy=proxy,
                impersonate=impersonate,
                timeout=timeout,
                json_body=json_body,
                data_body=data,
                output=output,
                pretty=pretty,
                verbose=verbose,
                no_color=no_color,
                insecure=insecure,
                referer=referer,
                user_agent=user_agent,
                fail=fail,
            )
        )

    command.__name__ = method.lower()
    command.__doc__ = f"Send a {method} request."
    return command


_VERBS: tuple[tuple[str, bool], ...] = (
    ("GET", False),
    ("POST", True),
    ("PUT", True),
    ("PATCH", True),
    ("DELETE", True),
    ("HEAD", False),
)

for _method, _has_body in _VERBS:
    _factory = _make_body_command if _has_body else _make_bodyless_command
    app.command(name=_method.lower(), rich_help_panel="HTTP Methods")(_factory(_method))


# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------
@app.command(name="profiles", rich_help_panel="Utilities")
def profiles() -> None:
    """List supported browser impersonation profiles."""
    from curlx.fingerprint import list_profiles

    fmt = OutputFormatter()
    table = Table(title="Browser Profiles", box=box.ROUNDED)
    table.add_column("Profile Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="green")

    for name in list_profiles():
        table.add_row(name, f"Impersonate {name}")

    # The table is this command's payload, so it belongs on stdout.
    fmt.console.print(table)


@app.command(name="user-agents", rich_help_panel="Utilities")
def user_agents(
    browser: Annotated[
        str, typer.Argument(help="Browser name (chrome, firefox, safari, edge)")
    ] = "chrome",
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
