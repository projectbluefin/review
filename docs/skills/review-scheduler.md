---
name: review-scheduler
version: "1.0"
last_updated: 2026-09-07
id: review-scheduler
one_line_purpose: Admit review work process-wide, batch transport, and never batch judgement.
entry_point: docs/skills/review-scheduler.md
category: ci-ops
status: active
dependencies: []
tags: [scheduler, capacity, throughput, rate-limit, worktree]
description: "Owns process-wide review admission, capacity measurement, subprocess deadlines, dependency circuit breakers, and repository transport reuse. Use when changing review_engine.py, capacity.py, or anything that dispatches reviews."
metadata:
  type: runbook
  context7-sources: [/websites/textual_textualize_io]
---

# Review Scheduler

> Batch the transport. Never batch the judgement.

Mass review is the dashboard's throughput surface: a maintainer selects many
pull requests across many repositories and expects them all reviewed, fixed,
and landed without supervision. Every rule below exists because the obvious
optimisation for that workload is the wrong one.

## The unit of batching

**Repository is the unit of transport and mutation. Pull request is the unit
of inference.**

Reviewing several pull requests inside one model session looks like the
efficient choice and is not. It serialises a busy repository into a single
lane, lets one wedged session block every pull request behind it, grows the
context monotonically across unrelated diffs, and allows findings from one
pull request to bleed into the next. The executor contract is deliberately
one item in, one receipt out, and the harness binds each review to an exact
`base...head` pair.

The cost worth removing is transport, not inference: a full clone per head is
pure duplication when twenty pull requests share five repositories. Keep one
repository mirror, batch-fetch the selected commits into it, then give every
review attempt its own isolated worktree and its own fresh model context.

## Admission is process-wide

Capacity is a property of the machine, so it must be enforced once for the
whole process. A per-dispatch executor with a per-dispatch admission check is
not a limit: dispatching N single-item batches starts N concurrent reviews
regardless of the configured cap, because each batch only ever sees its own
active set.

There is exactly one admission queue. Everything that wants to run a review
asks it, including the slay pipeline, and it is the only thing that owns a
worker pool.

## Capacity is measured, not guessed

A review is not one process. Each one dispatches the specialised check
subagents, which run concurrently, so an outer slot corresponds to a whole
descendant process tree. Size a slot by measuring that tree at peak, never by
the orchestrator's own resident set, and never by a constant chosen because it
looked safe.

Memory is not the only bound. CPU bounds admission too, and the effective cap
is the minimum of every bound that applies. Whatever the cap resolves to, the
interface must report it: a run silently throttled to two slots on a small
machine looks identical to a broken scheduler.

Choose a default by benchmarking at 1, 2, 4, 6, and 8 workers. Do not raise a
default because a formula permits it.

Benchmarking `scripts/benchmark-capacity.py` across 1, 2, 4, 6, and 8 workers
on a 6-core/12-thread host (AMD Ryzen 5 7600X, 16 GB) with five check
subagents per slot establishes `DEFAULT_REVIEW_CAP = 4`. Throughput scales
sharply from 1 to 2 workers (4.05 to 6.7 u/s), gains modestly from 2 to 4
(7.7 u/s), and then **plateaus**: 4, 6 and 8 workers all land within a few
percent of each other across five repetitions, which is inside the run-to-run
spread. Peak descendant-tree RSS, by contrast, keeps scaling linearly — about
810 MB at 4 workers, 1200 MB at 6, 1600 MB at 8.

So the cap is set by memory, not by a throughput cliff. Past 4 workers each
extra slot costs roughly 400 MB and buys no measurable throughput. Three
repetitions were not enough to see this: they produced an apparent 15%
regression at 8 workers that five repetitions showed to be noise. Smaller
machines throttle below this automatically via `cores // 2`.

## Deadlines are mandatory

