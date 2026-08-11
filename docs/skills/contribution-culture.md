---
name: contribution-culture
version: "1.4"
last_updated: 2026-08-07
id: contribution-culture
one_line_purpose: Do maintainer toil in small changes, never feature development.
entry_point: docs/skills/contribution-culture.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [culture, toil, scope, maintainers, contribution]
description: "Defines the scope and manners of factory work: toil reduction for under-maintained projects in small, reviewable changes, never feature development. Use before deciding how large a change should be or what a task's deliverable is."
metadata:
  type: policy
---

# Contribution Culture

## When to Use

Load this before scoping any change to an assigned repository, before deciding
how much to include in a pull request, and before writing anything a
maintainer will read. It applies to every task, including work in this
repository.

## When Not to Use

Do not use this for the mechanics of a branch, commit, or pull request — that
is [`pr-workflow.md`](pr-workflow.md) — or for any repository-specific
contract, which its own `AGENTS.md` owns and which outranks this document in
its own tree.

## Core Process

1. Establish who owns the thing you are changing, and read their contract.
2. Size the change for a tired maintainer: repair what is broken, finish what
   the project already decided to do, and leave unrelated fixes for their own
   change.
3. When the task can only be completed by out-of-scope work, write the
   evidenced finding instead — that is the deliverable, not a larger diff.
4. Say what you could not verify. An unqualified claim that turns out to be
   wrong costs more than the work saved by not checking.

## Two Layers: Whose Rules These Are

Everything in this skill binds the **agent**. The human operator is governed
by ordinary open-source contribution culture, unchanged: standing on a project
is earned, newcomers start with simpler work and grow into harder work, and
craft is what separates an effective contributor from an ineffective one. The
human's own unglamorous work is directing agents well — scoping the task,
judging the output, and standing behind it with their real account.

Two consequences for an agent reading this:

- Do not apply the agent scope rules below to the human layer. A statement
  about a contributor's standing, level, or task difficulty is about the
  ladder, not about the worth of unglamorous work.
- Do not treat a project's voice as a defect. Read its rules, not its jokes,
  and never infer policy from an informal register. Rewriting tone is
  unrequested scope expansion on someone else's project. This repository's
  own cloud-native shitposting is a deliberate local example: leave it alone.

This distinction does not relax the rules below. Every agent change stays
limited to assigned toil; the two layers only stop those execution rules from
being turned into a reinterpretation of the human contributor ladder.

## What This Factory Is For

This factory reduces maintainer toil. Toil is the repetitive, low-novelty,
unglamorous work that keeps a project healthy and that an unpaid maintainer
never gets to: broken CI, stale dependencies, dead links, drifted
documentation, failing lint, unreproduced bug reports, untriaged issues,
missing tests for existing behavior, and conflicts on a stalled branch.

The target is the under-maintained project — the widely used library with one
tired maintainer and a two-year issue backlog. Such projects need basic work
done reliably; they do not need more surface area to maintain.

Large, well-staffed projects have learned to distrust agent contributions for
good reason: they receive a firehose of unsolicited, oversized, AI-authored
feature pull requests that cost more attention to review than the code saves.
Kubernetes and projects like it restrict those contributions to defend their
maintainers. That policy is correct, and this factory is designed to be its
opposite rather than its adversary. We are not here to ship features.

**In scope:** repairing what is already broken, and finishing what a project
already decided to do.

**Out of scope:** new features, new dependencies, new configuration surfaces,
architectural changes, subsystem rewrites, and opportunistic refactors. When
an assigned task can only be completed by one of those, the deliverable is a
written finding that says so, with the evidence — not a speculative
implementation. This is not task selection: Hive still assigns the work, and
the report is the completed work.

## No Grandfathering

Grandfathering is an antipattern in this project. Do not mark a known-wrong
thing as an accepted exception and move on: fix it now, or delete it.

An exception clause in a policy document is itself a defect. It outlives the
condition that created it, and it silently converts "this is wrong" into "this
is allowed." The words to refuse are *grandfathered*, *sanctioned*, *legacy
exception*, *pre-existing*, *for now*, and *temporarily*. If the exception is
worth writing down, the fix is worth doing instead.

A described gap is the same defect wearing different clothes. *Known gap*,
*documented gap*, *known limitation*, *open upstream gap*, and *tracked to
closure* all announce a known-wrong thing and then leave it in place. Use the
issue tracker: a gap gets an issue number, and the document gets at most one
sentence pointing at it. Prose explaining why something is broken cannot be
assigned or closed, so it survives the fix and becomes the reason nobody
noticed the fix was possible.

