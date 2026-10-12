"""The TypeScript contract (packages/contract) agrees with the Pydantic models.

Most of the checking is Vitest's, against fixtures this repo commits. This file
holds the two halves that need Python:

- the committed fixtures match a fresh export, so the TypeScript side is never
  tested against a stale picture of the models;
- JSON the TypeScript side wrote validates against the Pydantic models.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from directory_pipeline.domain import models as m

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contract" / "fixtures"
EXPORTER = ROOT / "scripts" / "export_contract_fixtures.py"
FROM_TS = FIXTURES / "from-ts" / "cases.json"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$")


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


def _normalize(value: Any) -> Any:
    """Compare datetimes as instants: TypeScript writes .123Z, Pydantic .123000Z."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str) and _ISO.match(value):
        return datetime.fromisoformat(value)
    return value


def test_typescript_output_validates_in_pydantic() -> None:
    if not FROM_TS.exists():
        if os.environ.get("CONTRACT_REQUIRE_FROM_TS") == "1":
            pytest.fail(f"{FROM_TS} is missing: run `pnpm test` first")
        pytest.skip("no TypeScript output yet: run `pnpm test`")

    cases = json.loads(FROM_TS.read_text(encoding="utf-8"))["cases"]
    assert cases, "the TypeScript side wrote no cases"

    failures: list[str] = []
    for case in cases:
        label = f"{case['model']}/{case['case']}"
        try:
            parsed = getattr(m, case["model"]).model_validate(case["data"])
        except ValidationError as exc:
            failures.append(f"{label}: Pydantic rejected it\n{exc}")
            continue
        if _normalize(parsed.model_dump(mode="json")) != _normalize(case["data"]):
            failures.append(f"{label}: Pydantic read back a different value")
    assert not failures, "\n\n".join(failures)
