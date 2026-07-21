"""Prompt-polarity convention — every LLM prompt declares its polarity.

Phase D carryover #5 (from 2026-04-26 audit): the
``extension_is_standalone`` polarity bug (True = default, False =
affirmative) cost $3.45 of wasted LLM spend across four states before
it was caught. Root cause was the validator and the prompt disagreeing
on which value carries evidence spans; nothing in the prompt file
declared its polarity so authoring a new fact meant reading the whole
prompt to infer it.

This test treats drift between a prompt's declared polarity and
``INVERTED_POLARITY_BOOL_FACTS`` membership as a pytest failure so a
future polarity bug surfaces in CI rather than mid-fanout.

Convention: each prompt file's leading ``{# ... #}`` author block
carries a line of the form:

    Polarity: <descriptor>.

where ``<descriptor>`` is one of:

- ``standard (True = affirmative claim; spans required when True)``
- ``inverted (True = default; False = affirmative claim with spans)``
- ``n/a (count-int ...)``  — for count-int facts
- ``n/a (enum3 ...)``      — for enum facts
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.score.extract import SUPPORTED_FACTS, prompt_path_for
from src.score.schema import (
    ENUM_VALUED_FACTS,
    INT_VALUED_FACTS,
    INVERTED_POLARITY_BOOL_FACTS,
)

_POLARITY_RE = re.compile(r"Polarity:\s*([^\n.]+)\.", re.IGNORECASE)


def _leading_comment_block(prompt_path: Path) -> str:
    """Return the text of the leading ``{# ... #}`` Jinja comment block.

    Raises ``AssertionError`` on a prompt missing the block entirely —
    every prompt in ``SUPPORTED_FACTS`` must open with an author block.
    """
    raw = prompt_path.read_text(encoding="utf-8")
    m = re.match(r"\s*\{#(.*?)#\}", raw, re.DOTALL)
    assert m is not None, f"{prompt_path.name}: missing leading {{# ... #}} block"
    return m.group(1)


def _declared_polarity(prompt_path: Path) -> str:
    block = _leading_comment_block(prompt_path)
    m = _POLARITY_RE.search(block)
    assert m is not None, (
        f"{prompt_path.name}: missing 'Polarity: ...' line in leading comment. "
        f"See tests/test_score_prompt_polarity.py docstring for the convention."
    )
    return m.group(1).strip().lower()


@pytest.mark.parametrize("fact", SUPPORTED_FACTS)
def test_prompt_declares_polarity(fact: str) -> None:
    """Every LLM-fact prompt declares its polarity in the leading block."""
    path = prompt_path_for(fact)
    polarity = _declared_polarity(path)
    assert polarity, f"{fact}: Polarity declaration empty"


@pytest.mark.parametrize("fact", SUPPORTED_FACTS)
def test_declared_polarity_matches_schema(fact: str) -> None:
    """Declared polarity must match ``INVERTED_POLARITY_BOOL_FACTS``
    membership + ``INT_VALUED_FACTS`` / ``ENUM_VALUED_FACTS`` kind.

    Drift between the prompt's declared polarity and the runtime set
    breaks the validator's evidence-span policy. POC-2 caught
    ``extension_is_standalone`` only after 100% downgrade on AZ — this
    test asserts the same thing the validator does, at import time.
    """
    path = prompt_path_for(fact)
    polarity = _declared_polarity(path)

    is_int = fact in INT_VALUED_FACTS
    is_enum = fact in ENUM_VALUED_FACTS
    is_inverted = fact in INVERTED_POLARITY_BOOL_FACTS

    if is_int:
        assert "n/a" in polarity and "count" in polarity, (
            f"{fact}: declared polarity {polarity!r} does not mark this as a count-int fact"
        )
    elif is_enum:
        assert "n/a" in polarity and "enum" in polarity, (
            f"{fact}: declared polarity {polarity!r} does not mark this as an enum fact"
        )
    elif is_inverted:
        assert polarity.startswith("inverted"), (
            f"{fact}: INVERTED_POLARITY_BOOL_FACTS member must declare "
            f"'Polarity: inverted'; got {polarity!r}"
        )
    else:
        assert polarity.startswith("standard"), (
            f"{fact}: standard bool fact must declare 'Polarity: standard'; "
            f"got {polarity!r}. If polarity actually inverted, add the fact "
            f"to INVERTED_POLARITY_BOOL_FACTS in score/schema.py."
        )


def test_polarity_header_stripped_from_system_prompt() -> None:
    """The Polarity header is an author annotation — it must not reach
    the LLM. ``_load_template`` strips the leading ``{# ... #}`` block
    before the split; this test guards the coupling."""
    from src.score.extract import _load_template

    for fact in SUPPORTED_FACTS:
        system_text, _ = _load_template(prompt_path_for(fact))
        assert "Polarity:" not in system_text, (
            f"{fact}: Polarity declaration leaked into the SYSTEM prompt — "
            f"authors would pollute LLM context"
        )
