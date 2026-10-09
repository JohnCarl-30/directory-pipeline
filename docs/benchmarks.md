# Entity resolution at 100k records

`resolution/entity.py` opens with a claim:

> Comparing every record to every other record is O(n^2) -- 100k companies is 5
> billion comparisons. **Blocking** fixes that.

Until this page, that was an assertion. Everything measured in this repo ran on
12 records, where 66 comparisons fit in a loop and brute force would have been
fine. `scripts/bench_resolution.py` runs the real `blocking_keys`,
`score_pair`, `generate_candidates` and `resolve` over a corpus with known
answers, and **it found two bugs** — one of which silently merged every branch
of every chain.

```bash
python scripts/bench_resolution.py                  # 100k, ~55s
python scripts/bench_resolution.py --records 10000  # ~4s
python scripts/bench_resolution.py --markdown       # these tables
```

One seeded RNG, so any number here is reproducible. Measured on an M-series
laptop, single process, CPython 3.13.

---

## The corpus

A benchmark made only of easy positives measures the generator, so there are
four populations and two of them are supposed to stay apart:

| population | what it is | expected |
|---|---|---|
| singletons | one record, no duplicate anywhere | no pair |
| duplicates | 2–3 listings of one company, perturbed the way directories actually differ: legal-suffix swaps, word order, adjacent-character typos, an abbreviated last word, a missing website, a missing phone | **merge** |
| branches | same company, same corporate domain, genuinely different city — a chain | **do not merge**; land in review |
| near misses | different companies, similar names, same city, different domain and phone — "Harbor Point Labs" and "Harbor Point Legal" on one street | **do not merge** |

| | |
|---|---|
| records | 100,002 |
| true entities | 82,411 |
| duplicate pairs seeded | 22,030 |
| branch pairs (must not merge) | 5,237 |
| near-miss pairs (must not merge) | 3,745 |

---

## Blocking

| | |
|---|---|
| brute force n(n-1)/2 | 5,000,150,001 |
| pairs offered by blocking | 3,792,285 |
| comparisons avoided | 99.9242% |
| reduction factor | **1,319x** |
| buckets | 319,397 |
| buckets over cap (200) | 0 |
| pairs discarded by the cap | 0 |
| blocking recall (ceiling) | 99.86% |

The 5 billion is exact: 100,002 records really is 5,000,150,001 pairs, and
blocking scores 3.8 million of them.

**Blocking recall is the number that matters**, and it is the one most easily
left unmeasured. A pair that never shares a key is never scored, so 99.86% is
a ceiling on everything downstream — no scoring change can recover the 30 pairs
blocking never offered. They are variants that lost their website *and* their
phone *and* took a typo in the first token of the name, which removes every key
they had in common.

**The `len(bucket) > 200` guard never fired.** That is a real result and a
limitation of this corpus rather than a clean bill of health: synthetic phone
numbers and domains are uniformly distributed, so nothing here reproduces the
shared switchboard number or the shared `info@` at a hosting provider that the
guard exists for. On a real corpus that guard discards pairs, and the benchmark
reports how many — it just has nothing to report yet.

---

## Scoring

| | |
|---|---|
| pairs at or above review (0.45) | 27,219 |
| pairs at or above match (0.62) | 21,523 |
| duplicate pairs recovered | 21,523 / 22,030 |
| recall | 97.70% |
| precision | **100.00%** |

| | |
|---|---|
| branches wrongly merged | 0 / 5,237 |
| branches held in review | 5,237 |
| near misses wrongly merged | 0 / 3,745 |

Precision was **77.8%** before the first bug below was fixed, and every single
false positive was a branch merge.

---

## Bug 1: a chain with one corporate mailbox merged into one record

`location_conflict` exists exactly to stop this. The README said so:

> Every branch of a chain lists the same corporate website. Without this, domain
> agreement alone merges a whole multi-site company into one record.

