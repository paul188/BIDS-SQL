"""
cli.py
------
Command-line interface for the BIDS-SQL input pipeline.

Subcommands:
    index   Discover, index, and validate a local BIDS folder
    rules   Print the active discovery rules and exit

Remote ingesters (index-openneuro, index-datalad) live in the separate
openneuro-bids-crawler repo which installs bids-sql as a dependency.
"""

import argparse
import asyncio
import logging
import sys

from rules import DEFAULT_BIDS_RULESET


# ─── Subcommand handlers ───────────────────────────────────────────────────────

def _cmd_index(args: argparse.Namespace) -> None:
    from input_pipeline import run_pipeline
    asyncio.run(run_pipeline(args.folder, skip_validation=args.no_validate))


def _cmd_rules(_args: argparse.Namespace) -> None:  # noqa: ARG001
    print(DEFAULT_BIDS_RULESET)
    print()
    print(DEFAULT_BIDS_RULESET.as_human_readable_list())


# ─── Argument parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bids-sql",
        description="Index and query BIDS datasets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index a local folder (full pipeline with validation)
  python cli.py index /data/studies

  # Skip validation (faster, useful without bids-validator installed)
  python cli.py index /data/studies --no-validate

  # Print active discovery rules
  python cli.py rules
        """,
    )

    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # ── index ─────────────────────────────────────────────────────────────────
    p_index = sub.add_parser(
        "index",
        help="Discover, index, and validate a local BIDS folder",
    )
    p_index.add_argument("folder", help="Root folder to scan for BIDS datasets")
    p_index.add_argument(
        "--no-validate",
        action="store_true",
        default=False,
        help="Skip the bids-validator step (steps 4+5)",
    )
    p_index.set_defaults(func=_cmd_index)

    # ── rules ─────────────────────────────────────────────────────────────────
    p_rules = sub.add_parser("rules", help="Print active discovery rules and exit")
    p_rules.set_defaults(func=_cmd_rules)

    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
