"""The examples in the README and the documentation site, held to the real API.

Examples rot first, because nothing runs them, and running them would mean
calling ENA. So these check what can be checked offline: every Python example
parses and calls only functions, methods and arguments that exist, and every
command line example is one the parser accepts.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import re
import shlex
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import enaportal
from enaportal.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent

PAGES = sorted([ROOT / "README.md", *(ROOT / "docs").rglob("*.md")])

# Fences inside a tab or an admonition are indented, and close at the same
# indent they opened at.
_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)```(?P<lang>[\w-]*)[^\n]*\n(?P<body>.*?)^(?P=indent)```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

_SEPARATORS = frozenset({"|", "||", "&&", ";", "&"})
_REDIRECTS = frozenset({">", ">>", "<"})


def _load_hooks() -> ModuleType:
    path = ROOT / "scripts" / "docs_hooks.py"
    spec = importlib.util.spec_from_file_location("docs_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocks(page: Path, lang: str) -> list[str]:
    return [
        textwrap.dedent(match["body"])
        for match in _FENCE.finditer(page.read_text(encoding="utf-8"))
        if match["lang"] == lang
    ]


def _page_id(page: Path) -> str:
    return str(page.relative_to(ROOT))


def _commands(block: str) -> Iterator[list[str]]:
    """Each enaportal invocation in a shell block, without its redirections."""
    for line in block.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        command: list[str] = []
        for token in [*lexer, ";"]:
            if token not in _SEPARATORS:
                command.append(token)
                continue
            if command and command[0] == "enaportal":
                yield _without_redirects(command[1:])
            command = []


def _without_redirects(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif token in _REDIRECTS:
            skip = True
        else:
            kept.append(token)
    return kept


class _Namespace:
    """What each name in one example is bound to, as far as enaportal is concerned."""

    def __init__(self) -> None:
        self.bound: dict[str, Any] = {"enaportal": enaportal}
        self.clients: dict[str, type] = {}

    def learn(self, tree: ast.Module) -> list[str]:
        problems: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "enaportal":
                for alias in node.names:
                    if hasattr(enaportal, alias.name):
                        self.bound[alias.asname or alias.name] = getattr(enaportal, alias.name)
                    else:
                        problems.append(f"enaportal has no {alias.name!r} to import")
            elif isinstance(node, ast.With):
                for item in node.items:
                    target = item.optional_vars
                    call = item.context_expr
                    if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
                        continue
                    held = self.resolve(call.func)
                    if isinstance(held, type):
                        self.clients[target.id] = held
        return problems

    def resolve(self, node: ast.expr) -> Any:
        """The object an expression names, or None if it is not enaportal's."""
        if isinstance(node, ast.Name):
            return self.bound.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = self.bound.get(node.value.id)
            if owner is not None:
                return getattr(owner, node.attr, _MISSING)
            client = self.clients.get(node.value.id)
            if client is None:
                return None
            if node.attr in _instance_attributes(client):
                return None
            return getattr(client, node.attr, _MISSING)
        return None

    def holds_client(self, node: ast.expr) -> bool:
        """Whether this is a method reached through a client, which is unbound here."""
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self.clients
        )


_MISSING = object()


def _instance_attributes(cls: type) -> set[str]:
    """The attributes a class sets on self in __init__, which the class itself lacks."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls.__init__)))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def _check_call(call: ast.Call, namespace: _Namespace) -> str | None:
    target = namespace.resolve(call.func)
    where = ast.unparse(call.func)
    if target is _MISSING:
        return f"{where} does not exist"
    if target is None or not callable(target):
        return None
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return None
    positional = [None] * (len(call.args) + namespace.holds_client(call.func))
    try:
        inspect.signature(target).bind(*positional, **dict.fromkeys(k.arg for k in call.keywords))
    except TypeError as exc:
        return f"{ast.unparse(call)}: {exc}"
    return None


def _python_problems(block: str) -> list[str]:
    tree = ast.parse(block)
    namespace = _Namespace()
    problems = namespace.learn(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and namespace.resolve(node) is _MISSING:
            problems.append(f"{ast.unparse(node)} does not exist")
        elif isinstance(node, ast.Call):
            problem = _check_call(node, namespace)
            if problem is not None:
                problems.append(problem)
    return sorted(set(problems))


@pytest.mark.parametrize("page", PAGES, ids=_page_id)
def test_python_examples_call_only_what_exists(page: Path) -> None:
    for block in _blocks(page, "python"):
        assert _python_problems(block) == [], block


@pytest.mark.parametrize("page", PAGES, ids=_page_id)
def test_command_examples_parse(page: Path) -> None:
    parser = build_parser()
    for block in _blocks(page, "bash"):
        for argv in _commands(block):
            try:
                parser.parse_args(argv)
            except SystemExit:
                pytest.fail(f"the parser rejects: enaportal {shlex.join(argv)}")


def test_the_pages_have_examples_to_check() -> None:
    python = sum(len(_blocks(page, "python")) for page in PAGES)
    commands = sum(len(list(_commands(b))) for page in PAGES for b in _blocks(page, "bash"))
    assert python > 40
    assert commands > 30


def test_the_checks_catch_a_wrong_example() -> None:
    wrong = """
from enaportal import PortalClient

enaportal.serch("read_run")
enaportal.search("read_run", limt=10)
with PortalClient() as ena:
    ena.search_to_file("out.tsv", "read_run", qeury="tax_tree(4932)")
"""
    problems = _python_problems(wrong)
    assert any("enaportal.serch does not exist" in problem for problem in problems)
    assert any("limt" in problem for problem in problems)
    assert any("qeury" in problem for problem in problems)


def test_commands_are_split_at_pipes_and_stripped_of_redirections() -> None:
    block = (
        "enaportal related PRJEB1787 | enaportal fetch - > runs.xml\n"
        "enaportal search read_run --query 'tax_tree(4932)' -f run_accession \\\n"
        "  --limit 10  # a comment\n"
        "aria2c -i files.txt\n"
    )
    assert list(_commands(block)) == [
        ["related", "PRJEB1787"],
        ["fetch", "-"],
        ["search", "read_run", "--query", "tax_tree(4932)", "-f", "run_accession", "--limit", "10"],
    ]


def test_the_command_reference_covers_every_subcommand() -> None:
    rendered = _load_hooks().render_commands()
    subcommands = next(
        action.choices
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for name in subcommands:
        assert f"## {name}\n" in rendered
        assert f"usage: enaportal {name}" in rendered
    assert "\x1b[" not in rendered


def test_the_command_reference_replaces_only_its_marker() -> None:
    hooks = _load_hooks()
    assert hooks.on_page_markdown("# Other page\n") == "# Other page\n"
    assert "usage: enaportal" in hooks.on_page_markdown(f"# Commands\n\n{hooks.MARKER}\n")