The benchmark merged **112 of 112** branch pairs at 2k records, and 5,237 of
5,237 at 100k. The arithmetic:

```
  domain            same      +0.55
  name              same      +0.30
  email             same      +0.20
  location_conflict fires     -0.38
                              -----
                               0.67   >= 0.62, merge
```

The weight was calibrated against the one branch pair in the demo fixtures —
Cascade Freight, Portland and Seattle — and **that pair lists per-branch
emails** (`dispatch@` and `seattle@`). So the email term contributed nothing,
the pair scored 0.85 − 0.38 = 0.47, landed in review, and the case looked
handled. A chain that publishes a single `info@` on every listing, which is at
least as common, added 0.20 of "independent" agreement for a fact the shared
domain had already asserted.

The fix is not a bigger penalty. A mailbox at the shared domain is *the same
fact twice*: `info@chain.com` appears on every branch listing for the same
reason the website does, so it is no longer counted as independent agreement
when a location conflict fires. A shared mailbox at some *other* domain still
counts, because that is something the website did not already say.

Strengthening `location_conflict` to −0.50 would also have cleared the
threshold, and it would have dropped the Cascade pair to 0.35 — below the
review band, so the genuinely ambiguous pair the adjudicator exists for would
have been silently rejected instead of silently merged. Two tests in
`tests/test_entity.py` pin both halves.

---

## Bug 2: the candidate set was scored twice

| stage | before | after |
|---|---|---|
| `generate_candidates` | 55.47s | 52.10s |
| `resolve` | 61.34s | **0.19s** |
| total | 119.46s | **54.39s** |
| peak RSS | 1,133 MB | 969 MB |

`resolve()` called `generate_candidates()` internally. Both callers —
`activities.resolve_duplicates` and `scripts/run_local.py` — already had the
candidate set, because they need the borderline pairs to hand the adjudicator
*before* clustering. So every production path scored 3.8 million pairs, threw
the result away, and scored them again.

At 12 records this is 40ms against 40ms and invisible. At 100k it is half the
stage's wall clock. `resolve` now takes the candidates when the caller has
them, and still computes them when it does not.

---

## Cost

| | |
|---|---|
| generate corpus | 1.53s |
| blocking keys | 0.58s |
| generate_candidates | 52.10s |
| resolve (clusters) | 0.19s |
| **total** | **54.39s** |
| records/sec (blocking) | 172,491 |
| pairs/sec (scoring) | 69,721 |
| peak RSS | 969 MB |

Blocking is not the bottleneck and never was — 0.58s to key 100k records.
Scoring 3.8M pairs is the whole cost, and it is pure-Python `jaro_winkler` at
~70k pairs/sec.

---

## What this does not show

- **Single process, one core.** The pipeline resolves per batch inside an
  activity, so real throughput is this times the worker count. This measures
  the algorithm, not the deployment.
- **Memory grows with candidate pairs, not records.** 969 MB at 3.8M pairs,
  most of it the `compared` set of 40-character record-id pairs. At 1M records
  the pair count grows faster than linearly and this becomes the binding
  constraint before CPU does; `compared` is also per-call, so batching bounds it
  in production in a way this whole-corpus run does not.
- **The perturbations are a guess at how listings differ.** They are drawn from
  the same intuition that wrote the blocking keys, which is the same caveat the
  extraction eval carries: the method transfers, the percentages will not. A
  real corpus has middle initials, trading names, PO boxes, suite numbers in the
  street field and companies that genuinely renamed.
- **Blocking-key sensitivity is real and easy to miss.** An earlier version of
  the generator gave 89% of names a leading qualifier ("North", "Metro"), which
  collapsed the `nm:{first_token}:{locality}` key and cut the reduction factor
  from 914x to 206x at 2k records. That was a generator artifact, not a finding
  — but the fragility it exposed is not: first-token blocking is weak for names
  that share a generic first word, and a real corpus has plenty.
