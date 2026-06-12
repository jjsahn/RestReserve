"""Command-line interface.

Exit codes: 0 booked (or dry-run selected a slot), 1 config/auth/venue error,
2 timed out without booking.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from restreserve import __version__
from restreserve.config import ConfigError, load_config
from restreserve.log import setup_logging
from restreserve.resy_client import ResyClient, ResyError
from restreserve.sniper import Sniper

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as e:
        log.error("%s", e)
        return EXIT_ERROR
    except ResyError as e:
        log.error("%s", e)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("interrupted — nothing booked")
        return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restreserve",
        description="Book a Resy reservation the instant slots open.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug logging (every request)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snipe = sub.add_parser("snipe", help="wait for the drop and book")
    add_config_arg(snipe)
    snipe.add_argument("--dry-run", action="store_true",
                       help="full pipeline but never books")
    snipe.add_argument("--venue-id", type=int)
    snipe.add_argument("--venue-slug")
    snipe.add_argument("--location", help='e.g. "new-york-ny" (with --venue-slug)')
    snipe.add_argument("--date", help="reservation date YYYY-MM-DD")
    snipe.add_argument("--party-size", type=int)
    snipe.add_argument("--window", help="time window HH:MM-HH:MM, e.g. 18:30-20:00")
    snipe.add_argument("--ideal-time", help="preferred time HH:MM")
    snipe.add_argument("--table-type", help="soft preference, e.g. 'dining room'")
    snipe.add_argument("--drop-time",
                       help='ISO datetime when slots open, e.g. "2026-06-13T09:00:00-04:00"')
    snipe.add_argument("--lead-ms", type=int, help="start polling this early (default 1000)")
    snipe.add_argument("--poll-interval-ms", type=int, help="default 250")
    snipe.add_argument("--max-duration-s", type=int, help="give up after (default 180)")
    snipe.set_defaults(func=cmd_snipe)

    search = sub.add_parser("venue-search", help="find a venue's id and slug")
    search.add_argument("query")
    add_config_arg(search)
    search.set_defaults(func=cmd_venue_search)

    auth = sub.add_parser("auth-test", help="verify credentials and payment method")
    add_config_arg(auth)
    auth.set_defaults(func=cmd_auth_test)

    return parser


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", type=Path, default=None,
        help="config file (default: ./config.json if it exists)",
    )


def resolve_config_path(args: argparse.Namespace) -> Path | None:
    if args.config is not None:
        return args.config
    default = Path("config.json")
    return default if default.exists() else None


def cmd_snipe(args: argparse.Namespace) -> int:
    overrides: dict = {
        "venue_id": args.venue_id,
        "venue_slug": args.venue_slug,
        "location": args.location,
        "date": args.date,
        "party_size": args.party_size,
        "ideal_time": args.ideal_time,
        "table_type": args.table_type,
        "drop_time": args.drop_time,
        "lead_ms": args.lead_ms,
        "poll_interval_ms": args.poll_interval_ms,
        "max_duration_s": args.max_duration_s,
        "dry_run": True if args.dry_run else None,
    }
    if args.window:
        try:
            start, end = args.window.split("-", 1)
        except ValueError:
            log.error("--window must look like 18:30-20:00")
            return EXIT_ERROR
        overrides["window_start"] = start.strip()
        overrides["window_end"] = end.strip()

    cfg = load_config(resolve_config_path(args), overrides)
    client = ResyClient(cfg.api_key, timeout=cfg.request_timeout_s)
    try:
        result = Sniper(cfg, client).run()
    finally:
        client.close()

    if result is None:
        return EXIT_TIMEOUT
    if result.dry_run:
        log.info("dry run complete: would have booked %s", result.slot)
    return EXIT_OK


def cmd_venue_search(args: argparse.Namespace) -> int:
    cfg = _auth_only_config(args)
    client = ResyClient(cfg.api_key, timeout=cfg.request_timeout_s)
    try:
        if cfg.auth_token:
            client.set_auth_token(cfg.auth_token)
        elif cfg.email and cfg.password:
            client.authenticate(cfg.email, cfg.password)
        hits = client.venue_search(args.query)
    finally:
        client.close()
    if not hits:
        log.warning("no venues found for %r", args.query)
        return EXIT_ERROR
    print(f"{'venue_id':>9}  {'slug':<35} {'location':<16} name")
    for hit in hits:
        print(f"{str(hit['venue_id']):>9}  {str(hit['slug']):<35} "
              f"{str(hit['location']):<16} {hit['name']}")
    return EXIT_OK


def cmd_auth_test(args: argparse.Namespace) -> int:
    cfg = _auth_only_config(args)
    client = ResyClient(cfg.api_key, timeout=cfg.request_timeout_s)
    try:
        if cfg.auth_token:
            client.set_auth_token(cfg.auth_token)
            pm = client.fetch_payment_method()
            token = cfg.auth_token
        else:
            session = client.authenticate(cfg.email, cfg.password)
            token, pm = session.token, session.payment_method_id
    finally:
        client.close()
    log.info("auth OK — token %s...%s", token[:6], token[-4:])
    if pm is None:
        log.warning("no payment method found; most venues require one to book")
    else:
        log.info("payment method id: %d", pm)
    return EXIT_OK


def _auth_only_config(args: argparse.Namespace):
    """Load config for subcommands that only need credentials.

    Fills in dummy target/drop fields so full validation passes.
    """
    return load_config(
        resolve_config_path(args),
        {
            "venue_id": 1,
            "date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            "drop_time": (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
            ).isoformat(),
        },
    )


if __name__ == "__main__":
    sys.exit(main())
