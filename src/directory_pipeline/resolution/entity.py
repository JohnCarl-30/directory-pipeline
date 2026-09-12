"""Entity resolution: blocking, then scoring, then (optionally) adjudication.

Comparing every record to every other record is O(n^2) -- 100k companies is 5
billion comparisons. **Blocking** fixes that: records only get compared if they
share a cheap key (a domain, a normalized-name prefix, a phone number). Recall
comes from using *several* independent blocking keys, so a record missing one
signal can still be caught by another.

Scoring is a weighted sum of field agreements, not a single similarity number.
Fields differ enormously in how much a match tells you: two companies sharing a
domain are almost certainly the same company; two sharing a city are not.

The output is clusters, with one canonical record each. Non-canonical records
are kept and indexed with `duplicate_of` set -- deleting a duplicate loses the
provenance that proves the merge was right.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from ..domain.models import CompanyRecord
from ..extraction.normalize import domain_of
from ..observability import get_logger

log = get_logger(__name__)

# Weights are log-odds-ish: how much does agreement on this field move belief?
# Domain is near-decisive; city alone is nearly worthless.
FIELD_WEIGHTS: dict[str, float] = {
    "domain": 0.55,
    # A shared E.164 number is strong evidence: it is a contact channel the
    # business controls, and two unrelated companies almost never share one.
    # Calibrated so exact-name + exact-phone clears the threshold on its own,
    # which is the common "same company, no website listed" case.
    "phone": 0.35,
    "email": 0.20,
    "name": 0.30,
    "postal_code": 0.10,
    "city_region": 0.08,
    # Negative signal. A shared corporate domain across two clearly different
    # localities is the signature of a branch or franchise, not a duplicate --
    # every location of a chain lists the same website. Without this, domain
    # agreement alone merges an entire multi-site company into one record.
    # Weighted to pull such a pair down into the review band rather than to
    # reject it outright: it is genuinely ambiguous, so it belongs in front of
    # the adjudicator.
    "location_conflict": -0.38,
}

MATCH_THRESHOLD = 0.62  # at or above -> same entity
REVIEW_THRESHOLD = 0.45  # between -> borderline, worth adjudicating


@dataclass
class Candidate:
    left: CompanyRecord
    right: CompanyRecord
    score: float
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def is_match(self) -> bool:
        return self.score >= MATCH_THRESHOLD

    @property
    def needs_review(self) -> bool:
        return REVIEW_THRESHOLD <= self.score < MATCH_THRESHOLD


def blocking_keys(record: CompanyRecord) -> set[str]:
    """Cheap keys that co-locate plausible duplicates.

    Multiple independent keys per record: a company with a typo'd name can
    still block on its phone number, and vice versa.
    """
    keys: set[str] = set()
    if dom := domain_of(record.contact.website):
        keys.add(f"dom:{dom}")
    if phone := record.contact.phone_e164:
        keys.add(f"tel:{phone}")
    if email := record.contact.email:
        keys.add(f"eml:{email}")
    name = record.name_normalized
    if name:
        # First token + locality: "acme" in Austin blocks with "acme" in Austin,
        # not with "acme" in Boston.
        first = name.split(" ")[0]
        if len(first) >= 3:
            locality = (record.address.city or "").lower().replace(" ", "")
            keys.add(f"nm:{first}:{locality}")
        # Sorted-token fingerprint catches word-order differences.
        keys.add(f"fp:{''.join(sorted(name.replace(' ', '')))[:24]}")
    return keys


def jaro_winkler(a: str, b: str) -> float:
    """Similarity tuned for names: rewards a shared prefix.

    Company names differ at the end far more than the start ("Acme Systems"
    vs "Acme Systems Group"), which is exactly what the Winkler prefix bonus
    is for.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    window = max(len(a), len(b)) // 2 - 1
    window = max(window, 0)
    a_flags = [False] * len(a)
    b_flags = [False] * len(b)
    matches = 0

    for i, ch in enumerate(a):
        start = max(0, i - window)
        end = min(i + window + 1, len(b))
        for j in range(start, end):
            if not b_flags[j] and b[j] == ch:
                a_flags[i] = b_flags[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i, flagged in enumerate(a_flags):
        if not flagged:
            continue
        while not b_flags[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    m = float(matches)
    jaro = (m / len(a) + m / len(b) + (m - transpositions) / m) / 3.0

    prefix = 0
    for x, y in zip(a, b, strict=False):
        if x != y or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _fold(value: str | None) -> str:
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value.lower())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


def score_pair(left: CompanyRecord, right: CompanyRecord) -> Candidate:
    """Weighted agreement across independent signals."""
    signals: dict[str, float] = {}

    left_dom, right_dom = domain_of(left.contact.website), domain_of(right.contact.website)
    if left_dom and right_dom:
        signals["domain"] = (
            1.0 if left_dom == right_dom else -0.6
        )  # disagreement is evidence *against*

    if left.contact.phone_e164 and right.contact.phone_e164:
        signals["phone"] = 1.0 if left.contact.phone_e164 == right.contact.phone_e164 else 0.0

    if left.contact.email and right.contact.email:
        signals["email"] = 1.0 if left.contact.email == right.contact.email else 0.0

    if left.name_normalized and right.name_normalized:
        similarity = jaro_winkler(left.name_normalized, right.name_normalized)
        signals["name"] = similarity if similarity > 0.82 else 0.0

    if left.address.postal_code and right.address.postal_code:
        signals["postal_code"] = (
            1.0 if left.address.postal_code == right.address.postal_code else 0.0
        )

    lc, rc = _fold(left.address.city), _fold(right.address.city)
    same_locality: bool | None = None
    if lc and rc:
        same_region = (left.address.region or "") == (right.address.region or "")
        same_locality = lc == rc and same_region
        signals["city_region"] = 1.0 if same_locality else 0.0

    # Only meaningful when both sides actually asserted a domain and a
    # locality; absence of an address is not evidence of a different one.
    if signals.get("domain") == 1.0 and same_locality is False:
        signals["location_conflict"] = 1.0

    score = sum(FIELD_WEIGHTS[k] * v for k, v in signals.items())
    return Candidate(
        left=left, right=right, score=round(max(0.0, min(score, 1.0)), 4), signals=signals
    )


def generate_candidates(records: list[CompanyRecord]) -> list[Candidate]:
    """Block, then score only within blocks."""
    buckets: dict[str, list[CompanyRecord]] = defaultdict(list)
    for record in records:
        for key in blocking_keys(record):
            buckets[key].append(record)

    compared: set[tuple[str, str]] = set()
    candidates: list[Candidate] = []
    for key, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        if len(bucket) > 200:
            # A bucket this large is a bad key (e.g. every record shares a
            # switchboard number). Comparing it is quadratic and low-value.
            log.warning("resolve.bucket_too_large", key=key, size=len(bucket))
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                pair = tuple(sorted((a.record_id, b.record_id)))
                if pair in compared:
                    continue
                compared.add(pair)
                candidate = score_pair(a, b)
                if candidate.score >= REVIEW_THRESHOLD:
                    candidates.append(candidate)
    return candidates


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def resolve(
    records: list[CompanyRecord], accepted: list[Candidate] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Cluster records.

    Returns (record_id -> cluster_id, record_id -> canonical record_id).
    `accepted` lets an adjudicator promote borderline pairs to matches before
    clustering, without changing the scoring rules.
    """
    by_id = {r.record_id: r for r in records}
    union = UnionFind()
    for record in records:
        union.find(record.record_id)

    matches = [c for c in generate_candidates(records) if c.is_match]
    matches.extend(accepted or [])
    for candidate in matches:
        union.union(candidate.left.record_id, candidate.right.record_id)

    clusters: dict[str, list[str]] = defaultdict(list)
    for record_id in by_id:
        clusters[union.find(record_id)].append(record_id)

    cluster_of: dict[str, str] = {}
    canonical_of: dict[str, str] = {}
    for root, members in clusters.items():
        # Canonical = most complete record; ties broken by id for determinism,
        # so re-running the pipeline picks the same survivor every time.
        canonical = max(members, key=lambda rid: (_completeness(by_id[rid]), rid))
        for member in members:
            cluster_of[member] = root
            canonical_of[member] = canonical
    return cluster_of, canonical_of


def _completeness(record: CompanyRecord) -> int:
    return sum(
        1
        for value in (
            record.contact.website,
            record.contact.phone_e164,
            record.contact.email,
            record.address.city,
            record.address.postal_code,
            record.description,
            record.categories,
            record.employee_count,
        )
        if value
    )
