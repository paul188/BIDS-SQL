"""
cli.py
------
Command-line interface for the BIDS-SQL input pipeline.

Usage:
    python cli.py <folder> [--no-validate] [--rules]

Commands are thin wrappers around input_pipeline.run_pipeline(); all
business logic lives in input_pipeline.py.
"""

import argparse
import asyncio
import sys

from rules import DEFAULT_BIDS_RULESET


def _cmd_run(args: argparse.Namespace) -> None:
    """Run the full discovery → index → validate pipeline."""
    from input_pipeline import run_pipeline
    asyncio.run(run_pipeline(args.folder, skip_validation=args.no_validate))


def _cmd_rules(_args: argparse.Namespace) -> None:
    """Print all rules in the default ruleset and exit."""
    print(DEFAULT_BIDS_RULESET)
    print()
    print(DEFAULT_BIDS_RULESET.as_human_readable_list())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bids-sql",
        description="Discover, index, and validate BIDS datasets under a folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index everything under /data/studies and run bids-validator on each
  python cli.py /data/studies

  # Index only (skip validation — useful if bids-validator is not installed)
  python cli.py /data/studies --no-validate

  # Print all active discovery rules and exit
  python cli.py --rules
        """,
    )

    parser.add_argument(
        "folder",
        nargs="?",           # optional so --rules can run without a folder
        help="Root folder to scan for BIDS datasets",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        default=False,
        help="Skip the bids-validator step (steps 4+5)",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        default=False,
        help="Print the active discovery rules and exit",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.rules:
        _cmd_rules(args)
        return

    if not args.folder:
        parser.error("A folder argument is required unless --rules is passed.")

    _cmd_run(args)


if __name__ == "__main__":
    main(sys.argv[1:])
