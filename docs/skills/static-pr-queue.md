---
name: static-pr-queue
version: "1.7"
last_updated: 2026-08-08
id: static-pr-queue
one_line_purpose: Publish a safe, static queue of public pull requests.
entry_point: docs/skills/static-pr-queue.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [github, actions, queue, static, pullrequest]
description: "Publishes the public static PR queue without creating queue authority. Use when changing queue generation, ranking, artifacts, or refresh automation."
metadata:
  type: procedure
  context7-sources: [/websites/github_en_actions, /websites/github_en_rest]
---

# Static PR Queue

## When to Use

Use this when changing `queue/`, `public/queue.*`, or
`.github/workflows/update-pr-queue.yml`.

## When NOT to Use

Do not use the queue to select Hive work, assign agents, claim pull requests,
merge changes, or expose private repository data. Hive owns task selection;
GitHub owns pull-request state and merge decisions.

## Core Process

1. Keep queue generation dependency-free and test it with fixture-backed
   `fetch`; validation must never call GitHub's live API.
2. Fetch only configured public repositories through GitHub REST. Use
   `GET /repos/{owner}/{repo}/pulls` with `state=open`, pagination up to
   `per_page=100`, pull-request review evidence, and
   `GET /repos/{owner}/{repo}/commits/{ref}/check-runs`.
3. Validate source responses before ranking. A failed primary PR-list request
   fails the refresh and preserves the previous artifacts. Missing review,
   mergeability, or check evidence for one PR emits `investigate`.
4. Rank repository batches by open-PR count, then action, oldest activity, and
   stable PR ID. Keep labels informational; do not invent a priority taxonomy.
5. Compare the ordered JSON `items` array with the prior artifact before
   writing. Preserve `generated_at` when items are identical, or every
   scheduled refresh creates a meaningless commit.
6. Refresh only static `public/queue.md` and `public/queue.json` through a
   GitHub Pages artifact; never push generated snapshots to protected `main`.
   The root `public/index.html` redirects to the Markdown view. The queue accepts
   `repository_dispatch` type `renovate-completed` from the central
   Renovate workflow; retain its schedule as a fallback.
7. Use `GITHUB_TOKEN` with `contents: read`, `pages: write`, and
   `id-token: write` in the refresh workflow. Check out `main` explicitly and
   never use `pull_request_target` or execute pull-request head code in a
   deploy-capable job.
8. Keep the surface one document. Do not add `/org`, `/repo`, `/batch`,
   `/next`, content negotiation, `/.well-known/agent-queue`, webhooks, claims,
   leases, a key-value store, or private-repository support until a real
   consumer proves the need. The JSON is small enough to filter client-side.

## Common Rationalizations

| Rationalization | Reality |
| --- | --- |
| "A lease avoids duplicate reviews." | It duplicates Hive coordination and creates a second authority. |
| "A timestamp-only refresh is harmless." | It commits every 15 minutes without new evidence and obscures real changes. |
| "A fork workflow can run the generator with write access." | Never run untrusted PR code with a write-capable token. Fork events wait for the scheduled refresh. |
| "The queue can tell agents to merge." | It can emit only `ready-for-human-merge`; repository gates still apply. |

## Consuming The Queue

`bluefin-review queue` is the one shipped consumer. It reads the snapshot only
to decide **what to show a human next**, and re-reads every displayed fact from
GitHub live, because the snapshot can be hours old and a stale "clean" reading
is the one most likely to mislead a reviewer. Treat that split as the rule for
any future consumer: the queue orders the walk, GitHub supplies the evidence.

A consumer must not gain an approve or merge action. `ready-for-human-merge` is
a recommendation to a person, not an instruction a tool may execute, so the
menu offers neither and `tests/bluefin-review.sh` fails if a `pr merge` or
`pr review` call appears in the reviewer.

The snapshot carries each pull request's `author` so a consumer can skip the
walker's own work. Authorship is the one field that never changes after
creation, so it is safe to trust from the snapshot where state like
mergeability is not; a walk is for reviewing other people's work.

### Duplicates Are Evidence, Overlap Is Not

The queue ranks each pull request alone, so it cannot see that two entries are
the same work. Report that at the point of review, and keep two signals apart:

- **Duplicate** — the pull requests update the same dependency, or close the
  same issue. One supersedes the other.
- **Overlap** — they merely touch a file in common. That is a merge-ordering
  hazard, and both changes may still be wanted.

Measured against the live queue, shared files match 174 pairs while
same-dependency and same-issue match 13. Collapsing the two would hide every
real duplicate behind noise from one busy workflow file.

Normalise a Renovate title to the dependency it updates before comparing;
`update module <path>`, `update dependency <name>`, `<image> docker digest`,
and `<action> action` are all the same shape wearing different words. Fetch
each repository's open pull requests once per walk and cache them.

## Red Flags

- A workflow uses `pull_request_target`, a fork head ref, a personal token, or
  a direct push to protected `main`.
- A local `pull_request` trigger is expected to observe Renovate PRs in other
  repositories.
- A refresh writes an empty snapshot after a GitHub source error.
- `generated_at` changes when the ranked `items` array did not.
- Queue code mutates labels, assignments, Hive, or pull requests.
- A new action suggests feature work instead of repairing a stalled pull
  request.
- A static artifact includes private repository data or credentials.

## Verification

- [ ] `node --test queue/test/queue.test.mjs` passes.
- [ ] A simulated source failure leaves both prior queue artifacts unchanged.
- [ ] A repeated identical fixture leaves `generated_at` unchanged.
- [ ] `pre-commit run --all-files` passes.
- [ ] The workflow checks out `main`, grants only `contents: read`,
      `pages: write`, `id-token: write`, and excludes `pull_request_target`.

## Sources

- GitHub Actions workflow permissions and `pull_request_target` security:
  Context7 `/websites/github_en_actions`.
- GitHub REST pull-request and check-run endpoints: Context7
  `/websites/github_en_rest`.
