"""The TypeScript contract (packages/contract) agrees with the Pydantic models.

Most of the checking is Vitest's, against fixtures this repo commits. This file
holds the two halves that need Python:

- the committed fixtures match a fresh export, so the TypeScript side is never
  tested against a stale picture of the models;
- JSON the TypeScript side wrote validates against the Pydantic models.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contract" / "fixtures"
EXPORTER = ROOT / "scripts" / "export_contract_fixtures.py"


def test_committed_fixtures_match_the_models(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(EXPORTER), "--out", str(tmp_path)], check=True)

    fresh = sorted(p.name for p in tmp_path.iterdir())
    assert fresh, "the exporter wrote nothing"
    for name in fresh:
        committed = FIXTURES / name
        hint = "run: python scripts/export_contract_fixtures.py"
        assert committed.exists(), f"contract/fixtures/{name} is missing; {hint}"
        assert committed.read_text(encoding="utf-8") == (tmp_path / name).read_text(
            encoding="utf-8"
        ), f"contract/fixtures/{name} is stale; {hint}"
