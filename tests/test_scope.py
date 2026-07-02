"""The scope guard (brief guardrail 5).

Fails if execution-layer code appears anywhere under src/: imports of
web3/eth_account/py_clob_client, or strings associated with order placement
and signing. This keeps the canonical repository exactly what it claims to
be -- a measurement instrument. It is not a usage restriction (MIT permits
forks to remove it); it prevents execution code from entering this
publication via contributions or automation.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

FORBIDDEN_IMPORTS = ("web3", "eth_account", "py_clob_client")
FORBIDDEN_STRINGS = ("private_key", "POST /order", "create_order", "eip712")

IMPORT_PATTERNS = [
    re.compile(rf"^\s*(import|from)\s+{re.escape(mod)}\b", re.MULTILINE)
    for mod in FORBIDDEN_IMPORTS
]


def _iter_source_files():
    files = sorted(SRC.rglob("*.py"))
    assert files, f"no Python sources found under {SRC}"
    return files


def test_no_forbidden_imports():
    violations = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in IMPORT_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path}: matches {pattern.pattern!r}")
    assert not violations, "execution-layer imports found:\n" + "\n".join(violations)


def test_no_forbidden_strings():
    violations = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8").lower()
        for needle in FORBIDDEN_STRINGS:
            if needle.lower() in text:
                violations.append(f"{path}: contains {needle!r}")
    assert not violations, "execution-layer strings found:\n" + "\n".join(violations)
