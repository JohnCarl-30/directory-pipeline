"""LLM adjudication for borderline duplicate pairs.

Probabilistic matching is decisive at the ends of the range and useless in the
middle. "Acme Systems, Austin" vs "Acme Systems Group, Austin" scores 0.5:
maybe a subsidiary, maybe a rebrand, maybe the same company listed twice. A
human would resolve it in two seconds by reading both records.

That middle band is the only place the model is invoked, which keeps cost
proportional to genuine ambiguity rather than to corpus size.

This uses **tool calling** rather than structured outputs, because the decision
is an action with arguments ("merge these, here's why"), and a strict tool
schema makes the arguments type-safe. `strict: true` guarantees `tool_use.input`
validates against the schema exactly -- no defensive key checks downstream.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import Settings
from ..domain.models import CompanyRecord
from ..observability import METRICS, get_logger, timed
from .entity import Candidate

log = get_logger(__name__)

ADJUDICATION_TOOL: dict[str, Any] = {
    "name": "record_decision",
    "description": (
        "Record whether two directory listings refer to the same real-world company. "
        "Call this exactly once, after comparing the two records."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["same_company", "different_companies", "insufficient_evidence"],
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in the verdict.",
            },
            "deciding_evidence": {
                "type": "string",
                "description": "The single field or fact that decided it. One sentence.",
            },
            "relationship": {
                "type": "string",
                "enum": ["identical", "subsidiary", "franchise", "unrelated", "unknown"],
                "description": (
                    "How the two entities relate. A subsidiary or franchise is a "
                    "different company even when names and owners overlap."
                ),
            },
        },
        "required": ["verdict", "confidence", "deciding_evidence", "relationship"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = """\
You decide whether two business-directory listings describe the same company.

Treat these as strong evidence of the SAME company:
- identical web domain, or identical phone number at the same address
- one name is the other plus or minus a legal suffix or punctuation

Treat these as evidence of DIFFERENT companies:
- different web domains (two companies rarely share a domain)
- same brand at different street addresses -- that is a franchise or a branch, \
which is a different company for directory purposes
- a parent and its named subsidiary

Return `insufficient_evidence` when the records are too sparse to tell. That is \
a useful answer; a confident guess is not. Call `record_decision` exactly once.\
"""


def _describe(record: CompanyRecord) -> str:
    fields = [
        f"name: {record.name}",
        f"legal_name: {record.legal_name or '-'}",
        f"website: {record.contact.website or '-'}",
        f"phone: {record.contact.phone_e164 or '-'}",
        f"email: {record.contact.email or '-'}",
        f"address: {record.address.as_text() or '-'}",
        f"categories: {', '.join(record.categories) or '-'}",
        f"employees: {record.employee_count or '-'}",
        f"description: {(record.description or '-')[:300]}",
    ]
    return "\n".join(fields)


class Adjudicator:
    """Resolves borderline pairs. Degrades to 'no opinion' when unavailable."""

    def __init__(self, settings: Settings, *, concurrency: int = 8) -> None:
        self.settings = settings
        self._client: Any = None
        self._semaphore = asyncio.Semaphore(concurrency)

    @property
    def enabled(self) -> bool:
        return self.settings.llm_extraction_enabled

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key or None)
        return self._client

    async def adjudicate(self, candidate: Candidate) -> dict[str, Any] | None:
        client = self._get_client()
        user_content = (
            "Record A:\n"
            f"{_describe(candidate.left)}\n\n"
            "Record B:\n"
            f"{_describe(candidate.right)}\n\n"
            f"Deterministic matcher score: {candidate.score} "
            f"(signals: {candidate.signals})"
        )

        async with self._semaphore:
            with timed("resolve.adjudicate"):
                response = await client.messages.create(
                    model=self.settings.extraction_model,
                    max_tokens=1024,
                    system=[
                        {
                            "type": "text",
                            "text": _SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[ADJUDICATION_TOOL],
                    tool_choice={"type": "tool", "name": "record_decision"},
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": user_content}],
                )

        if response.stop_reason == "refusal":
            METRICS.incr("resolve.adjudicate_refusal")
            return None

        for block in response.content:
            if block.type == "tool_use" and block.name == "record_decision":
                # strict: true -- this input is schema-valid, no key guarding.
                return dict(block.input)
        return None

    async def adjudicate_many(
        self, candidates: list[Candidate], *, min_confidence: float = 0.7
    ) -> list[Candidate]:
        """Return the borderline pairs the model confirms as the same company."""
        if not self.enabled or not candidates:
            if candidates:
                METRICS.incr("resolve.adjudication_skipped", len(candidates))
                log.info("resolve.adjudication_skipped", pairs=len(candidates))
            return []

        results = await asyncio.gather(
            *(self.adjudicate(c) for c in candidates), return_exceptions=True
        )

        accepted: list[Candidate] = []
        for candidate, result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("resolve.adjudicate_failed", error=str(result))
                continue
            if not result:
                continue
            if (
                result["verdict"] == "same_company"
                and result["relationship"] == "identical"
                and float(result["confidence"]) >= min_confidence
            ):
                accepted.append(candidate)
                METRICS.incr("resolve.adjudicated_match")
                log.info(
                    "resolve.adjudicated_match",
                    left=candidate.left.name,
                    right=candidate.right.name,
                    evidence=result["deciding_evidence"],
                )
            else:
                METRICS.incr("resolve.adjudicated_distinct")
        return accepted
