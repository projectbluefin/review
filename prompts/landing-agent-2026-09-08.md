# Bluefin Review — PR-first landing agent brief

**Repository:** https://github.com/projectbluefin/review
**Prepared:** September 8, 2026
**Audited main:** `5f5e1aa5084d67d7d2f3af580d4be067a9a782ab`
**Goal:** Keep the completed PR and issue dispositions source-backed, then
maintain the #366 security hold. #420, #398, #418, #404, #427, and #416 are
merged; #366 is the only held open PR and is not a quota to merge.

This is guidance, not a static queue or source of live execution authority. Re-read current GitHub state and repository instructions before acting; commit prompt updates only when explicitly requested. This audit inspected connected GitHub sources, diffs, comments, CI results, and selected logs; it did not locally execute the project, rebase branches, merge PRs, or prove native runsc operation.

### Current queue authority

- Merged: #420 → `a91e6c49`, #398 → `34f81eba`, #418 → `c8244270`,
  #404 → `b08a941a`, #427 → `e0942c0e`, and #416 → `5f5e1aa5`.
- Held: #366, head `e8197c553bd472c0102e9e08b2059565e1f1ed90`; keep it draft
  and held.

Do not reopen or re-land the merged entries, add #425 or #426 work, or fan out
new issue work. The ten named issue outcomes are closed/completed; only #366's
hold/proof gate and warranted prompt guidance remain.

## Working agreement

Act as the maintainer's landing coordinator, not a feature factory. Read `AGENTS.md`, `docs/SKILL.md`, and the closest current launcher, dashboard, landing, runtime, and testing skills. Current code and later accepted maintainer decisions take precedence over obsolete issue plans. Preserve unrelated worktrees, dirty files, active containers, credentials, and Jorge's work. Use isolated worktrees. Do not reset, prune, or delete foreign resources.

Use one integration writer. Read-only analysis may run in parallel, but overlapping patches must not race. Use the existing PRs and original author branches where authorized. Verify fork write access and maintainer-edit permission; when unavailable, return an exact patch and owner action instead of silently replacing another contributor's PR. Do not force-push, use admin merge, bypass checks, self-approve as an independent reviewer, or convert a permissions problem into a workflow security workaround.

Normal authorized squash merges are the objective, after required checks and review on the actual current head. Starting this maintenance session is not permission to weaken runtime policy, deploy to a cluster, change host settings, or merge a held security PR. Do not alter Hive's assignment/admission protocol.

## Phase 0 — rediscover and establish the baseline

Run read-only discovery first:

```bash
repo=projectbluefin/review
git status --short
git worktree list
git remote -v
gh api "repos/$repo/commits/main" --jq .sha
gh pr list --repo "$repo" --state open --limit 100 \
  --json number,title,url,headRefName,headRefOid,isDraft,updatedAt
for n in 366; do
  gh pr view "$n" --repo "$repo" \
    --json number,title,state,isDraft,headRefOid,headRefName,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,files,reviews,comments,maintainerCanModify
done
```

Also read unresolved review threads, issue comments, and repository rules. A historical green check is not evidence for a rebased head. The merged queue above is closed history; do not reopen it. Keep #366 draft/held with its precise security gate.

Current main includes the executable-mode fix, dependency refresh, test/import/
manifest repairs, #427 maintenance alignment, and the #416 launcher state
locality fix. Verify those merged outcomes when needed; do not reimplement them.

## Phase 1 — existing PR landing order

The ordinary PR landing phase is complete:

```text
#420/#398/#418/#404/#427/#416 merged
  -> #366 remains draft/held
```

The completed #420/#398/#418/#404/#427/#416 merges are historical evidence only.
Do not reopen their branches, repeat their repairs, or turn their issue
references into a new implementation fan-out.

### PR #366 — update the obsolete hold, do not merge it

Current head: `e8197c553bd472c0102e9e08b2059565e1f1ed90`; draft and behind.
Keep #366 held. It restores #349 after rollback #365; it is not currently
active merely because #349 once merged.

Read Common #1039's latest review and final comment. That PR closed unmerged; its old merge/publication dependency is no longer actionable. Jorge's final direction is image-layer delivery of **host runsc**, not a repaired runtime installer and not runsc installed inside the Review workload image.

After ordinary PRs stabilize, refresh this branch only under the ownership rules. Reconcile draft/hold text and the relevant #348/#362/#351 dependency references with a real accepted host-image delivery owner. Do not invent a replacement PR or claim a delivery date. No cross-repository implementation or broad issue rewrites are authorized by this brief; return the specific linked update when another maintainer must make it.

