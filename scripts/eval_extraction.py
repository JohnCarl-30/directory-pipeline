"""Measure what each extraction layer actually recovers.

The fixtures are the ground truth: the mock site renders its pages *from*
Company records, so the correct answer for every field is already known. Each
company is rendered under all three templates -- microdata, drifted, stub --
which turns 12 records into 36 pages spanning rich markup, prose-only, and
near-empty.

Reported per layer and per field:

    recall     of the pages where the fact exists, how many we recovered
    precision  of the values we emitted, how many were right

Precision is the number that matters. A missing field shows up in the QA
coverage report; a wrong field silently corrupts entity resolution downstream.

No model is called: this measures layers 1 and 2 only, and reports what is
left over as the work layer 3 would have to justify its cost against.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from directory_pipeline.domain.models import RawListing  # noqa: E402
from directory_pipeline.extraction.agent import (  # noqa: E402
    DomExtractor,
    TextFallbackExtractor,
)
from directory_pipeline.fixtures.data import COMPANIES  # noqa: E402
from directory_pipeline.fixtures.mock_directory import (  # noqa: E402
    _drifted_page,
    _microdata_page,
    _stub_page,
)

TEMPLATES = {
    "microdata": _microdata_page,
    "drifted": _drifted_page,
    "stub": _stub_page,
}

# extractor key -> fixture attribute
FIELDS = {
    "name": "name",
    "city": "city",
    "region": "region",
    "postal_code": "postal",
    "phone": "phone",
    "email": "email",
    "website": "website",
    "description": "description",
    "employee_count": "employees",
    "founded_year": "founded",
}


def norm(key: str, value: object) -> str:
    """Compare on meaning, not formatting."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if key == "phone":
        return re.sub(r"\D", "", s)[-10:]
    if key in ("website", "email"):
        s = re.sub(r"^https?://", "", s.lower()).removeprefix("www.").rstrip("/")
        return s
    if key in ("employee_count", "founded_year"):
        digits = re.sub(r"[^\d]", "", s)
        return digits.lstrip("0") or digits
    return " ".join(s.lower().split())


@dataclass
class Tally:
    recovered: int = 0  # emitted and correct
    wrong: int = 0  # emitted and incorrect
    missed: int = 0  # on the page, but not emitted
    absent: int = 0  # the fixture itself has no value
    unrecoverable: int = 0  # the fact exists but the page never prints it

    @property
    def truth(self) -> int:
        """Only facts a reader could find on the page count against recall."""
        return self.recovered + self.wrong + self.missed

    @property
    def emitted(self) -> int:
        return self.recovered + self.wrong

    def recall(self) -> float | None:
        return self.recovered / self.truth if self.truth else None

    def precision(self) -> float | None:
        return self.recovered / self.emitted if self.emitted else None


@dataclass
class LayerResult:
    name: str
    by_field: dict[str, Tally] = dc_field(default_factory=dict)
    by_template: dict[str, Tally] = dc_field(default_factory=dict)

    def tally(self, fieldname: str, template: str) -> list[Tally]:
        return [
            self.by_field.setdefault(fieldname, Tally()),
            self.by_template.setdefault(template, Tally()),
        ]


def score(layer: LayerResult, extracted: dict, company, template: str, html: str) -> None:
    for key, attr in FIELDS.items():
        expected = norm(key, getattr(company, attr, None))
        actual = norm(key, extracted.get(key))
        tallies = layer.tally(key, template)
        # A fact the page never prints is not an extraction failure. An LLM
        # could not recover it either, so it must not count against recall.
        # Match on a distinctive prefix, not the first word: "Freight" appears
        # on half the pages, but the first 30 characters of a description do not.
        probe = expected[:30]
        on_page = bool(expected) and probe in norm(key, html)
        for t in tallies:
            if not expected:
                t.absent += 1
            elif not on_page and not actual:
                t.unrecoverable += 1
            elif not actual:
                t.missed += 1
            elif actual == expected or expected in actual or actual in expected:
                t.recovered += 1
            else:
                t.wrong += 1


def pct(v: float | None) -> str:
    return "  --  " if v is None else f"{v * 100:5.1f}%"


def table(title: str, rows: dict[str, Tally]) -> None:
    print(f"\n{title}")
    print(
        f"  {'':<16} {'recall':>8} {'precision':>10} {'found':>7} {'missed':>7} "
        f"{'wrong':>6} {'n/a':>5}"
    )
    for name, t in rows.items():
        print(
            f"  {name:<16} {pct(t.recall()):>8} {pct(t.precision()):>10} "
            f"{t.recovered:>7} {t.missed:>7} {t.wrong:>6} {t.unrecoverable:>5}"
        )


def main() -> int:
    dom_only = LayerResult("DOM selectors")
    cascade = LayerResult("DOM + text fallback")
    dom, text = DomExtractor(), TextFallbackExtractor()

    pages = 0
    text_filled = 0
    for company in COMPANIES:
        for tname, render in TEMPLATES.items():
            pages += 1
            listing = RawListing(
                source="eval",
                source_id=company.slug,
                url=f"http://directory.test/company/{company.slug}",
                html=render(company),
            )

            raw = dom.extract(listing)
            score(dom_only, raw, company, tname, listing.html)

            # Layer 2 fills only what layer 1 missed -- DOM values always win.
            merged = dict(raw)
            from_text = text.extract(listing)
            for k, v in (from_text or {}).items():
                if v and not merged.get(k):
                    merged[k] = v
                    text_filled += 1
            score(cascade, merged, company, tname, listing.html)

    print(
        f"Extraction accuracy over {pages} pages "
        f"({len(COMPANIES)} companies x {len(TEMPLATES)} templates)"
    )
    print("Ground truth: the fixture records the pages are rendered from.")

    for layer in (dom_only, cascade):
        print(f"\n{'=' * 64}\n{layer.name}\n{'=' * 64}")
        table("by field", layer.by_field)
        table("by template", layer.by_template)

    d = sum(t.recovered for t in dom_only.by_field.values())
    c = sum(t.recovered for t in cascade.by_field.values())
    w = sum(t.wrong for t in cascade.by_field.values())
    m = sum(t.missed for t in cascade.by_field.values())
    print(f"\n{'=' * 64}\nMarginal value of layer 2\n{'=' * 64}")
    print(f"  DOM alone recovered          {d} fields")
    print(f"  text fallback added          {c - d} fields ({text_filled} writes)")
    u = sum(t.unrecoverable for t in cascade.by_field.values())
    print(f"  still missing after layer 2  {m}  <- what layer 3 must justify")
    print(f"  not printed on the page      {u}  (no layer can recover these)")
    print(f"  incorrect values emitted     {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
