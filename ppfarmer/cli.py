"""Launcher and command line.

Running `python -m ppfarmer` with no argument starts the local server and
opens the browser: that is the whole application. The other commands stay
available for scripting, but nothing requires them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from .api import OsuApiError, OsuClient
from .crawler import band_for, crawl, plan_band, resolve_user
from .discovery import COUNTRY_POOL
from .paths import default_db
from .rank import parse_duration, rank_beatmaps
from .store import (CACHE_MAX_AGE_DAYS, COMMON_MODS, Store, parse_mod_filter,
                    parse_profile, user_score_index)

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ppfarmer",
        description="Recommends maps that players just above your rank are farming.",
    )
    sub = p.add_subparsers(dest="command")

    sv = sub.add_parser("serve", help="Open the browser interface (default)")
    sv.add_argument("--port", type=int, default=8000, help="Port to listen on (default 8000)")
    sv.add_argument("--mode", default="osu", choices=["osu", "taiko", "fruits", "mania"])
    sv.add_argument("--no-open", action="store_true",
                    help="Do not open the browser automatically")
    sv.add_argument("--db", default=None, help="SQLite cache file")

    rec = sub.add_parser("recommend", help="Print the ranking in the terminal")
    rec.add_argument("user", nargs="?", default=None,
                     help="Profile URL, id or username (default: the saved profile)")
    rec.add_argument("--mode", default="osu", choices=["osu", "taiko", "fruits", "mania"])
    rec.add_argument("--players", type=int, default=None,
                     help="How many players to analyse just above you. More "
                          "meaningful than --spread: US players are out of "
                          "reach, so 1000 ranks only yield about 700")
    rec.add_argument("--spread", type=int, default=None,
                     help="Same in ranks (default: the band of the last crawl)")
    rec.add_argument("--top-n", type=int, default=10,
                     help="Top depth counted per player (default 10)")
    rec.add_argument("--limit", type=int, default=30, help="How many rows to print")
    rec.add_argument("--min-players", type=int, default=None,
                     help="Support threshold (default: 3%% of the panel, minimum 5)")
    rec.add_argument("--sort", default="players",
                     choices=["players", "gain", "pp", "efficiency"],
                     help="Popularity (default), estimated pp gain, raw pp, "
                          "or pp per minute of play")
    rec.add_argument("--target", type=int, default=None,
                     help="Stop discovery once N players are found")
    rec.add_argument("--stars-min", type=float, default=None, help="Minimum star rating")
    rec.add_argument("--stars-max", type=float, default=None, help="Maximum star rating")
    rec.add_argument("--length-min", default=None, metavar="LENGTH",
                     help="Minimum played length (90, 90s, 1m30, 1:30)")
    rec.add_argument("--length-max", default=None, metavar="LENGTH",
                     help="Maximum played length (120, 2m, 2m30)")
    rec.add_argument("--year-min", type=int, default=None, metavar="YEAR",
                     help="Only count scores set from this year onwards")
    rec.add_argument("--year-max", type=int, default=None, metavar="YEAR",
                     help="Only count scores set up to this year")
    rec.add_argument("--scoring", default="all", choices=["all", "stable", "lazer"],
                     help="Restrict to one game version: stable (Classic mod) "
                          "or lazer (default: both)")
    rec.add_argument("--mods", default=None, metavar="LIST",
                     help="Keep only these combinations, comma separated: "
                          + ", ".join(COMMON_MODS) + " or 'other' (e.g. HD,HR,HDDT)")
    rec.add_argument("--no-crawl", action="store_true",
                     help="Use the cache only, no crawl requests")
    rec.add_argument("--hide-played", action="store_true",
                     help="Hide maps already in your top 100 (they are shown and "
                          "flagged by default, since a score can be improved)")
    rec.add_argument("--db", default=None, help="SQLite cache file")

    st = sub.add_parser("status", help="Local cache summary")
    st.add_argument("--db", default=None, help="SQLite cache file")
    return p


def cmd_status(args: argparse.Namespace) -> int:
    db = args.db or default_db()
    with Store(db) as store:
        s = store.stats()
        profile = store.profile()
        band = store.last_band()
    console.print(f"[bold]Cache[/] {db}")
    console.print(f"  profile        : {profile or 'not set'}")
    if band:
        console.print(f"  crawled band   : #{band[0]:,} to #{band[1]:,}")
    console.print(f"  players known  : {s['players']:,}")
    console.print(f"  tops fetched   : {s['with_scores']:,}")
    console.print(f"  scores stored  : {s['scores']:,}")
    console.print(f"  maps known     : {s['beatmaps']:,}")
    return 0


def resolve_band(store: Store, rank: int, args: argparse.Namespace,
                 client: OsuClient | None = None
                 ) -> tuple[int, int, int | None, list | None]:
    """Band to analyse, the player target if one was asked for, and the
    country list when it had to be fetched.

    Priority: --players, then --spread, then the band of the last crawl, and
    finally 1000 ranks.
    """
    target = getattr(args, "players", None)
    if target and client is not None:
        # Sizing a band for a headcount needs to know how much of the
        # population is still reachable at that depth.
        low, high, countries = plan_band(client, rank, target, args.mode)
        return low, high, target, countries
    if args.spread:
        return (*band_for(rank, args.spread), None, None)
    saved = store.last_band()
    if saved:
        return (*saved, None, None)
    return (*band_for(rank, 1000), None, None)


def _run_crawl(client: OsuClient, store: Store, args: argparse.Namespace,
               low: int, high: int, target_players: int | None = None,
               countries: list | None = None) -> tuple[int, int]:
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(), console=console,
    ) as prog:
        disc = prog.add_task("Discovery (countries)", total=COUNTRY_POOL)
        tops = prog.add_task("Fetching tops", total=None, visible=False)

        def on_country(code: str, found: int, active: int) -> None:
            prog.update(disc, advance=1, description=f"Discovery {code} - {found} players")

        def on_player(i: int, total: int) -> None:
            prog.update(tops, completed=i, total=total, visible=True,
                        description="Fetching tops")

        report = crawl(client, store, low, high, args.mode, top_n=50,
                       target=args.target,
                       on_country=on_country, on_player=on_player,
                       target_players=target_players, countries=countries)

    low, high = report.get("band", (low, high))
    console.print(
        f"#{low:,}-#{high:,}: {report['discovered']:,} players discovered, "
        f"{report['crawled']:,} tops fetched."
    )
    skipped = report.get("skipped", 0)
    if skipped:
        console.print(
            f"[dim]{skipped:,} players skipped: their tops are less than "
            f"{CACHE_MAX_AGE_DAYS} days old.[/]"
        )
    if report.get("unreachable"):
        listed = ", ".join(
            f"{code} ({share * 100:.0f}%)" for code, share in report["unreachable"][:5]
        )
        console.print(
            f"[yellow]Out of reach:[/] {listed}. Their country ranking stops "
            f"before your band (10,000 cap per country), so about "
            f"{report['lost_share'] * 100:.0f}% of players at your level are missing."
        )
    console.print()
    return low, high


def cmd_recommend(args: argparse.Namespace) -> int:
    # Explicit path: find_dotenv() walks the call stack and fails when the
    # program is not launched as a script.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    with Store(args.db or default_db()) as store:
        client_id, secret = store.credentials()
    client = OsuClient(client_id, secret)
    with client, Store(args.db or default_db()) as store:
        who = parse_profile(args.user) if args.user else store.profile()
        if not who:
            console.print(
                "[red]No profile.[/] Pass one (a URL, id or username) or set it "
                "once in the browser interface with [bold]python -m ppfarmer[/]."
            )
            return 2
        me = resolve_user(client, who, args.mode)
        store.set_meta("profile", str(me["user_id"]))
        low, high, target, countries = resolve_band(
            store, me["global_rank"], args, client)
        console.print(
            f"[bold]{me['username']}[/] - rank #{me['global_rank']:,} "
            f"({me['pp']:.0f}pp, {me['country']})"
        )
        console.print(f"Band: #{low:,} to #{high:,}\n")

        if not args.no_crawl:
            low, high = _run_crawl(client, store, args, low, high,
                                   target, countries)

        # A single call gives the pp curve, the maps already played, and the
        # detail of your own scores (accuracy, FC) to display.
        my_top = client.user_best(me["user_id"], args.mode, limit=100)
        user_pps = [s["pp"] for s in my_top if s.get("pp") is not None]
        my_scores = user_score_index(my_top)
        exclude = set(my_scores) if args.hide_played else set()

        maps, panel = rank_beatmaps(
            store, low, high, top_n=args.top_n, exclude=exclude,
            limit=args.limit, min_stars=args.stars_min, max_stars=args.stars_max,
            user_pps=user_pps, sort=args.sort, min_players=args.min_players,
            user_scores=my_scores,
            min_length=parse_duration(args.length_min),
            max_length=parse_duration(args.length_max),
            mods_filter=parse_mod_filter(args.mods), scoring=args.scoring,
            year_min=args.year_min, year_max=args.year_max,
        )
        requests_used = client.request_count

    if not panel:
        console.print("[yellow]No player from this band has been fetched yet.[/]")
        return 1
    if not maps:
        console.print("[yellow]No map matches these filters.[/]")
        return 1

    console.print(
        f"[bold]Maps farmed between #{low:,} and #{high:,}[/]  "
        f"[dim]({panel:,} players, top {args.top_n}, sorted by {args.sort})[/]\n"
    )

    for i, m in enumerate(maps, start=1):
        stars = f"{m['stars']:.2f}*" if m["stars"] is not None else "?"
        pp = (f"{m['typical_pp']:.0f}med/{m['mean_pp']:.0f}mean over {m['mods_players']}"
              if m["typical_pp"] is not None else "?")
        # Stable / lazer split, only when both are present.
        versions = ""
        if m["stable_players"] and m["lazer_players"]:
            versions = f" ({m['stable_players']}st/{m['lazer_players']}lz)"
        elif m["stable_players"]:
            versions = " (stable)"
        gain = m["gain"]
        if gain is None:
            gain_txt = "[dim]unknown gain[/]"
        elif gain < 0.05:
            gain_txt = "[dim]no gain[/]"
        else:
            gain_txt = f"[green]+{gain:.1f}pp[/]"
        if m["played_length"]:
            secs = int(m["played_length"])
            length = f"{secs // 60}m{secs % 60:02d}"
            if m["pp_per_min"] and args.sort == "efficiency":
                length += f" ({m['pp_per_min']:.0f}pp/min)"
        else:
            length = "unknown length"
        year = f" | med {m['median_year']}" if m["median_year"] else ""
        # escape(): osu! titles contain brackets, which rich would read as markup.
        label = escape(f"{m['artist']} - {m['title']}")
        if m["version"]:
            label += escape(f" [{m['version']}]")
        played = r" [yellow]\[PLAYED][/]" if m["played"] else ""
        console.print(f"[dim]{i:>3}.[/] {label}{played}")
        console.print(
            f"     [bold]{m['players']:,}[/] players ({m['share']*100:.0f}%) | "
            f"{stars} | {m['mods']} | {pp}{versions} | {gain_txt} | {length}{year} | "
            f"[dim]osu.ppy.sh/b/{m['beatmap_id']}[/]"
        )
        if m["played"]:
            bits = []
            if m["my_pp"] is not None:
                bits.append(f"{m['my_pp']:.0f}pp")
            if m["my_accuracy"] is not None:
                bits.append(f"{m['my_accuracy'] * 100:.2f}%")
            if m["my_rank"]:
                bits.append(m["my_rank"])
            if m["my_mods"]:
                bits.append(m["my_mods"])
            if m["my_fc"] is True:
                bits.append("[green]FC[/]")
            elif m["my_fc"] is False:
                bits.append("[dim]no FC[/]")
            margin = ""
            if m["my_pp"] is not None and m["typical_pp"] is not None:
                delta = m["typical_pp"] - m["my_pp"]
                margin = (f" | [green]{delta:.0f}pp to gain[/]" if delta > 0
                          else " | [dim]already above the band[/]")
            console.print(f"     [yellow]your score:[/] {' | '.join(bits)}{margin}")

    if len(user_pps) >= 100:
        console.print(
            f"\n[dim]Your 100th score is worth {min(user_pps):.0f}pp: below that, a "
            f"map earns you nothing.[/] Try [bold]--sort gain[/]."
        )
    console.print(f"[dim]{requests_used} API requests this run.[/]")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # No argument at all: the browser interface is the application.
    if not argv:
        argv = ["serve"]
    elif argv[0] not in {"serve", "recommend", "status", "-h", "--help"}:
        argv.insert(0, "recommend")
    args = build_parser().parse_args(argv)

    if args.command == "status":
        return cmd_status(args)
    if args.command == "serve":
        from .web import serve
        try:
            serve(args.db, args.mode, args.port, not args.no_open)
            return 0
        except OsuApiError as exc:
            console.print(f"[red]Error:[/] {exc}")
            return 2
    try:
        return cmd_recommend(args)
    except ValueError as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 2
    except OsuApiError as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. The cache keeps what was fetched.[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
