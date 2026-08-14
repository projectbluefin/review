---
name: goose-context
version: "2.2"
last_updated: 2026-08-11
id: goose-context
one_line_purpose: Keep Goose config and skill routing working in the container.
entry_point: docs/skills/goose-context.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [goose, context7, skills, mcp, config]
description: "Keeps Goose configuration and global and repository skill routing available in the container, and records how Context7 reaches agents today. Use when Goose loses its config or misses a skill."
metadata:
  type: reference
  context7-sources: [/aaif-goose/goose, /websites/cli_github_manual]
---

# Goose Context

Maintainer reviews use Goose as the `goose` entry in the shared harness
registry. The adapter owns readiness, exact-head invocation, streaming,
cancellation, redacted evidence, and structured `ReviewResult` conversion;
body drafting is explicitly unsupported and never falls back to another
harness.

Readiness uses Goose's documented non-secret `goose info --check` surface.
An absent executable is reported separately from a present executable whose
provider check fails; review invocation is refused until that check succeeds.

## When to Use

Load this when changing Goose configuration, skill routing, or the
image-owned agent policy, or when asked to add a Context7 extension to the
image.

## When Not to Use

Do not load this for Hive assignment selection, contributor tmux lifecycle, or
task delivery; use the Hive runtime documentation instead.

## Core Process

1. Keep image-owned Goose configuration under
   `GOOSE_PATH_ROOT=/opt/bluefin/goose`. The pinned Hive runtime preserves an
   existing `~/.config/goose/config.yaml`; the controlled root still separates
   image policy, data, and state from that runtime-owned file.
2. Keep the image Copilot-only. `GOOSE_PROVIDER` may be unset or
   `github_copilot`; the entrypoint supplies `gpt-5.6-luna` and
   `GOOSE_THINKING_EFFORT=high` when callers do not override them.
3. Goose follows the upstream `canary` release. Build it with the required
   `github_token` secret so GitHub CLI can verify signed provenance from the
   official `canary.yml` workflow; never put that token in an image layer.
4. Context7 is the appliance's fresh-documentation seam and is configured in
   the image: the controlled Goose config enables the `context7` extension
   against the keyless public endpoint (`https://mcp.context7.com/mcp`), and
   the agent policy routes external API, framework, and platform questions
   through it before memory. Hive's hub still queries Context7 server-side
   (`v2/pkg/knowledge/context7.go`) and folds the result into the knowledge
   export — two deliveries of the same capability, one for assigned-task
   context, one for on-demand lookups. `tests/image-contract.sh` requires the
   extension in the config and the routing rule in the policy.
5. Treat generated global skills and repository skills differently. The image
   generates global skills at build time from the pinned
   `projectbluefin/common` catalog. After cloning a repository, `bluefin-review`
   names the target's `AGENTS.md`, optional `docs/SKILL.md`, and only active,
   described, unique catalog entries whose repository-relative entry points
   exist inside that checkout. It passes paths and provenance, never skill
   bodies; invalid optional context is named as unavailable and does not stop
   the review.
6. Keep `GOOSE_MODE: auto` in the controlled configuration. The agent runs its
   tools with no per-tool confirmation prompt, and that is required rather than
   convenient: Hive drives the CLI by simulated keystrokes, so a confirmation
   prompt blocks both the agent and the human at the terminal indefinitely.
   Goose also hard-errors in non-interactive mode when a tool confirmation is
   requested under `approve` or `smart_approve`, so `auto` is the only mode
   that works here. The compensating control is credential scope, not
   prompting: the agent holds a contributor GitHub token and runs unprivileged
   inside a disposable container, so its blast radius is that container plus
   whatever that token can reach. Prefer a `REVIEW_GH_TOKEN` limited to
   `public_repo` or `repo`, and state that tradeoff wherever the mode is
   documented.
7. Keep the image policy short. It is supplied to every Goose turn through
   `GOOSE_MOIM_MESSAGE_FILE`.
8. Treat the policy strings asserted by `tests/image-contract.sh` as contract
   anchors. Preserve them when changing policy wording, or intentionally update
   their test assertions in the same change.

Goose discovers native skills at session start, before an assigned repository
exists. Repository skills therefore require the explicit catalog lookup; they
are not automatically discovered. Generated `.agents/skills/` content is
build output and must not be committed.

The pinned Hive runtime links its refreshed knowledge export to Goose-native
`AGENTS.md` and `.goosehints`. Do not add `CONTEXT_FILE_NAMES` merely to retain
the legacy `CLAUDE.md` link; keep auto-loaded files concise.

Dashboard mode has no Hive session, so the entrypoint acquires the same context
through Hive's own seam: it sources `/etc/hive/entrypoint.d/*.sh` (whose hook
owns the hosted hub URL and the authenticated curl rewrite) and fetches the
export with upstream's exact `api/knowledge/export` expression. Do not hardcode
the hub URL or the hosted API path anywhere else — the hook is the single
definition.

