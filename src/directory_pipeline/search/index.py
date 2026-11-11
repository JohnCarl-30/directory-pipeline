"""OpenSearch index lifecycle: mappings, analyzers, and zero-downtime reindex.

The whole design rests on one rule: **applications never name an index.** They
talk to an alias. `companies` points at `companies-v3-20260912`, and a reindex
swaps the alias to `companies-v4-...` in a single atomic action. No writes are
lost, no request sees a half-built index, and rollback is the same swap in
reverse.

Mappings are explicit and `dynamic: strict`. Dynamic mapping is how a stray
field from an upstream API silently maps `revenue` as a `text` field and breaks
every range query in production three weeks later. Strict mode makes that a
loud 400 at ingest time instead.

The analyzer chain is the other half of relevance. Company names need three
different treatments of the same string, so `name` is indexed three ways:

    name            -> full-text, analyzed  (matches "acme systems" in prose)
    name.keyword    -> exact, unanalyzed    (aggregations, sorting, dedupe)
    name.ngram      -> edge n-grams         (type-ahead: "acm" -> "Acme")
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from opensearchpy import AsyncOpenSearch, NotFoundError, RequestError
from opensearchpy.helpers import async_bulk, async_scan

from ..config import Settings
from ..domain.models import SCHEMA_VERSION, Address, CompanyRecord, Contact, EnrichedCompany
from ..observability import METRICS, get_logger, timed
from ..resolution.entity import IndexedRecord

log = get_logger(__name__)

INGEST_PIPELINE_ID = "companies-normalize"

# mget ids per request. Keeps one backfill from building a megabyte of JSON.
_MGET_CHUNK = 1_000

# What index-wide resolution reads back: the fields scoring and canonical
# selection use, plus the verdict already stored and when the document was
# last written.
_RESOLUTION_SOURCE = [
    "record_id",
    "source",
    "source_id",
    "source_url",
    "content_hash",
    "name",
    "name_normalized",
    "legal_name",
    "categories",
    "description",
    "address",
    "contact",
    "employee_count",
    "founded_year",
    "cluster_id",
    "duplicate_of",
    "indexed_at",
]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _record_from_document(doc: dict[str, Any]) -> CompanyRecord:
    """Rebuild the parts of a CompanyRecord that resolution reads."""
    address = doc.get("address") or {}
    contact = doc.get("contact") or {}
    return CompanyRecord(
        record_id=doc["record_id"],
        source=doc.get("source") or "",
        source_id=doc.get("source_id") or "",
        source_url=doc.get("source_url") or "",
        content_hash=doc.get("content_hash") or "",
        name=doc.get("name") or "",
        name_normalized=doc.get("name_normalized") or "",
        legal_name=doc.get("legal_name"),
        categories=doc.get("categories") or [],
        description=doc.get("description"),
        address=Address(
            line1=address.get("line1"),
            city=address.get("city"),
            region=address.get("region"),
            postal_code=address.get("postal_code"),
            country=address.get("country") or "US",
        ),
        contact=Contact(
            phone_e164=contact.get("phone_e164"),
            email=contact.get("email"),
            website=contact.get("website"),
        ),
        employee_count=doc.get("employee_count"),
        founded_year=doc.get("founded_year"),
    )


# Custom analyzers. `company_name_analyzer` strips the legal suffixes that make
# "Acme Inc" and "Acme LLC" look different to BM25 -- the same normalization the
# Python layer does, applied at query time so user input gets it too.
ANALYSIS: dict[str, Any] = {
    "filter": {
        "company_suffixes": {
            "type": "stop",
            "stopwords": [
                "inc",
                "incorporated",
                "llc",
                "ltd",
                "limited",
                "corp",
                "corporation",
                "co",
                "company",
                "plc",
                "gmbh",
                "holdings",
                "group",
            ],
        },
        "english_stemmer": {"type": "stemmer", "language": "light_english"},
        "edge_ngrams": {"type": "edge_ngram", "min_gram": 2, "max_gram": 15},
    },
    "analyzer": {
        "company_name_analyzer": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding", "company_suffixes"],
        },
        "company_name_ngram": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding", "company_suffixes", "edge_ngrams"],
        },
        # Search-time twin of the n-gram analyzer: the QUERY must not be
        # n-grammed, or "acme" would match every document containing "a".
        "company_name_ngram_search": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding", "company_suffixes"],
        },
        "description_analyzer": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding", "english_stemmer"],
        },
    },
}

MAPPINGS: dict[str, Any] = {
    "dynamic": "strict",
    "properties": {
        "record_id": {"type": "keyword"},
        "schema_version": {"type": "integer"},
        "source": {"type": "keyword"},
        "source_id": {"type": "keyword"},
        "source_url": {"type": "keyword", "index": False},
        # Hash of the page body this record came from, read back on the
        # next crawl to skip pages that have not changed. `index: False`
        # because it is fetched by document id and never searched or
        # aggregated -- the only access path is mget.
        "content_hash": {"type": "keyword", "index": False},
        "name": {
            "type": "text",
            "analyzer": "company_name_analyzer",
            "fields": {
                "keyword": {"type": "keyword", "ignore_above": 256},
                "ngram": {
                    "type": "text",
                    "analyzer": "company_name_ngram",
                    "search_analyzer": "company_name_ngram_search",
                },
            },
        },
        "name_normalized": {"type": "keyword"},
        "legal_name": {"type": "text", "analyzer": "company_name_analyzer"},
        "categories": {
            "type": "text",
            "analyzer": "description_analyzer",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 128}},
        },
        "description": {"type": "text", "analyzer": "description_analyzer"},
        "address": {
            "properties": {
                "line1": {"type": "text", "index": False},
                "city": {"type": "keyword", "fields": {"text": {"type": "text"}}},
                "region": {"type": "keyword"},
                "postal_code": {"type": "keyword"},
                "country": {"type": "keyword"},
                "full": {"type": "text"},
            }
        },
        "contact": {
            "properties": {
                "phone_e164": {"type": "keyword"},
                "email": {"type": "keyword"},
                "website": {"type": "keyword"},
            }
        },
        "employee_count": {"type": "integer"},
        "founded_year": {"type": "short"},
        "cluster_id": {"type": "keyword"},
        "duplicate_of": {"type": "keyword"},
        "is_canonical": {"type": "boolean"},
        "extraction": {
            "properties": {
                "method": {"type": "keyword"},
                "confidence": {"type": "float"},
            }
        },
        "enrichment": {
            "properties": {
                "provider": {"type": "keyword"},
                "industry": {"type": "keyword", "fields": {"text": {"type": "text"}}},
                "naics": {"type": "keyword"},
                "revenue_usd": {"type": "long"},
                "linkedin_url": {"type": "keyword", "index": False},
                "technologies": {"type": "keyword"},
                "confidence": {"type": "float"},
                "fetched_at": {"type": "date"},
            }
        },
        "completeness": {"type": "float"},
        "first_seen_at": {"type": "date"},
        "last_seen_at": {"type": "date"},
        # Written by the ingest pipeline, never by the client. It still needs a
        # mapping entry -- `dynamic: strict` rejects the document otherwise.
        "indexed_at": {"type": "date"},
    },
}

SETTINGS_BODY: dict[str, Any] = {
    "index": {
        "number_of_shards": 1,  # single-node demo; size by data, not by habit
        "number_of_replicas": 0,
        "refresh_interval": "1s",
        "analysis": ANALYSIS,
    }
}

# Server-side normalization that must hold no matter which client writes.
# Belt-and-braces with the Python normalizer: a backfill script or a colleague's
# one-off reindex goes through this too.
INGEST_PIPELINE: dict[str, Any] = {
    "description": "Normalize company documents on ingest",
    "processors": [
        {"trim": {"field": "name", "ignore_missing": True}},
        {"lowercase": {"field": "contact.email", "ignore_missing": True}},
        {"lowercase": {"field": "contact.website", "ignore_missing": True}},
        {"uppercase": {"field": "address.country", "ignore_missing": True}},
        {
            # Server-side write timestamp. Distinct from `last_seen_at`, which
            # is when the crawler saw the page: comparing the two tells you
            # whether a document is stale because the crawl is behind or
            # because indexing is.
            "set": {
                "field": "indexed_at",
                "value": "{{{_ingest.timestamp}}}",
                "ignore_failure": True,
            }
        },
    ],
}


def physical_index_name(alias: str, version: int = SCHEMA_VERSION) -> str:
    """A name that is readable *and* guaranteed unique.

    The timestamp alone is not enough: it has second granularity, so a
    bootstrap followed immediately by a reindex -- or two reindexes in the same
    second -- produce the same name. `create_index` treats an existing index as
    success (it has to, for the bootstrap to be idempotent), so the collision is
    silent and the reindex then tries to read and write the same index. The
    random suffix removes the collision entirely.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{alias}-v{version}-{stamp}-{uuid.uuid4().hex[:6]}"


