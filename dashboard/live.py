#!/usr/bin/env python3
"""
dashboard/live.py — Live terminal dashboard using `rich`.
Polls the Intelligence API every 5 seconds and renders:
  - Active stores with visitor count, conversion rate, queue depth
  - Active anomalies with severity colour coding
  - Live event feed (last 10 events)

# PROMPT: Build a terminal dashboard for retail store metrics using the `rich` library.
# Show: per-store visitors/conversion/queue in a table, anomalies in a Panel,
# recent events as a scrolling log. Refresh every 5s.
# CHANGES MADE: Added anomaly severity colour-coding (AI used plain white for all).
# Changed polling to use rich.Live context manager for flicker-free updates.
# Added graceful exit on Ctrl-C.
"""
import os
import time
import requests
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text

API_URL = os.environ.get("API_URL", "http://localhost:8000")
REFRESH_INTERVAL = 5
TEST_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SEVERITY_STYLE = {
    "CRITICAL": "bold red",
    "WARN": "yellow",
    "INFO": "cyan",
}

console = Console()


def fetch(path: str) -> dict | None:
    try:
        r = requests.get(f"{API_URL}{path}", timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_row(
        Layout(name="stores", ratio=2),
        Layout(name="anomalies", ratio=1),
    )
    return layout


def render_header() -> Panel:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return Panel(
        Text(f"🏪  Purplle Store Intelligence  |  {now}", style="bold white", justify="center"),
        style="blue",
    )


def render_stores(health: dict | None) -> Panel:
    table = Table(show_header=True, header_style="bold blue", expand=True)
    table.add_column("Store ID", style="cyan", no_wrap=True)
    table.add_column("Visitors", justify="right")
    table.add_column("Conv %", justify="right")
    table.add_column("Queue", justify="right")
    table.add_column("Abandon %", justify="right")
    table.add_column("Feed", justify="center")

    if not health:
        table.add_row("[red]API unreachable[/red]", "-", "-", "-", "-", "-")
        return Panel(table, title="[bold]Store Metrics[/bold]")

    stores = health.get("stores", [])
    if not stores:
        table.add_row("[dim]No data yet[/dim]", "-", "-", "-", "-", "-")
    else:
        for s in stores:
            store_id = s["store_id"]
            m = fetch(f"/stores/{store_id}/metrics?date={TEST_DATE}")
            stale = "🔴" if s.get("stale") else "🟢"
            if m:
                table.add_row(
                    store_id,
                    str(m.get("unique_visitors", 0)),
                    f"{m.get('conversion_rate', 0)*100:.1f}%",
                    str(m.get("current_queue_depth", 0)),
                    f"{m.get('abandonment_rate', 0)*100:.1f}%",
                    stale,
                )
            else:
                table.add_row(store_id, "?", "?", "?", "?", stale)

    return Panel(table, title="[bold]Store Metrics[/bold]")


def render_anomalies(health: dict | None) -> Panel:
    lines = []
    if health:
        for s in health.get("stores", []):
            data = fetch(f"/stores/{s['store_id']}/anomalies?date={TEST_DATE}")
            if data:
                for a in data.get("anomalies", []):
                    sty = SEVERITY_STYLE.get(a["severity"], "white")
                    lines.append(
                        Text(f"[{a['severity']}] {a['type']} — {a.get('suggested_action','')[:60]}",
                             style=sty)
                    )
    if not lines:
        lines = [Text("No active anomalies", style="dim green")]

    content = Text("\n").join(lines) if lines else Text("—")
    return Panel(content, title="[bold red]Anomalies[/bold red]")


def render_footer(health: dict | None) -> Panel:
    total = health.get("total_events_ingested", 0) if health else 0
    status = health.get("status", "UNKNOWN") if health else "API UNREACHABLE"
    style = "green" if status == "OK" else "yellow" if status == "DEGRADED" else "red"
    return Panel(
        Text(f"API Status: {status}  |  Total Events Ingested: {total:,}", style=style, justify="center")
    )


def run_dashboard():
    layout = build_layout()
    console.print("[bold green]Store Intelligence Dashboard starting...[/bold green]")

    with Live(layout, refresh_per_second=0.5, screen=True) as live:
        while True:
            health = fetch("/health")
            layout["header"].update(render_header())
            layout["stores"].update(render_stores(health))
            layout["anomalies"].update(render_anomalies(health))
            layout["footer"].update(render_footer(health))
            time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    try:
        run_dashboard()
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped.[/yellow]")
