"""Command-line tools, run from the repo root as `python -m app.cli <command>`.

- `version`: print the app name and installed version

Later phases add the browser and triage commands. The real work lives in other modules;
this file only parses arguments and prints results.
"""

import argparse
import sys
from collections.abc import Callable
from importlib.metadata import version

from app.config import Settings, get_settings


def cmd_version(settings: Settings, _args: argparse.Namespace) -> int:
    print(f"{settings.app_name} {version('askfirst')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("version", help="print the app name and version").set_defaults(
        handler=cmd_version
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Callable[[Settings, argparse.Namespace], int] = args.handler
    return handler(get_settings(), args)


if __name__ == "__main__":
    sys.exit(main())
