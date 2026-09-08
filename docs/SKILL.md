# review Skill Router

After reading [`AGENTS.md`](../AGENTS.md) and the local
[agentic model](factory/agentic-model.md), choose the one task-specific
document below. Load only the matching skill.
[`contribution-culture.md`](skills/contribution-culture.md) is the exception:
it scopes every task, so read it alongside the matching skill.

| Task | Skill |
|---|---|
| Scope a change, size a pull request, or address a maintainer | [`contribution-culture.md`](skills/contribution-culture.md) |
| Change a launcher recipe, launch mode, or container execution | [`launcher.md`](skills/launcher.md) |
| Scale out contributor workers across a Kubernetes cluster | [`cluster-workers.md`](skills/cluster-workers.md) |
| Lend host Kubernetes cluster access to the review dashboard | [`lab-broker.md`](skills/lab-broker.md) |
| Investigate the contributor runtime, task delivery, or token lifetime | [`hive-runtime.md`](skills/hive-runtime.md) |
| Investigate an assigned-task or connection problem | [`hive-triage.md`](skills/hive-triage.md) |
| Report evidence to or follow up on a `hivecommons/hive` issue | [`upstream-hive.md`](skills/upstream-hive.md) |
| Change Goose configuration or skill loading | [`goose-context.md`](skills/goose-context.md) |
| Maintain the five review check subagents and review scope | [`review-checks.md`](skills/review-checks.md) |
| Change the contributor image Containerfile or pinned inputs | [`image-build.md`](skills/image-build.md) |
| Audit image composition, SBOM manifests, SLSA, or publishing | [`image-audit.md`](skills/image-audit.md) |
| Change the maintainer review dashboard or its pilot tests | [`review-dashboard.md`](skills/review-dashboard.md) |
| Manage multi-PR landing batches and background fix-and-land | [`landing-batches.md`](skills/landing-batches.md) |
| Monitor running review containers, landing batches, and agent health | [`review-monitoring.md`](skills/review-monitoring.md) |
| Prepare a branch, commit, or pull request | [`pr-workflow.md`](skills/pr-workflow.md) |
| Triage, label, or route an issue or pull request | [`pr-labels.md`](skills/pr-labels.md) |
| Maintain documentation, skills, or factory compliance | [`skill-improvement.md`](skills/skill-improvement.md) |
| Coordinate bounded factory continuation and writable capacity | [`factory-operations.md`](skills/factory-operations.md) |
| Run or evaluate automated final batch reviews | [`final-review.md`](skills/final-review.md) |

`docs/skills/index.json` is the machine-readable catalog, generated from the
frontmatter in each skill file. When changing a skill, regenerate it with
`bash scripts/check-skill-frontmatter.sh --write` in the same change.
