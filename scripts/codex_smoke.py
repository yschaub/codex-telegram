#!/usr/bin/env python3
"""Codex integration smoke test: new turn + resumed turn.

Runs the same SDK path used by the Telegram bot and fails fast if either
response is empty or the resumed session cannot continue.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from src.codex.sdk_integration import CodexSDKManager
from src.config.loader import load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt-1",
        default="Say hello in one short sentence.",
        help="Prompt for initial turn.",
    )
    parser.add_argument(
        "--prompt-2",
        default="Now answer in one sentence that confirms this is a resumed session.",
        help="Prompt for resumed turn.",
    )
    parser.add_argument(
        "--working-directory",
        default=None,
        help="Override working directory (defaults to APPROVED_DIRECTORY).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override Codex timeout (seconds).",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = load_config()
    if args.timeout and args.timeout > 0:
        config.codex_timeout_seconds = args.timeout

    manager = CodexSDKManager(config)
    if not manager.codex_path:
        print("FAIL: Codex CLI not found (check PATH/CODEX_CLI_PATH).")
        return 1

    working_directory = (
        Path(args.working_directory).expanduser().resolve()
        if args.working_directory
        else Path(config.approved_directory).resolve()
    )
    if not working_directory.exists():
        print(f"FAIL: working directory does not exist: {working_directory}")
        return 1

    print(f"Using Codex: {manager.codex_path}")
    print(f"Working directory: {working_directory}")

    first = await manager.execute_command(
        prompt=args.prompt_1,
        working_directory=working_directory,
        session_id=None,
        continue_session=False,
    )
    if not first.content.strip():
        print("FAIL: first turn returned empty content.")
        return 1
    if not first.session_id:
        print("FAIL: first turn did not return a session ID.")
        return 1

    second = await manager.execute_command(
        prompt=args.prompt_2,
        working_directory=working_directory,
        session_id=first.session_id,
        continue_session=True,
    )
    if not second.content.strip():
        print("FAIL: resumed turn returned empty content.")
        return 1
    if second.session_id != first.session_id:
        print(
            "FAIL: resumed turn returned a different session ID "
            f"({first.session_id} -> {second.session_id})."
        )
        return 1

    print("PASS: Codex new+resume smoke test succeeded.")
    print(f"Session ID: {second.session_id}")
    print(f"Turn 1 preview: {first.content[:120].replace(chr(10), ' ')}")
    print(f"Turn 2 preview: {second.content[:120].replace(chr(10), ' ')}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except Exception as exc:
        print(f"FAIL: smoke test crashed: {exc}")
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()