Main now has Kubernetes contributor/dashboard routes and `scripts/review-session-runtime.py`. The dashboard Pod manifest does not select `runtimeClassName` or prove runsc. A launcher-host `runsc` binary does not prove a cluster node's runtime. Inventory every current agent-capable execution path, including broker jobs and fallback branches, before claiming all-path isolation. Do not silently narrow #348 or add an unapproved cluster rollout. Record the required maintainer decision and proof gate.

Keep draft/hold until supported delivery and native acceptance prove the actual product path. Native evidence must bind host/image identity, architecture, kernel, Podman/runsc versions, rootless behavior, runtime identity on real workloads, ordinary permitted networking, credential ordering, cancellation/detached stop, and owned-resource cleanup. Each claimed architecture needs separate native evidence. Mocks, a multi-arch manifest, and a green image build are not native acceptance.

Never use `ignore-cgroups=true`, host networking, a fallback to crun/runc/default runtime, broad host-UDS exposure, or disabling SELinux to make this gate pass. Refreshing the held PR is not the same as unblocking it.

## Phase 2 — current issue dispositions

All ten named issue outcomes are closed/completed on live main. Keep their
source evidence available for audit, but do not reopen them or create issue
fanout:

| Issues | Current disposition | Evidence owner |
|---|---|---|
| #397, #417 | Closed/completed | Merged #398 and #418 |
| #392 | Closed/completed | Merged #404 |
| #400 | Closed/completed | Merged #416 |
| #393, #403, #395 | Closed/completed | Current image, knowledge-path, and PR-title contracts |
| #359, #360, #405 | Closed/completed | Current Hive v4 and maintenance alignment |

The remaining objective is #366's host-runtime hold and proof gate, plus
warranted source-backed prompt guidance. Do not manufacture code changes to
increase a completion count.

## Explicitly outside today's implementation package

#415's remaining broad deletions are product decisions. Jorge's September 7 follow-up says #406–#414 are complete. Preserve live batch ActionPlan capabilities, Headroom telemetry, lab/exec broker functionality, and final-review policy rounds unless separately authorized. Reconcile old #385–#391 plans before ever dispatching them; do not resurrect removed interfaces or delete live ones to hit the old line-count target.

Do not expand into #351 Shadow Mode, the #362 physical execution engine, #421's separate image proposal, #425 or #426, broad harness/backend parents, new schedulers, speculative ACP work, or an entire board rewrite. The existing Python batch engine and Kubernetes dashboard helper are not proof that #362's distinct physical-engine contract is complete.

## Verification and evidence contract

Run the current required repository commands; the following audited baseline is a starting point, not permission to ignore newer gates:

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/sbom-manifest.sh
bash tests/image-contract.sh
bash tests/bluefin-review.sh
bash tests/dashboard-contract.sh
python3 tests/lab-broker-contract.py
bash tests/worktree-guard.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
pre-commit run shellcheck --hook-stage manual --all-files
```

Run `bash tests/runsc-isolation.sh` on #366 where that suite exists, and the newly wired registry/import-root tests on their relevant heads. Use the pinned Textual environment and appropriate Pilot/visual evidence when UI behavior changes. Re-fetch head/base/checks/review immediately before every merge. A conflict resolution changes the reviewed artifact.

For every PR, return: current head/base; exact changed scope; reproduced failure; minimal repair; local commands and results; required hosted checks with links; independent review status; actual merge SHA or precise remaining blocker. For every issue, record the source commit, test/run or native receipt, acceptance fulfilled versus unproved, and why it is closed, narrowed, or held. Do not label “green main” as “every pending PR is ready.”

Final output must distinguish MERGED, VERIFIED/CLOSED, UPDATED-BLOCKED, and NOT-ATTEMPTED. Keep #366's missing native/runtime coverage visible. Do not end with “all done” while checks are pending or a native claim is unproved.

## Primary evidence to re-open

- https://github.com/projectbluefin/review/commit/5f5e1aa5084d67d7d2f3af580d4be067a9a782ab
- https://github.com/projectbluefin/review/pull/416
- https://github.com/projectbluefin/review/pull/366
- https://github.com/projectbluefin/common/pull/1039#pullrequestreview-5127325531
- https://github.com/projectbluefin/common/pull/1039#issuecomment-5563786674
- https://github.com/projectbluefin/bluefin/issues/1139
- https://github.com/projectbluefin/review/issues/415#issuecomment-5575951509
- https://github.com/projectbluefin/review/issues/405#issuecomment-5575683549
- https://gvisor.dev/docs/user_guide/rootless/
- https://docs.podman.io/en/latest/markdown/podman.1.html
