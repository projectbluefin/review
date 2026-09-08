# Bluefin Agentic Factory Feedback Loop

This document is the canonical local model for `review`. It adapts
[`projectbluefin/common`'s Agentic Operating Model][common-model] to this
repository's two-mode review appliance. Read it after
`AGENTS.md` and before task-specific skills.

The published image derives from the pinned lab-runner base and includes the
Goose/Hive contributor worker and maintainer dashboard. Its two launch modes
share the same image while keeping Hive assignment authority separate from
the human review surface.

The model is documentation: the launcher, image, tests, skills, and
user-facing instructions must describe the same roles and authority
boundaries. When source evidence changes the model, update this document and
the affected local contract together. Do not preserve superseded plans,
session logs, or design scratchpads as competing explanations.

## Roles and authority

| Term | Meaning | Authority |
|---|---|---|
| **Bluefin Agentic Factory Feedback Loop** | The lifecycle that turns agent work and test feedback into reviewed Bluefin changes. | The model for this repository. |
| **Toil** | Repetitive, low-novelty maintenance work an under-maintained project needs: broken CI, stale pins, drifted documentation, unreproduced reports, untriaged issues, stalled branches. | Toil is the work this factory exists to absorb. |
| **Contributor** | A contributor using the worker configuration to receive and complete Hive-assigned work. They are treated as a contributor, they just happen to specialize in the `clanker-queue`. It's a "subclass" of contributor like a video game RPG character. Same team, different specialization. | Hive assigns work; the worker implements only its assigned scope. |
| **Maintainer/Reviewer** | A maintainer assessing an incoming pull request. This is a role, same team but this is an active review process, brainmeat needed. | The human decides review, approval, and merge. |
| **Review Evidence** | Read-only pull-request, issue, verification, and merge-state context shown before a review. | Evidence informs a human; it never makes a decision. |
| **Managed Reviewer Client** | A foreground, preconfigured Goose session that a Maintainer Reviewer may choose after examining Review Evidence. | It prepares a Review Draft. Its merge keys only execute a typed, human-confirmed decision: asking Hive's authenticated endpoint to create the App-authored exact-head approval and `lgtm` opt-in, or a maintainer's direct squash merge, which requires GitHub's `push` permission and never overrides branch protection. It decides nothing itself. It can also submit the maintainer's own review — approve, request changes, or comment — which merges nothing and arms nothing. |
| **Portable Reviewer Prompt** | Markdown Review Evidence and queue instructions for a maintainer's own client. | It is context, not an assignment. |
| **Bluefin PR Queue** | A cached, live GitHub view of open factory pull requests and suggested next actions. | GitHub is authoritative; the queue neither assigns work nor merges. |
| **Dashboard Activity** | The high-priority, bounded display of active parent reviews, Check workers, landing agents, queued work, and freshness. | It reports live or retained evidence; it never assigns Hive work. |
| **Review Draft** | Analysis, review text, or commands prepared for a Maintainer Reviewer. | A human explicitly considers and submits it. |

Avoid classifying contributors by role; this isn't a class system it's the loadout a contributor chooses to use that day.

## Two layers

The factory has a human layer and an agent layer, governed by different rules.
Conflating them is the most likely misreading of this model.

The **agent** does the unglamorous work: the toil defined above, in small,
evidenced, reviewable changes. They are humorously referred to as clankers as a joke on the absurdity of the world we live in.

The **human** does the "unglamorous work" of directing agents — scoping a task,
judging the output, and carrying the result to a maintainer. Their standing is
earned under ordinary open-source contribution culture, which AI did not
change; projects determine it, and Hive may use it when distributing work.
Nothing in this repository sets, scores, or automates it, and the
`human-queue` is out of scope here.

The IMPORTANT DISTINCTION in the culture is that the humans take pride in maintaining systems at the highest levels. If they are doing their jobs, they are invisible. We are designing this tool because the mental toll of that maintenance is hurting people. Amongst their peers there is a culture of respect and craftmanship. The leaderboards/contribution graphs are supposed to be a friendly way to remind maintainers that their work is recognized by their peers. This is one of the highest honors a maintainer can receive. Silent professionals.

Do not reconcile the two layers by applying agent scope rules to the human, or
by reading the human ladder as a statement about agent output.

## Scope of work

This is a toil-reduction factory for under-maintained open-source projects,
not a feature factory. Factory Workers repair what is already broken and
finish what a project already decided to do; they do not add features,
dependencies, configuration surfaces, or architecture.

Well-staffed projects restrict large agent-authored pull requests because
those consume more maintainer attention than they return. That reasoning is
the model here too: the reviewer's attention is the scarce resource, so a
change is sized to be reviewable rather than to be complete in one pass. When
an assigned task can only be finished by out-of-scope work, the deliverable is
an evidenced written finding. That is completed work, not a declined
assignment; Hive's authority over what gets worked on is unchanged.

[`docs/skills/contribution-culture.md`](../skills/contribution-culture.md)
carries the operational form of this section.

## Repository boundary

`review` owns the contributor image, credential handoff, and review context.
Hive owns the contributor WebSocket protocol, task selection, assignment prompt
injection, the `contributor` tmux session, and output capture. The launcher
must not decline, retry, or otherwise manage assignments mid-protocol; the
one permitted filter is own-work exclusion on the maintainer-facing queue
view, so a reviewer never receives their own authored pull requests.
Hive also owns contributor completion. Review may display a read-only Hive
projection, but it never completes an assignment.

The human Maintainer Reviewer is the decision point. A Factory Worker,
Managed Reviewer Client, Portable Reviewer Prompt, Review Evidence view, or
Bluefin PR Queue must never claim approval, queue-management, or
task-selection authority. The merge decision is the human's: the Managed
Reviewer
Client's merge keys exist to execute the human's typed, per-number-confirmed
decision, nothing more. That decision has two shapes, and the distinction is
the point. Queueing applies `lgtm`, which is an explicit opt-in to automation:
it hands the pull request to Hive's governor sweep, which re-verifies and
merges on green CI. Merging directly performs the same squash immediately,
without arming anything. `lgtm` is therefore a choice to automate, never a
toll a maintainer must pay to land a change. The direct path is a maintainer
power — gated on GitHub's `push` permission, read from GitHub per repository
rather than assumed — and it never overrides branch protection, so a
repository that requires review or green checks still refuses.

Recording a verdict is a third, smaller thing, and it is neither of those: a
Maintainer Reviewer may leave an ordinary GitHub review — approve, request
changes, or comment — which merges nothing and arms nothing. A review that can
only be given by also queueing or merging is not a review, and the verdict a
reviewer most needs to give is the one that says no.

The pinned FSDK base owns the contributor toolchain. `review` consumes the
tools the image ships and does not reimplement them: a missing utility is
fixed at the FSDK seam, and a shim is removed the moment that fix lands. A
local reimplementation is not a neutral stopgap — it shadows the real tool on
`PATH` and silently substitutes its own semantics for the ones every caller
assumes.

## Optional lab authority

The appliance owns no lab and depends on none. A Maintainer Reviewer may
nonetheless lend one dashboard session their own cluster: the Launcher asks
once, on the terminal that started the session, and on yes runs a host-side
Lab Broker whose private Unix socket is the only thing agent-controlled work
receives. The broker is the authority boundary — it holds the Kubernetes and
issue-writing credentials, accepts three typed requests bound to session,
repository, pull request, and exact head, and dispatches only an explicit map
of QA and verification workflows. No agent may ask it for a command, a
manifest, a namespace, or a template outside that map, and the Factory Worker
receives no lab capability at all.

Automatic issue filing is the one machine authority in this model, and it is
deliberately narrow: only a verified finding, corroborated across samples,
classified into a fixed class, routed to a fixed repository
(`projectbluefin/lab` for cluster platform, `projectbluefin/server` for server
product), deduplicated by a versioned fingerprint against an existing open
issue, with bounded redacted evidence. Anything ambiguous is `unroutable` and
files nothing, and a filing failure is reported rather than swallowed. It
grants no approval, queueing, merge, or task-selection authority.

Lab evidence supplements a review and never gates one. An unavailable lab, a
degraded broker, or a failed workflow moves verification back to published
registry evidence — the same path a session with no lab always takes — and
never produces a blocked pull request.

## Final review authority

A landed batch gets a bounded final review-and-fix phase. It runs in the
existing landing lane under the maintainer's original confirmed selection: it
may commit only on branches that selection already authorized, it may not
widen scope, and it may not remove a hold, force-push, use `--admin`, or
bypass a required check. Its model is a session policy the Maintainer Reviewer
chooses once and may change; the reviewer of a round is never the fixer of
that round, and after five unsuccessful rounds the batch is visibly blocked
for a human rather than looped again.

## Documentation discipline

Keep the model executable and compact:

1. Treat local code and tests as evidence for implementation behavior.
2. Treat `AGENTS.md`, this document, and the matching skill as the
   agent-facing contract.
3. Record durable operational knowledge in `docs/skills/` and generate
   `docs/skills/index.json` from skill frontmatter.
4. Delete stale changelogs, session notes, plans, design scratchpads, and
   append-only status documents. They are historical noise, not the model.
5. Use the pinned `projectbluefin/common` catalog as a shared sidecar after
   local documentation; it complements but does not override local authority.

## Verification

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/bluefin-review.sh
bash tests/dashboard-contract.sh
bash tests/worktree-guard.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
```

[common-model]: https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md
