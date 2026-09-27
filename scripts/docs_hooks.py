"""MkDocs hooks for the documentation site.

The command reference is rendered from the parser the command runs on, at build
time, so the page cannot describe an option that no longer exists.
"""

from __future__ import annotations

import argparse
import re
from functools import partial
from typing import Any, Final

from enaportal.cli import build_parser

MARKER: Final = "<!-- enaportal-commands -->"

HELP_WIDTH: Final = 88

# Python 3.14 colours argparse help when it decides a terminal is attached,
# which `mkdocs serve` in a terminal is.
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")


def on_page_markdown(markdown: str, **kwargs: Any) -> str:
    """Replace the marker with the help text of every subcommand."""
    if MARKER not in markdown:
        return markdown
    return markdown.replace(MARKER, render_commands())


def render_commands() -> str:
    """The help of the command and each subcommand, as Markdown sections."""
    parser = build_parser()
    sections = [_help_block(parser)]
    for name, subcommand in _subcommands(parser).items():
        sections.append(f"## {name}\n\n{_help_block(subcommand)}")
    return "\n\n".join(sections) + "\n"


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    # argparse has no public way to list subcommands.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _help_block(parser: argparse.ArgumentParser) -> str:
    # Pinned so the page does not rewrap with the width of whoever builds it.
    parser.formatter_class = partial(argparse.HelpFormatter, width=HELP_WIDTH)
    text = _ANSI.sub("", parser.format_help()).rstrip()
    return f"```text\n{text}\n```"