class SearchIndex:
    """Alias-fronted index operations."""

    def __init__(self, settings: Settings, client: AsyncOpenSearch | None = None) -> None:
        self.settings = settings
        self.alias = settings.opensearch_alias
        self.client = client or AsyncOpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=settings.opensearch_auth,
            verify_certs=False,
            ssl_show_warn=False,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )

    async def aclose(self) -> None:
        await self.client.close()

    async def ping(self) -> bool:
        """Reachability probe. Failure is an expected answer, not an incident.

        The client logs connection failures at warning level with a full
        traceback, which is noise when we are deliberately asking "are you
        there?", so it is muted for the duration of the check.
        """
        logger = logging.getLogger("opensearch")
        previous = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            return bool(await self.client.ping())
        except Exception:
            return False
        finally:
            logger.setLevel(previous)

    async def ensure_pipeline(self) -> None:
        await self.client.ingest.put_pipeline(id=INGEST_PIPELINE_ID, body=INGEST_PIPELINE)

    async def create_index(self, name: str) -> None:
        body = {"settings": SETTINGS_BODY, "mappings": MAPPINGS}
        try:
            await self.client.indices.create(index=name, body=body)
            log.info("index.created", index=name)
        except RequestError as exc:
            if getattr(exc, "error", "") != "resource_already_exists_exception":
                raise

    async def resolve_alias(self, alias: str | None = None) -> str | None:
        """Which physical index is the alias currently pointing at?

        `None` for "no such alias" is an expected answer on a first bootstrap,
        not an incident, so the client's 404 logging is muted for the lookup.
        """
        alias = alias or self.alias
        logger = logging.getLogger("opensearch")
        previous = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            response = await self.client.indices.get_alias(name=alias)
        except NotFoundError:
            return None
        finally:
            logger.setLevel(previous)
        return next(iter(response), None)

    async def bootstrap(self, alias: str | None = None) -> str:
        """Create the first index and point the alias at it. Idempotent."""
        alias = alias or self.alias
        await self.ensure_pipeline()
        if existing := await self.resolve_alias(alias):
            return existing
        name = physical_index_name(alias)
        await self.create_index(name)
        await self.client.indices.update_aliases(
            body={"actions": [{"add": {"index": name, "alias": alias, "is_write_index": True}}]}
        )
        log.info("index.bootstrapped", index=name, alias=alias)
        return name

    async def index_documents(
        self, companies: list[EnrichedCompany], *, alias: str | None = None
    ) -> int:
        """Bulk-index through the alias.

        `_id` is the deterministic record_id, so re-running the pipeline
        updates in place. Without that, a re-crawl doubles the corpus.
        """
        target = alias or self.alias
        if not companies:
            return 0

        # An `index` op replaces the whole document, so a re-crawled page
        # would otherwise be "first seen" again every time it changed.
        first_seen = await self._existing_first_seen(
            [company.company.record_id for company in companies], target
        )

        actions = []
        for company in companies:
            document = company.to_document()
            if earlier := first_seen.get(company.company.record_id):
                document["first_seen_at"] = earlier
            actions.append(
                {
                    "_op_type": "index",
                    "_index": target,
                    "_id": company.company.record_id,
                    "pipeline": INGEST_PIPELINE_ID,
                    "_source": document,
                }
            )

        with timed("index.bulk"):
            succeeded, raw_errors = await async_bulk(
                self.client, actions, raise_on_error=False, stats_only=False
            )

        # `async_bulk` returns a count or a list for its second element
        # depending on `stats_only`, and types it as the union of both. We pass
        # False, so it is the list -- the per-document errors we log below.
        errors = cast("list[Any]", raw_errors)
        if errors:
            METRICS.incr("index.bulk_errors", len(errors))
            for error in list(errors)[:5]:
                log.error("index.bulk_error", detail=error)
        METRICS.incr("index.documents", succeeded)
        return int(succeeded)

    async def _existing_first_seen(self, record_ids: list[str], target: str) -> dict[str, str]:
        """`record_id -> first_seen_at` for documents already in the index."""
        found: dict[str, str] = {}
        for start in range(0, len(record_ids), _MGET_CHUNK):
            chunk = record_ids[start : start + _MGET_CHUNK]
            try:
                response = await self.client.mget(
                    body={"ids": chunk}, index=target, _source=["first_seen_at"]
                )
            except NotFoundError:
                return {}  # no index yet: everything is new
            for doc in response.get("docs", []):
                if doc.get("found") and (seen := (doc.get("_source") or {}).get("first_seen_at")):
                    found[doc["_id"]] = seen
        return found

    async def content_hashes(
        self, source: str, source_ids: list[str], *, alias: str | None = None
    ) -> dict[str, str]:
        """`source_id -> content_hash` for records already indexed.

        This is what makes a re-crawl incremental: a page whose body hash is
        unchanged is dropped before extraction, which is where the model cost
        lives.

        Fetched by document id with `mget`, not by query. `record_id` is
        `sha1(source:source_id)`, so there is exactly one document per pair and
        the id is computable without asking -- no search, no scoring, no
        dependence on the `source_id` field being indexed or on the refresh
        interval having caught up.

        Missing documents and documents with an empty hash are both simply
        absent from the result, so a first crawl and a crawl after a schema
        change behave the same way: nothing matches, everything is fetched.
        """
        if not source_ids:
            return {}

        target = alias or self.alias
        by_record_id = {
            CompanyRecord.make_record_id(source, source_id): source_id for source_id in source_ids
        }

        hashes: dict[str, str] = {}
        ids = list(by_record_id)
        # Chunked because mget bodies are unbounded by the caller: a batch is
        # 25 URLs today, and nothing stops a backfill passing 50,000.
        for start in range(0, len(ids), _MGET_CHUNK):
            chunk = ids[start : start + _MGET_CHUNK]
            try:
                response = await self.client.mget(
                    body={"ids": chunk},
                    index=target,
                    _source=["content_hash"],
                )
            except NotFoundError:
                # No alias yet -- a first run. Not an error, just no history.
                return {}

            for doc in response.get("docs", []):
                if not doc.get("found"):
                    continue
                digest = (doc.get("_source") or {}).get("content_hash")
                if digest:
                    hashes[by_record_id[doc["_id"]]] = digest

        METRICS.incr("index.content_hashes_loaded", len(hashes))
        return hashes

    async def load_for_resolution(
        self,
        *,
        alias: str | None = None,
        since: datetime | None = None,
        on_page: Callable[[int], None] | None = None,
    ) -> list[IndexedRecord]:
        """Every document behind the alias, as resolution input.

        `since` marks which documents this crawl wrote: `indexed_at` (set by
        the ingest pipeline) at or after it means touched. A document with no
        readable `indexed_at`, or a call with no `since`, counts as touched --
        the cost of being wrong that way is one extra adjudication, the other
        way a stale verdict.
        """
        target = alias or self.alias
        rows: list[IndexedRecord] = []
        try:
            async for hit in async_scan(
                self.client,
                index=target,
                query={"query": {"match_all": {}}, "_source": _RESOLUTION_SOURCE},
                size=1_000,
            ):
                doc = hit.get("_source") or {}
                try:
                    record = _record_from_document(doc)
                except (KeyError, ValueError) as exc:
                    log.warning("resolve.unreadable_document", id=hit.get("_id"), error=str(exc))
                    continue
                indexed_at = _parse_timestamp(doc.get("indexed_at"))
                touched = since is None or indexed_at is None or indexed_at >= since
                rows.append(
                    IndexedRecord(
                        record=record,
                        cluster_id=doc.get("cluster_id"),
                        duplicate_of=doc.get("duplicate_of"),
                        touched=touched,
                    )
                )
                if on_page and len(rows) % 1_000 == 0:
                    on_page(len(rows))
        except NotFoundError:
            return []
        return rows

    async def apply_clusters(
        self, updates: dict[str, tuple[str, str | None]], *, alias: str | None = None
    ) -> int:
        """Write new cluster verdicts as partial updates.

        Partial, not a re-index: only the three verdict fields change, and the
        rest of the document -- `first_seen_at`, `indexed_at`, enrichment -- is
        left exactly as the crawl wrote it.
        """
        if not updates:
            return 0
        target = alias or self.alias
        actions = [
            {
                "_op_type": "update",
                "_index": target,
                "_id": record_id,
                "doc": {
                    "cluster_id": cluster_id,
                    "duplicate_of": duplicate_of,
                    "is_canonical": duplicate_of is None,
                },
            }
            for record_id, (cluster_id, duplicate_of) in updates.items()
        ]
        with timed("index.cluster_updates"):
            succeeded, raw_errors = await async_bulk(
                self.client, actions, raise_on_error=False, stats_only=False
            )
        errors = cast("list[Any]", raw_errors)
        if errors:
            METRICS.incr("index.cluster_update_errors", len(errors))
            for error in list(errors)[:5]:
                log.error("index.cluster_update_error", detail=error)
        METRICS.incr("index.cluster_updates", succeeded)
        return int(succeeded)

    async def _reindex_via_task(
        self,
        body: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        poll_interval_s: float = 2.0,
        resume_task_id: str | None = None,
        progress_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit the copy as a task and poll it to completion.

        `resume_task_id` reattaches to a copy already running on the cluster
        instead of submitting another one, which is what makes a retry of this
        safe: the task outlives the process that submitted it.
        """
        if resume_task_id:
            task_id: str | None = resume_task_id
            log.info("reindex.task_resumed", task_id=task_id)
        else:
            submitted = await self.client.reindex(body=body, wait_for_completion=False)
            task_id = submitted.get("task")
            if not task_id:
                raise RuntimeError("reindex did not return a task id")
            log.info("reindex.task_submitted", task_id=task_id)

        while True:
            status = await self.client.tasks.get(task_id=task_id)
            if status.get("completed"):
                break
            if on_progress:
                # Gives the caller somewhere to heartbeat from: a copy that
                # outlives the activity heartbeat timeout would otherwise be
                # declared dead and restarted from scratch.
                # The task id rides along so the caller can record it and
                # reattach after a restart rather than starting a second copy.
                # Every report carries the task id and target index, because
                # only the most recent heartbeat's details survive a restart.
                on_progress(
                    {
                        **status.get("task", {}).get("status", {}),
                        **(progress_extra or {}),
                        "task_id": task_id,
                    }
                )
            await asyncio.sleep(poll_interval_s)

        if error := status.get("error"):
            raise RuntimeError(f"reindex task failed: {error}")
        response: dict[str, Any] = status.get("response", {})
        return response

    async def reindex(
        self,
        *,
        alias: str | None = None,
        wait_for_completion: bool = False,
        drop_old_index: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        resume: dict[str, Any] | None = None,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Zero-downtime reindex behind the alias.

        Sequence:
          1. create the new index with current mappings
          2. copy documents (source index keeps serving reads the whole time)
          3. refresh so the new index is searchable *before* the swap
          4. atomically move the alias in a single update_aliases call
          5. optionally drop the old index

        Step 4 is why this is zero-downtime: `update_aliases` applies its
        actions atomically, so no request ever observes zero or two indices
        behind the alias.

        `wait_for_completion` selects how the copy is driven, not whether this
        call returns early -- it always returns after the swap. True issues one
        long HTTP request; False (the default) submits a task and polls it,
        which is what survives a long copy and gives the caller a heartbeat
        point.

        Caveat worth naming: writes that land on the source index after the
        copy begins are not carried over. In production you either pause the
        writer for the swap, or dual-write to both indices during the copy.
        """
        alias = alias or self.alias
        started = time.perf_counter()

        source = await self.resolve_alias(alias)

        # A retry must not mint a new target: physical_index_name() is unique by
        # construction (timestamp plus random suffix), so recomputing it would
        # create a second index and submit a second copy while the first is
        # still running -- two full copies against the cluster, and an orphan
        # index nobody swaps to. Reusing the recorded target and task makes the
        # retry continue the original copy instead.
        resume_task_id = (resume or {}).get("task_id")
        resume_target = (resume or {}).get("target_index")
        target = resume_target or physical_index_name(alias)

        if resume_target and source == resume_target:
            # The previous attempt got as far as the swap and died before it
            # could report back. The work is done; finish the bookkeeping
            # rather than refusing to "reindex an index into itself" on every
            # retry and failing a workflow whose reindex succeeded.
            return await self._finish_swapped(
                alias=alias,
                target=target,
                previous=(resume or {}).get("source_index"),
                task_id=resume_task_id,
                drop_old_index=drop_old_index,
                started=started,
            )

        if target == source:
            # Unreachable with a unique suffix, but the failure mode this
            # guards against is subtle enough to be worth an explicit error
            # rather than an opaque one from the server.
            raise RuntimeError(f"reindex source and target are the same index: {source}")

        await self.ensure_pipeline()
        if resume_target:
            log.info("reindex.resuming", target=target, task_id=resume_task_id)
        else:
            await self.create_index(target)

        copied = 0
        if source:
            log.info(
                "reindex.copy_start",
                source=source,
                target=target,
                blocking=wait_for_completion,
            )
            body = {
                "source": {"index": source},
                "dest": {"index": target, "pipeline": INGEST_PIPELINE_ID},
            }
            if wait_for_completion:
                # One long HTTP request. Fine for a small index, but the
                # connection has to survive the entire copy.
                response = await self.client.reindex(
                    body=body, wait_for_completion=True, refresh=True
                )
            else:
                # Submit-and-poll. Nothing holds a socket open, so this is the
                # form that survives a multi-hour copy.
                response = await self._reindex_via_task(
                    body,
                    on_progress=on_progress,
                    resume_task_id=resume_task_id,
                    # The source rides along too: if this attempt dies after
                    # the swap, the retry needs it to drop the old index.
                    progress_extra={"target_index": target, "source_index": source},
                    poll_interval_s=poll_interval_s,
                )
            copied = int(response.get("created", 0)) + int(response.get("updated", 0))
            failures = response.get("failures") or []
            if failures:
                log.error("reindex.failures", count=len(failures), sample=failures[:3])
                raise RuntimeError(f"reindex had {len(failures)} document failures")

        await self.client.indices.refresh(index=target)

        actions: list[dict[str, Any]] = [
            {"add": {"index": target, "alias": alias, "is_write_index": True}}
        ]
        if source:
            actions.append({"remove": {"index": source, "alias": alias}})
        await self.client.indices.update_aliases(body={"actions": actions})
        log.info("reindex.alias_swapped", alias=alias, from_index=source, to_index=target)

        dropped = False
        if drop_old_index and source:
            # Off by default: the old index is the rollback path. Keep it until
            # the new one has served real traffic.
            await self.client.indices.delete(index=source)
            dropped = True
            log.warning("reindex.old_index_dropped", index=source)

        return {
            "alias": alias,
            "source_index": source,
            "target_index": target,
            "documents_copied": copied,
            "swapped": True,
            "old_index_dropped": dropped,
            "duration_s": round(time.perf_counter() - started, 3),
        }

    async def _finish_swapped(
        self,
        *,
        alias: str,
        target: str,
        previous: str | None,
        task_id: str | None,
        drop_old_index: bool,
        started: float,
    ) -> dict[str, Any]:
        """Report a reindex whose swap a previous attempt already made."""
        log.info("reindex.already_swapped", alias=alias, target=target, previous=previous)
        copied = 0
        if task_id:
            status = await self.client.tasks.get(task_id=task_id)
            response = status.get("response") or {}
            copied = int(response.get("created", 0)) + int(response.get("updated", 0))

        dropped = False
        if drop_old_index and previous and previous != target:
            # The previous attempt may have dropped it already.
            await self.client.indices.delete(index=previous, ignore_unavailable=True)
            dropped = True
            log.warning("reindex.old_index_dropped", index=previous)

        return {
            "alias": alias,
            "source_index": previous,
            "target_index": target,
            "documents_copied": copied,
            "swapped": True,
            "old_index_dropped": dropped,
            "duration_s": round(time.perf_counter() - started, 3),
        }

    async def rollback(self, alias: str, to_index: str) -> None:
        """Point the alias back at a previous index. The reason we keep them."""
        current = await self.resolve_alias(alias)
        actions: list[dict[str, Any]] = [
            {"add": {"index": to_index, "alias": alias, "is_write_index": True}}
        ]
        if current:
            actions.append({"remove": {"index": current, "alias": alias}})
        await self.client.indices.update_aliases(body={"actions": actions})
        log.warning("index.rolled_back", alias=alias, from_index=current, to_index=to_index)

    async def snapshot(self, repository: str, snapshot_name: str, alias: str | None = None) -> None:
        """Snapshot the alias's current index. Cheap insurance before a migration."""
        index = await self.resolve_alias(alias or self.alias)
        if not index:
            raise RuntimeError(f"alias {alias or self.alias} does not exist")
        await self.client.snapshot.create(
            repository=repository,
            snapshot=snapshot_name,
            body={"indices": index, "include_global_state": False},
            params={"wait_for_completion": "true"},
        )
        log.info("index.snapshot_created", repository=repository, snapshot=snapshot_name)

    async def stats(self, alias: str | None = None) -> dict[str, Any]:
        alias = alias or self.alias
        index = await self.resolve_alias(alias)
        if not index:
            return {"alias": alias, "index": None, "documents": 0}
        count = await self.client.count(index=alias)
        return {"alias": alias, "index": index, "documents": count.get("count", 0)}