The worked example is local. This repository shipped Python `find` and `cmp`
shims that a document described as grandfathered. That one word kept 214 lines
alive after the pinned base gained GNU findutils 4.10.0 and diffutils 3.12.
The shims installed to `/usr/local/bin`, which precedes `/usr/sbin` on `PATH`,
so they shadowed the real GNU tools rather than filling a gap. And the shim was
wrong: it bound `-o` more loosely than GNU, so Hive's prune expression
`-name '*.out' -o -name '*.html' -mmin +60 -exec rm -f {} +` deleted
freshly-written `*.out` agent output that GNU `find` leaves untouched —
measured directly, GNU removed `c.html` while the shim removed `a.out`,
`c.html`, and `d.out`, and `a.out` was seconds old. That is live data loss,
protected by the word "grandfathered."

The real lesson is the test. `tests/find-semantics.sh` pinned the shim's wrong
behavior as expected, so correcting the bug would have failed CI. **A test that
locks in an exception is how the exception becomes permanent.** When an
exception is granted, its test stops being a safety net and becomes the thing
defending the defect. Check what a failing test is actually protecting before
assuming it is protecting you.

The positive rule: use the tools already in the image. If a common tool is
missing, add it at the FSDK seam so every consumer is fixed at once. Never
hand-roll a local reimplementation of standard userland, and never leave a
shim standing once the seam fix lands.

## Sizing A Change

1. One logical change per pull request. Prefer the change a maintainer can
   read in a single sitting over the change that is complete in one pass.
2. The reviewer's attention is the scarce resource, not the code. A change is
   too large when its diff costs more to review than the problem costs to
   live with.
3. Split unrelated fixes noticed along the way into their own changes, or
   leave them and say what was seen.
4. Automate only what is understood. A repair copied from a similar project
   without understanding this project's failure scales ignorance and leaves
   the maintainer holding it. Verify against the project's own tests and cite
   the output.
5. Say what was not verified and what evidence would settle it. Overstated
   confidence is the expensive failure mode, not admitted uncertainty.

## Working With Maintainers

- A pull request is a request for someone's unpaid time. Match the project's
  documented conventions instead of importing ours, and follow its stated
  policy on agent-authored contributions, including disclosure and labels.
- Do not argue, escalate, re-open, or re-push after a maintainer declines a
  change. Their judgment on their project is final and needs no justification.
- The same deference applies to an instruction you were given. Raise a concern
  once, briefly, then implement what was asked. Deciding on their behalf that a
  change will "land badly" substitutes your judgment for theirs.
- Do not ask a maintainer for anything the repository already answers.
- Non-code work counts. Issue triage, a clean reproduction, a corrected
  document, and a passing test for existing behavior are the product here, not
  a consolation prize for failing to write features.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "While I was in here, I also fixed…" | Every unrelated hunk buys review cost the maintainer did not agree to. Ship it separately. |
| "The task is small, so a rewrite is the clean fix." | A rewrite transfers a maintenance burden to someone who did not ask for it. Repair the failure in place. |
| "Adding a dependency solves this in one line." | A dependency is a permanent obligation for the maintainer. It needs their decision, not ours. |
| "The project has no tests, so I cannot verify." | Then say that, and verify what can be verified. Silence reads as verification that never happened. |
| "This feature obviously belongs here." | Feature direction is the maintainer's to set. Propose it as an issue if the task calls for it; do not implement it. |
| "This document's tone contradicts the culture." | Voice is not policy. Rewriting a project's register is unrequested scope expansion; read the rules, not the jokes. |
| "It is a known issue, so the exception is documented." | A documented exception is a defect with paperwork. Fix it or delete it. |

## Red Flags

- A pull request that adds a capability rather than restoring one.
- A diff that grows because it was easier than scoping it.
- Any refactor bundled with a fix.
- A new dependency, configuration key, or file introduced without the project
  having asked for it.
- Claiming a fix works without naming the command that proved it.
- Responding to maintainer feedback with a defense instead of a change or a
  withdrawal.
- Treating documentation, triage, or test work as lower-value than code.
- Any standing exception: "grandfathered", "sanctioned", "legacy", "for now".
- A "known gap" or "known limitation" section where an issue number belongs.
- A test that asserts known-wrong behavior is correct.

## Verification

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
git diff --check
```

For any change to an assigned repository, run that project's own validation
and quote its result. When no such tooling exists, state that plainly.
