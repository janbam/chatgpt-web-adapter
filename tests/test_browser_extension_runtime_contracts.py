from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


HARNESS = Path(__file__).with_name("browser_extension_runtime_contracts.mjs")


def _run_contract(name: str) -> None:
    """Execute one dependency-free browser-worker contract in Node."""

    node = shutil.which("node")
    assert node is not None, "Node.js is required to verify browser extension behavior"
    completed = subprocess.run(
        [node, str(HARNESS), name],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_provisional_conversation_identity_is_rejected_at_runtime() -> None:
    _run_contract("canonical-identity")


def test_canonical_bearer_stays_inside_browser_runtime() -> None:
    _run_contract("canonical-bearer")
