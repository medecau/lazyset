"""Run the code examples in the guides so they cannot rot silently.

Each guide executes top to bottom as one session, in one namespace: fenced
`python` blocks are `exec`'d, `pycon` transcripts are checked with the stdlib
`doctest` module, and both see the same names. That is the whole mechanism —
no plugin, no collection hook. A reader following the guide from the top gets
what the test gets, and an example that leans on state the prose never set up
fails here instead of misleading them.

Two HTML comments, invisible in rendered Markdown, opt a fence out of that
session. Put either above the fence — blank lines in between are fine, since
Markdown formatters insert them around an HTML block:

    <!-- example: skip -->      Not runnable: placeholder metasyntax, another
                                backend's driver, a table the guide never
                                creates. Checked for syntax only.

    <!-- example: isolated -->  Runnable, but a side example rather than a step
                                in the session. It runs against a copy of the
                                namespace, so rebinding `db` (or closing it)
                                cannot poison everything below.

A marker that binds to no fence is an error rather than a silent no-op — a
typo in one is invisible in the rendered page, and would quietly put a block
back into the session.

Transcripts always run against a copy — `doctest` copies the globals it is
handed — so a `pycon` block reads the session but never writes to it.
"""

import doctest
import io
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GUIDES = ["README.md", "docs/quickstart.md", "docs/queries.md"]

_FENCE = re.compile(
    r"^(?:<!-- example: (?P<mode>skip|isolated) -->\n(?:[ \t]*\n)*)?"
    r"```(?P<lang>python|pycon)\n(?P<code>.*?)^```",
    re.MULTILINE | re.DOTALL,
)
_ANY_MARKER = re.compile(r"^<!-- example:.*-->$", re.MULTILINE)


class Block:
    """One fenced example, with the file line its first code line sits on."""

    def __init__(self, mode: str | None, lang: str, line: int, code: str):
        self.mode = mode
        self.lang = lang
        self.line = line
        self.code = code


def _blocks(text: str) -> Iterator[Block]:
    for match in _FENCE.finditer(text):
        yield Block(
            match.group("mode"),
            match.group("lang"),
            text.count("\n", 0, match.start("code")) + 1,
            match.group("code"),
        )


def _orphan_markers(text: str) -> list[int]:
    """Line numbers of `example:` markers that no fence picked up."""
    bound = {m.start() for m in _FENCE.finditer(text) if m.group("mode")}
    return [
        text.count("\n", 0, m.start()) + 1
        for m in _ANY_MARKER.finditer(text)
        if m.start() not in bound
    ]


def _source(block: Block) -> str:
    """Pad the block so tracebacks point at its real line in the Markdown."""
    return "\n" * (block.line - 1) + block.code


def _run_transcript(block: Block, globs: dict[str, object], path: Path) -> None:
    test = doctest.DocTestParser().get_doctest(
        block.code, globs, path.name, str(path), block.line - 1
    )
    # A ```pycon fence with no >>> would otherwise pass by having nothing to
    # check — the failure mode that makes a green doctest run meaningless.
    assert test.examples, f"{path}:{block.line} pycon fence has no >>> examples"

    report = io.StringIO()
    result = doctest.DocTestRunner(verbose=False).run(test, out=report.write)
    if result.failed:
        pytest.fail(f"{path}:{block.line}\n{report.getvalue()}", pytrace=False)


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_examples(
    guide: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ROOT / guide
    text = path.read_text()
    blocks = list(_blocks(text))
    assert blocks, f"{guide} has no python/pycon fences — did the tags change?"

    orphans = _orphan_markers(text)
    assert not orphans, (
        f"{guide}: `example:` marker on line(s) {orphans} is attached to no "
        "python/pycon fence — check the spelling and what follows it"
    )

    # Examples connect to relative SQLite paths and to a bare connect(); keep
    # the files they create, and the default URL, out of the working tree.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    session: dict[str, object] = {}
    for block in blocks:
        if block.mode == "skip":
            compile(_source(block), str(path), "exec")
            continue
        globs = dict(session) if block.mode == "isolated" else session
        if block.lang == "python":
            exec(compile(_source(block), str(path), "exec"), globs)
        else:
            _run_transcript(block, globs, path)
