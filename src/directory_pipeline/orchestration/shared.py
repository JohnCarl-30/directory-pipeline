"""Retry policies and timeouts, in one place.

Timeouts are the part people skip, and they are the part that decides whether a
stuck worker blocks a workflow for 10 seconds or forever.

  start_to_close   -- how long ONE attempt may run. Set it from the p99 of the
                      operation, not from hope.
  schedule_to_close -- total budget across all attempts. The real deadline.
  heartbeat        -- how often a long activity must check in. Without it, a
                      worker that dies mid-activity is only noticed at
                      start_to_close; with it, Temporal reschedules in seconds.

Retry policy differs by failure shape, which is why there are several:
scraping fails often and transiently (retry a lot, back off hard); a paid API
call fails expensively (retry less); a bulk index is mostly all-or-nothing.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

TASK_QUEUE = "directory-pipeline"

# Errors where retrying cannot help. Naming them stops Temporal from burning a
# full retry budget on a 404 or a schema violation.
# Matched by Temporal against type(exc).__name__, so every entry must name a
# class that something actually raises -- a stale name fails silently rather
# than loudly. tests/test_workflows.py pins that.
#
# Fetch failures are deliberately absent: the scraping client already knows
# whether a retry can help, and the activities forward that verdict as
# ApplicationError(non_retryable=...). A name here could not express it, which
# is why the "PermanentFetchError" that used to sit in this list matched nothing
# and let terminal 404s retry six times.
NON_RETRYABLE = [
    "ExtractionError",
    "ValidationError",
]

# Scraping: cheap per attempt, frequently transient (429s, blips, proxy churn).
SCRAPE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=6,
    non_retryable_error_types=NON_RETRYABLE,
)

# Third-party APIs: each attempt may cost money. Fewer attempts, longer waits.
API_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=4,
    non_retryable_error_types=NON_RETRYABLE,
)

# Indexing: local, fast, and usually a cluster blip when it fails.
INDEX_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
    non_retryable_error_types=NON_RETRYABLE,
)

# Reindex: a single long operation. Retrying a partial reindex is safe because
# the target index is created fresh each attempt.
REINDEX_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=3,
)

DISCOVER_TIMEOUTS = {
    "start_to_close_timeout": timedelta(minutes=15),
    "schedule_to_close_timeout": timedelta(minutes=45),
    "heartbeat_timeout": timedelta(minutes=2),
}

FETCH_TIMEOUTS = {
    "start_to_close_timeout": timedelta(minutes=10),
    "schedule_to_close_timeout": timedelta(minutes=30),
    "heartbeat_timeout": timedelta(minutes=1),
}

ENRICH_TIMEOUTS = {
    "start_to_close_timeout": timedelta(minutes=10),
    "schedule_to_close_timeout": timedelta(minutes=40),
    "heartbeat_timeout": timedelta(minutes=1),
}

INDEX_TIMEOUTS = {
    "start_to_close_timeout": timedelta(minutes=5),
    "schedule_to_close_timeout": timedelta(minutes=20),
}

REINDEX_TIMEOUTS = {
    "start_to_close_timeout": timedelta(hours=2),
    "schedule_to_close_timeout": timedelta(hours=6),
    "heartbeat_timeout": timedelta(minutes=5),
}
