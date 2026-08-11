---
name: upstream-hive
version: "1.3"
last_updated: 2026-08-07
id: upstream-hive
one_line_purpose: File and follow up on kubestellar/hive issues as an exemplary downstream.
entry_point: docs/skills/upstream-hive.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [hive, upstream, issues, evidence, contribution]
description: "Defines how this repository reports evidence to kubestellar/hive and follows up on filed issues. Use before opening, commenting on, or responding to an upstream Hive issue or pull request."
metadata:
  type: policy
---

# Upstream Hive

## When to Use

Load this before opening an issue, adding a comment, or responding to a
maintainer on `kubestellar/hive`, and when a filed issue needs follow-up.

We are a downstream consumer of Hive's contributor protocol. Reporting what we
observe is expected work, not a favor. The standard is to be the downstream an
upstream maintainer wants: precise, evidence-first, and never presuming their
decisions.

## When Not to Use

Do not use this for a gap owned by this repository, by
`projectbluefin/fsdk-containers` (base-image content), or by
`projectbluefin/common` (org skills). Route by who owns the broken thing, not
by which repository is easiest to file in.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "A local workaround is faster than an upstream fix." | It is, once. Then it is permanent, undocumented, and diverges at the next pin bump. Accepted upstream gaps get no local workaround. |
| "They will not act on it, so why file?" | An unreported gap is indistinguishable from one nobody hit. Evidence with a reproduction is what makes it actionable. |
| "I will file it and move on." | Filed issues are followed up here. An abandoned issue is a gap that stopped being tracked. |

## Upstream Facts

Verified 2026-08-04 against `kubestellar/hive`:

- The default branch is `v2`. Cite code there, not `main`.
- There is no `CONTRIBUTING.md`, `AGENTS.md`, code of conduct, issue template,
  or pull request template. Absent guidance is not permission to invent our
  own process; follow the conventions their repository visibly practices.
- They use Kubernetes/prow-style labels: `kind/*`, `priority/*`, `triage/*`,
  `needs-triage`, `size/*`, `lgtm`, `ai-generated`, and `dco-signoff: yes|no`.
- Merged pull requests carry `Signed-off-by:` and use `Refs #NNNN` to link a
  parent issue.
- Maintainers reply to design issues with a comment opening `DESIGN-RESPONSE`.

## What "Accepted Upstream Gap" Means

Both halves of the phrase are load-bearing, and the rule that follows from it
depends on reading both correctly.

An **upstream gap** is a missing capability in Hive's contributor protocol,
assignment flow, or contributor runtime — the surface Hive owns and we consume.
A missing capability in the pinned base image is **not** an upstream gap. It is
a base-image capability gap; it is filed on
[`projectbluefin/fsdk-containers`](https://github.com/projectbluefin/fsdk-containers)
under the same evidence discipline as this document, and it is governed by
[`image-build.md`](image-build.md): use the tools the image already ships, and
where one is genuinely missing, add it at the FSDK seam so every consumer is
fixed at once. Different tracker, same rules — reproduce it, file it, reference
the number, and never describe it in a document instead. Never hand-roll a
local reimplementation of standard userland. There is no
standing exception to this and no wording that creates one, so there is no
base-image precedent to cite for shimming a protocol gap either.

A gap is **accepted** once an upstream maintainer has responded with a
decision — typically a `DESIGN-RESPONSE` comment, but any explicit maintainer
answer counts. Before that response the gap is an open report, not an accepted
one, and the correct behavior is the same either way: we wait. An unanswered
report is not a licence to work around the gap while we wait, and an accepted
one is not a licence to work around a decision already made.

So for a Hive protocol gap there is no state in which a downstream retry, poll,
timeout, negotiation, fallback, or shim is correct. Moving the pin is the fix
once upstream ships one, and until then we run with the gap and say so in the
issue.

## Core Process

1. **Report evidence, not prescriptions.** Give observations, reproductions,
   measurements, and code citations. Offer options with tradeoffs and an
   explicit gate; let maintainers choose. Never open with a proposed patch to
   their architecture or a recommendation phrased as a requirement.
2. **Name our own bugs first.** Where a finding is a `projectbluefin/review`
   design consequence, say so plainly in the same comment. Retract our own
   incorrect claims explicitly rather than quietly dropping them.
3. **Cite code by pinned permalink.** Link a specific commit SHA with line
   anchors, never a branch path that will drift. Timestamp live probes in UTC
   and record the exact request and response.
4. **File as a child of the field-notes parent.** Downstream findings open with
   a `Part of #NNNN` reference to the tracking issue so maintainers can see the
   whole report as one body of work.
5. **Leave triage to them.** File issues unlabeled and unassigned. Do not apply
   `kind/*`, `priority/*`, or `triage/*`, do not set milestones, and never add
   or remove a task-admission label in any repository to influence what work
   Hive assigns.
6. **Follow up when the state changes, not on a timer.** Add a comment when we
   have new evidence, when a maintainer asks a question, when a shipped change
   makes our report stale, or when a finding is disproven and should be
   withdrawn. Update the parent issue's status when children resolve.
7. **Answer a `DESIGN-RESPONSE` in kind.** Acknowledge the decision, state
   agreement or the remaining concern with evidence, and confirm what we will
   not build downstream. Do not relitigate a settled direction.
8. **Keep the boundary explicit.** State in the issue that we are not adding
   client-side selection, retry, negotiation, filtering, or fallback
   heuristics. A downstream workaround becomes their future compatibility
   burden, so waiting for the real field is the cooperative choice.
9. **Sign contributions if we send code.** Any pull request to Hive needs a
   `Signed-off-by:` trailer for their DCO check and a scope-matched title.
   Keep it small and single-purpose, and disclose agent authorship in line
   with their `ai-generated` label practice. Upstream maintainer attention is
   the cost we are trying to lower, not spend.

## Red Flags

- Prescribing an implementation, or writing an issue as a change request.
- Citing a branch path instead of a pinned commit SHA.
- Labeling, assigning, prioritizing, or milestoning an upstream issue.
- Filing a report we cannot reproduce, or omitting the timestamp of a probe.
- Presenting a downstream design consequence as an upstream defect.
- Building a local workaround for an upstream protocol gap, accepted or still
  awaiting a response.
- Citing a base-image shim as precedent for a protocol workaround.
- Reopening a direction a maintainer already decided in a `DESIGN-RESPONSE`.
- Filing a new issue for evidence that belongs on an existing one.
- Describing a gap in a document instead of filing it. A paragraph cannot be
  assigned or closed; it survives the fix and reads as acceptance.

## Verification

- [ ] Every code claim links a pinned commit SHA with line anchors.
- [ ] Every live probe records its UTC timestamp, request, and response.
- [ ] The issue offers options and a gate, not a mandated solution.
- [ ] Ours-versus-theirs attribution is stated explicitly.
- [ ] No label, assignee, priority, or milestone was set upstream.
- [ ] No corresponding workaround was added to this repository.

```bash
gh issue view <number> --repo kubestellar/hive --comments
bash scripts/check-skill-frontmatter.sh
```