Every subprocess the scheduler starts has a deadline: the review executor, and
every clone, fetch, and checkout. A call without a deadline is an unattended
run's permanent stall, and it will not be noticed because the surrounding
machinery is still nominally healthy.

A scheduler with no capacity must block until capacity exists. It must not
spin.

## Dependency failures are circuit breakers, not errors

GitHub, Hive, and the model provider fail independently and must be broken
independently. A shared pause per dependency prevents twenty reviews from
independently rediscovering the same outage.

`gh` is invoked as a subprocess and its rate-limit responses are ordinary
non-zero exits, so nothing notices a secondary rate limit until the work is
already failing. Route every GitHub call through one place that honours
`Retry-After` and `x-ratelimit-reset`, then applies capped exponential backoff
with jitter. Continuing to issue requests while limited risks losing API
access for everyone sharing the token.

Reads retry automatically. **Mutations retry only where idempotency is
proven**; an ambiguous timeout on a mutation is not a licence to repeat it.

States are explicit and durable: `blocked`, `retry_at`, and terminal outcomes
are values the interface can show, not conditions inferred from silence.

## Run state and diagnostics have different lifetimes

An unattended run accumulates something per pull request in several places at
once: a review batch, a landing task, a triage entry, a permission answer, and
a trace line. None of that may grow for the lifetime of the process.

Separate the two kinds of state and give each its own bound. **Run state** is
what the appliance still has to act on -- it belongs in the durable run store,
which bounds itself and survives restart. **Diagnostics** are what a human
reads afterwards; they belong in one size-capped rotating log, and losing an
old segment must cost no decision.

Every in-memory collection gets a bound or a prune. Eviction is oldest-first
and never removes live work: a running batch still carries its watcher and its
events, and an unstarted or active landing is still owed execution. Two traps
recur:

- **`id()` is reused once an object is freed.** A set of `id(task)` left
  behind by an evicted task will misreport an unrelated later task as active.
  Discard the membership entry in the same step that drops the object.
- **A cap on a parsed transcript is a correctness bug, not a memory fix.**
  Review output is parsed into a verdict, so silently truncating it hands
  findings from a partial capture to the landing gate. Fail closed instead:
  past the cap, refuse the verdict.

State that is written and never read is not a cache to bound -- it is a leak
to delete.

## Rationalisations

| Rationalisation | Reality |
|---|---|
| "One session per repository shares context, so it is cheaper." | It serialises the repository, bleeds findings between pull requests, and makes one wedged session a repository-wide outage. The saving is in transport, which is reusable without sharing a context. |
| "The cap is already configurable, so raise it." | Exposure is not enforcement. Verify the cap binds process-wide before changing its value. |
| "Measure the Python process to size a slot." | A slot is a process tree including every check subagent. Measure the tree. |
| "Retry the failed mutation." | Only when idempotency is proven. Otherwise an ambiguous timeout becomes a duplicate action. |
| "Raise concurrency first, add throttling later." | Concurrency without a rate-limit strategy is how a shared token gets banned. |
| "Cap the collection and move on." | A cap on state something still parses or acts on changes behaviour. Bound caches; fail closed on evidence. |

## Red flags

- An executor or worker pool constructed anywhere except the one scheduler.
- An admission check whose active set is scoped to a single dispatch.
- A `subprocess` call in the review path with no timeout.
- A capacity constant that no measurement produced.
- A GitHub call that bypasses the throttled entry point.
- A busy-wait where a blocked state belongs.
- An append-only log or an unbounded collection on the per-pull-request path.

## Verification

```bash
python3 tests/review_scheduler_contract.py
python3 tests/review_engine_contract.py
bash tests/dashboard-contract.sh
```

`tests/soak_contract.py` imports the dashboard, so it runs in the Textual
virtualenv that `tests/dashboard-contract.sh` builds rather than under a bare
`python3`.

The scheduler's defining test: dispatch far more reviews than the cap as
independent single-item requests, and assert that concurrent reviews never
exceed the effective cap process-wide.
