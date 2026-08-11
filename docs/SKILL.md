# review Skill Router

After reading [`AGENTS.md`](../AGENTS.md) and the local
[agentic model](factory/agentic-model.md), choose the one task-specific
document below. Load only the matching skill.
[`contribution-culture.md`](skills/contribution-culture.md) is the exception:
it scopes every task, so read it alongside the matching skill.

| Task | Skill |
|---|---|
| Scope a change, size a pull request, or address a maintainer | [`contribution-culture.md`](skills/contribution-culture.md) |
| Change a launcher recipe, VM mode, or container-only mode | [`launcher.md`](skills/launcher.md) |
| Investigate the contributor runtime, task delivery, or token lifetime | [`hive-runtime.md`](skills/hive-runtime.md) |
| Investigate an assigned-task or connection problem | [`hive-triage.md`](skills/hive-triage.md) |
| Report evidence to or follow up on a `kubestellar/hive` issue | [`upstream-hive.md`](skills/upstream-hive.md) |
| Change Goose configuration or skill loading | [`goose-context.md`](skills/goose-context.md) |
| Change the contributor image or pinned image inputs | [`image-build.md`](skills/image-build.md) |
| Change the maintainer review dashboard or its pilot tests | [`review-dashboard.md`](skills/review-dashboard.md) |
| Change the public static pull-request queue | [`static-pr-queue.md`](skills/static-pr-queue.md) |
| Prepare a branch, commit, or pull request | [`pr-workflow.md`](skills/pr-workflow.md) |
| Triage, label, or route an issue or pull request | [`pr-workflow.md`](skills/pr-workflow.md) |
| Maintain documentation, skills, or factory compliance | [`skill-improvement.md`](skills/skill-improvement.md) |

`docs/skills/index.json` is the machine-readable catalog, generated from the
frontmatter in each skill file. When changing a skill, regenerate it with
`bash scripts/check-skill-frontmatter.sh --write` in the same change.