**Auto-loaded context is charged per subprocess, so measure it before linking
it.** The export lands at `~/agent.md` and is deliberately not linked to
`AGENTS.md`, `.goosehints`, or `.goose-instructions.md`. Goose loads those
files into every subprocess it starts and `goose review` starts one per check,
so linking a 417 KB export spent a large fraction of each check's window
before the diff was read; checks then answered with prose or an empty response
instead of JSON, and `goose review` reported the resulting run as `0
finding(s)` with exit 0. Name a large document by path and let the agent search
it. Upstream writes a non-empty "Knowledge base not yet available." placeholder
on fetch failure, so `bluefin-review` checks content, not just size, before
naming the export in its instructions.

## Review Context Is Not Session Context

`goose review` does **not** read `~/.agents/skills`. Without `--check-scope`
it discovers its own context from `.agents/REVIEW.md` and `.agents/checks/*.md`
inside the repository being reviewed. Verified with `goose review --dry-run`:
a check placed at the reviewed repository's root is reported as
`discovered ... (scope: <root>)`, while the same file under
`$HOME/.agents/checks` is ignored — unless a `--check-scope <DIR>` names an
out-of-tree directory, in which case that directory's `.agents/` REPLACES
repo-root discovery entirely. This image ships such an overlay at
`/opt/bluefin/review-scope/.agents/`, and `bluefin-review` copies it into a
per-run scratch scope (adding a per-stop `cluster-resolution` check when a
duplicate cluster exists) before every review. A reviewed repository's own
`.agents/checks/` is therefore suppressed under the overlay; Bluefin
repositories keep their checks in the shared overlay instead, and the
additive-flag gap is filed upstream as
[aaif-goose/goose#11060](https://github.com/aaif-goose/goose/issues/11060).

Two consequences:

1. Projecting an org skill into `~/.agents/skills` puts it in *session*
   context only. It does nothing for a review.
2. The reviewed repository belongs to someone else, so writing `.agents/`
   files into it is not available as a fix.

Use `--instructions`, which is additive. `--prompt` replaces Goose's embedded
review prompt wholesale and throws away its correctness and code-quality
passes. Name the documents on disk rather than inlining them: the projected
`pr-review` skill plus its references is roughly 33 KB, and Goose dispatches
one subprocess per check, so an inlined copy is paid on every check of every
review. `bluefin-review` sends a sub-1 KB pointer covering `pr-review`,
`queue-feed`, `hive-review`, `human-gates`, and Hive's `~/agent.md` knowledge
export, and omits any entry whose file is absent.

## Skill References Must Be Projected Too

`scripts/generate-skills.py` writes `SKILL.md` from each manifest
`entry_point`. Skill bodies also link sibling material as
`references/<name>.md`, and the manifest does not list those files, so they
are resolved from the body. Without that, `pr-review` alone shipped four
dangling links. Reference names come from a fetched body rather than the
manifest, so treat them as untrusted: accept a plain `<file>.md` sibling only,
and reject any path separator or traversal segment before joining a path.

## Common Rationalizations

- "The policy wording is cosmetic." The image contract validates specific
  skill-routing strings, the Context7 extension in the Goose config, and the
  documentation-first routing rule in the policy; change its assertions with
  the wording when the intended behavior changes.
- "A repository skill will load automatically." Native discovery occurs before
  the assigned repository exists, so repository skill routing needs the
  explicit catalog lookup.
- "The policy can contain every skill body." Persistent instructions consume
  context every turn; route to the relevant document instead.
`GOOSE_NO_CODE_TRUNCATION=true` keeps full code blocks visible during reviews
without increasing the bounded tool response size.

## Red Flags

- Treating Hive's preserved runtime config as the image-policy seam.
- Treating the mutable canary tag as a checksum pin or passing its verification
  token through a build argument.
- Calling repository skill discovery automatic or guaranteed.
- Committing generated `.agents/skills/` output.
- Growing `AGENTS.md` with content only some tasks need.
- Expanding the persistent policy with task-specific instructions.
- Changing policy wording without checking `tests/image-contract.sh`.
- Setting `GOOSE_MODE` to `approve` or `smart_approve`, or presenting a
  confirmation prompt as the safety control for this image.
- Treating absent Context7 output as evidence for an unverified external
  claim: state what could not be verified instead.
- Assuming `goose review` sees the skills projected into `~/.agents/skills`,
  or writing `.agents/` files into a repository under review to make it.
- Inlining skill bodies into `--instructions`, or reaching for `--prompt`
  when the intent is to add context rather than replace the review prompt.
- Projecting a skill without the `references/` documents its body links to.

## Verification

```bash
echo "$GOOSE_PATH_ROOT"                 # /opt/bluefin/goose
ls "$GOOSE_PATH_ROOT"                   # config survives Hive startup
ls /home/dev/.agents/skills             # generated global skills
python3 -c "import json; json.load(open('docs/skills/index.json'))"
wc -l AGENTS.md docs/skills/*.md        # each under 200
bash scripts/check-skill-frontmatter.sh
bash tests/image-contract.sh
bash tests/hive-compatibility.sh
```
