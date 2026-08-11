# review
enslaving the oppressors since 2026

**TLDR**: Automated review-appliance container designed to put the clankers to work. They're not going away, let's put them to work. Powered by [Kubestellar Hive's Contributor Relay](https://hive.kubestellar.io/) (ClankR). We did not make that up, real dads made these jokes.

![img](https://github.com/user-attachments/assets/6b8425b8-dedf-4dc9-aa54-60fa9e6cfd91)

`review` comes with goose prebundled and will passthrough client creds. PRs accepted for other clients, the design supports doing local side containers - but we don't want to ship a huge container either.

**These are NOT anonymous "donations"** - it's tied to the person's github account, reputation in the queue is based on your real life reputation in the project. The cream will rise to the top.

The Bluefin Hive will send these agents work and coordinate - which will dole out work based on your standing in the project. New contributors will be given easier tasks until they level up, and maintainers are given more important tasks. Everything in here is `clanker-queue` only, the `human-queue` is not managed here.

It owns the contributor image, credential handoff, and review context. Hive
owns the contributor protocol, task selection, the `contributor` tmux
session, prompt injection, and output capture.

## What this is for

This is toil reduction for open-source maintainers. The projects we care about
are the under-maintained ones: a widely used library, one tired maintainer, a
two-year backlog, and CI that has been red since a dependency moved. Those
projects need basic work done reliably — broken builds, stale pins, drifted
docs, unreproduced bug reports, untriaged issues.

We are not building features with this. Big projects restrict large
AI-authored pull requests because they cost maintainers more attention than
they return, and they are right to. This is designed as the opposite of that
firehose: small, scoped, evidenced changes that repair what is already broken.
Unglamorous work is the product here, not the consolation prize. See
[`docs/skills/contribution-culture.md`](docs/skills/contribution-culture.md).

## Operating model

`review` participates in the **Bluefin Agentic Factory Feedback Loop**. Its
canonical local model is [`docs/factory/agentic-model.md`](docs/factory/agentic-model.md):
Hive dispatches work to Factory Workers, maintainers retain review and merge
authority, and the MCP app presents read-only Review Evidence. The
documentation, launcher, image, and tests describe one model; none may
silently create a second workflow, authority path, or task queue.

## Roadmap

`review` is evolving into a two-mode review appliance:

1. **Headless queue worker** (`review-container`): ingests
   Hive tasks, donates inference, and produces structured pre-review
   feedback. `REVIEW_DETACH=1` runs it detached, with `just review-stop` as
   its lifecycle verb.
2. **Interactive maintainer dashboard** (`review-queue`): the review surface.
   Goose reviews a pull request in place and streams its verdict, alongside
   high-velocity triage with batching, agent-assisted
   documentation updates (#134), and Ghost Cluster build dispatch (#133).

The watcher feedback loop — comparing automated pre-reviews with maintainer
decisions and feeding the lessons back into `projectbluefin/common` — is
tracked as #135 and projectbluefin/common#972. The legacy QEMU VM mode has
been removed; the contributor container is the only runtime.

## Reporting upstream

Running a downstream consumer of Hive's contributor protocol means we find
things upstream cannot see from inside. Reporting that evidence to
[`kubestellar/hive`](https://github.com/kubestellar/hive), and following up on
what we file, is part of the job. We report observations, reproductions, and
options with tradeoffs; upstream owns the design decision and its own triage.
We do not add a local workaround for an accepted upstream gap, because a
downstream workaround becomes upstream's compatibility burden later. See
[`docs/skills/upstream-hive.md`](docs/skills/upstream-hive.md).

## Scope

The root `justfile` is the public launcher surface for this repository.
Run `just review-container`, `just review-queue`, and `just review-doctor`
from the repository root. If you ship it in a custom image, keep those same
recipes available through the installed root Justfile.

## Installing this into your own setup

NOTE: WIP - you want to run this in projectbluefin/common: the container AUTOMOUNTS the repo's agentic skills in the container so that the project context is given to every client. This is important because this let's us make more things deterministic. The more docs and scripts we can put in this thing the easier it is for less capable models to do this work. Local models are VIABLE!

For a checkout, run the recipes directly:

```bash
just --list
just review-container
```

## Commands

`justfile` is the installable artifact and exposes exactly
four public recipes:

| Command | Purpose |
|---|---|
| `just review-container [profile] [effort]` | Run the Hive queue worker: the contributor container that receives assigned tasks and donates inference. `REVIEW_DETACH=1` runs it as a detached background worker. |
| `just review-stop [name]` | Stop a detached worker. Refuses attended runs and containers this launcher did not start. |
| `just review-queue [profile] [effort] [flags…]` | Walk the Bluefin PR queue interactively in the contributor container. |
| `just review-doctor` | Perform read-only launch diagnostics. |

Interactive runs remain attached to their originating terminal, and Ctrl-C or
closing that terminal stops them. A detached worker (`REVIEW_DETACH=1`) is
the deliberate exception: it carries a `review.owner=detached` label, logs
through `podman logs -f`, is never silently reclaimed by a later launch, and
stops only through `just review-stop`.
Detaching tmux (`prefix`, then `d`) detaches the view only—the originating
terminal remains responsible for the run.

## Public PR queue

The generated public PR queue is a small, static review backlog:

- `/` serves the Markdown overview;
- `/queue.md` serves the Markdown artifact;
- `/queue.json` serves the machine-readable artifact.

Every artifact carries `generated_at`. Treat it as a recommendation snapshot:
check its freshness and verify the selected pull request directly in GitHub
before acting. The queue's actions are `fix-ci`, `resolve-conflicts`, `review`,
`investigate`, and `ready-for-human-merge`; it never authorizes a merge.

GitHub remains authoritative for pull requests, reviews, checks, and merge
state, while Hive remains authoritative for agent coordination. The queue does
not claim work, assign agents, mutate labels, include private repositories, or
run a service. The `queue.projectbluefin.io` custom-domain and DNS mapping are
an operations task outside this repository automation.

## Requirements and credentials

- `gh auth login --web --hostname github.com --scopes repo,read:org` is a hard
  prerequisite for every recipe.
- `podman`.
- Goose configured for GitHub Copilot, or `GITHUB_COPILOT_TOKEN`.
- For contributor Git operations, a separate GitHub token via
  `REVIEW_GH_TOKEN`.

Goose is the only agent backend and GitHub Copilot is the only supported
provider. `GOOSE_PROVIDER` may be unset or `github_copilot`; `GOOSE_MODEL`
optionally overrides the `gpt-5.6-luna` default, and
`GOOSE_THINKING_EFFORT` optionally overrides the default `max` reasoning
effort. A `gh auth
token` does not authenticate Copilot inference.

`review-container` takes two optional positional arguments, a model profile
and a thinking effort:

| Invocation | Model | Effort | Context |
|---|---|---|---|
| `just review-container` | `gpt-5.6-luna` | `max` | provider default |
| `just review-container luna` | `gpt-5.6-luna` | `max` | provider default |
| `just review-container opus5 high` | `claude-opus-5` | `high` | `264000` |
| `just review-container kimi` | `kimi-k3` | `max` | `264000` |

Run it with no arguments and it launches the default profile.
Efforts are `low`, `medium`, `high`, and `max`. Contributor runs are
automated once Hive starts feeding them work, so `opus5` and `kimi` clamp
`GOOSE_CONTEXT_LIMIT` rather than paying for a window nobody reads.
`GOOSE_MODEL`, `GOOSE_THINKING_EFFORT`, and `GOOSE_CONTEXT_LIMIT` from the
environment still win over any profile.

One contributor container owns the name `review-container`, and a second
launch under that name is refused rather than replacing a live session. To run
two agents at once, give the second one its own name:

```bash
REVIEW_CONTAINER_NAME=review-container-2 just review-container opus5 high
```

Each instance is guarded, reclaimed, and attached under its own name, so
Ctrl-C in one terminal stops only that agent. The name must match podman's own
rule, `[a-zA-Z0-9][a-zA-Z0-9_.-]*`; anything else is rejected before launch.
Both instances mount the same Hive contributor credentials and are fed
independent assignments by Hive, which remains the sole authority for task
selection.

The container recipe inherits the Copilot and GitHub tokens by environment
variable name, so token values are not placed on Podman's command line. The
agent can use every scope on its GitHub token; prefer a
`REVIEW_GH_TOKEN` limited to `public_repo` or `repo`.

At startup, the contributor image reports any unavailable common validation
commands (`bats`, `shellcheck`, `hadolint`, `systemd-analyze`, `pre-commit`,
`just`, `podman`, and `actionlint`) without blocking the assigned task. The
base ships none of the linters and no package manager to obtain one, tracked
as
[fsdk-containers#89](https://github.com/projectbluefin/fsdk-containers/issues/89).

Run `just review-doctor` to check launch readiness, including
local tools, contributor image, Hive
setup, and credentials. It never starts a container. A normal attended
launch runs Hive's upstream setup when `~/.config/hive/contributor.env` is
absent; doctor only reports that condition.

## Reviewing

For a Bluefin review, run `bluefin-review [range]` from a shell inside the
assigned repository. It is an executable on `PATH`, not a Goose slash command:
the image installs `image/bin/bluefin-review` at
`/usr/local/bin/bluefin-review`, and it prints a short
`HUMAN DECISION REQUIRED` banner before handing its arguments to Goose's
native `goose review`. To inspect earlier output, enter tmux copy-mode with
`Ctrl-b [`;
PageUp scrolls, tmux search finds text, and `q` returns to the live pane.
The mouse wheel also enters copy-mode and scrolls long output. Copy-mode only
changes your view; Hive still owns task and output handling.

### Reviewing the queue

`just review-queue` runs the dashboard in the contributor container — no Hive
registration. It needs only a GitHub token (the dashboard reads live
pull-request state) and, for `r`, the same Copilot credential the other launch
paths pass through. Arguments pass straight through to the dashboard:

```bash
just review-queue                      # the whole queue, merge-ready first
just review-queue kimi high            # pick the model profile and effort
just review-queue --repo bluefin       # one repository
just review-queue --action review      # one recommended action
```

The default is the **whole** queue. It used to default to the `review` action
alone, which is a handful of the hundred-plus open pull requests, so the
dashboard looked nearly empty and the merge-ready work was not on screen at
all. Stops are ordered for a maintainer — `ready-for-human-merge` first, then
`review`, then the ones waiting on their author (`resolve-conflicts`,
`fix-ci`) and on better evidence (`investigate`). `f` cycles the filter
through each action and back to everything, and the status bar always shows
how many stops are hidden and why.

### The dashboard

`just review-queue` opens the maintainer surface: a full-screen Textual app
with a queue pane (Renovate-style dependency bumps are marked BATCHABLE), a
live-evidence details pane, and a context pane carrying the duplicate
verdicts. The status bar reports queue depth, snapshot freshness, and your
GitHub identity; your own pull requests are filtered out.

`r` is the one that matters: it opens a full-screen review that streams
Goose's output live and then states its own outcome. `x` stops a running
review — the whole process group, escalating to `SIGKILL`, because a review is
a shell running Goose running a subprocess per check, and signalling only the
shell leaves those alive. `escape` closes the screen once it has finished. A review that finished
reports COMPLETE. A review whose checks returned no verdict — the model
answered with prose, or an empty response, and `goose review` exited 0 with a
finding count anyway — reports **INCOMPLETE** and says the count is not a
clean bill of health. Those two must never look alike, so they are the
regression `tests/dashboard_pilot.py` drives the real app to prove.

| Key | Action |
|---|---|
| `b` | toggle batch selection for the highlighted pull request |
| `r` | **start a review with Goose** — streams live, reports COMPLETE / INCOMPLETE / FAILED |
| `L` | leave a review on GitHub: approve, request changes, or comment (also from the review screen) |
| `d` | docs-update agent task (tracked as #134) |
| `g` | Ghost Cluster build dispatch (tracked as #133) |
| `o` | open in browser |
| `v` | view the diff — full screen, coloured, scrollable |
| `c` | comment |
| `a` | approve and queue: approval + `lgtm`, opting in to Hive auto-merge; for the batch selection if one exists |
| `m` | merge now: squash immediately, no `lgtm`, maintainers only — the batch selection if one exists |
| `x` | reject: comment, then close |
| `h` | handoff: copy the pull request's context to your clipboard (OSC 52) |
| `/` | steer: type instructions that ride along with the review you start |
| `f` | cycle the action filter (every action → one at a time → back) |
| `R` | refresh the queue snapshot and re-ask Hive (keeps your batch) |
| `u` | update the branch from its base — the batch selection if one exists |
| `H` | ask Hive: hub state, and who is working on what right now |
| `M` | resolve the duplicate cluster |
| `q` | quit |

The dashboard is the only surface that can change anything: every mutation
prints the exact `gh` command and runs only after you type
the pull request number, a maintainer can merge directly with `m` or hand the
pull request to Hive's governor sweep with `a` (approval + `lgtm`), drafts are
refused, and every
action
is appended as a JSON trace to `~/.local/state/bluefin-review/trace.jsonl`
inside the container for the review feedback loop.
`tests/dashboard-contract.sh` pins all of it.

The leading arguments are the same model profiles `review-container` takes
(`luna`, `opus5`, `kimi` plus an optional effort); everything from the first
flag onward passes straight through to the dashboard. `REVIEW_QUEUE_NAME=review-queue-2 just review-queue`
runs a second dashboard beside the first, like `REVIEW_CONTAINER_NAME` does for
`review-container`.

Pull requests you authored are skipped — the dashboard is for reviewing other
people's work. Authorship never changes, so the snapshot's `author` field is
the one value the filter trusts without a live re-read.

The container also fetches the Hive knowledge base through Hive's own
entrypoint hook chain, and leaves it at `~/agent.md` as a file the agent can
search. It is deliberately *not* linked to `AGENTS.md`/`.goosehints`: Goose
loads those into every subprocess it starts, `goose review` starts one per
check, and the live export is ~417 KB — enough to spend a large fraction of
each check's context window before the diff is read. The review scope names
the path instead.

Each selection shows read-only Review Evidence — author, draft state, review
decision, mergeability, size, and check totals — read live from GitHub rather
than from the snapshot, because a stale "clean" reading is the one most likely
to mislead a reviewer.

Everything that names a pull request, an issue, or a person is a terminal
hyperlink (OSC 8): queue rows, the evidence header, linked issues, duplicate
and overlap references, the author, the Hive contributor working on a stop,
and both screen headers. Ctrl-click or click them in any terminal that
supports the sequence — the same capability `h` already relies on for the
clipboard.

Stops that are no longer open on GitHub are refused at the point of action —
the snapshot can be hours old, and GitHub is the state.

### Reading the queue at a glance

Rows are coloured from the state the snapshot already carries, so a hundred
stops are scannable instead of linear:

| colour | meaning | what it usually needs |
|---|---|---|
| **bold green** | merge-ready | `m`, or `a` to let the sweep do it |
| cyan | reviewable, or already approved | `r` then `L` |
| red | conflicting | a human — `u` cannot fix a real conflict |
| yellow | checks failing | its author, or `u` if it is merely stale |
| grey | evidence incomplete | `investigate` — nobody can act yet |

Rows also carry `⚑ CONFLICTS`, `✗ CI` and `✓ approved` markers, so the state
survives a terminal without colour.

The key map is two lines at the bottom: reading actions on the first, the ones
that change something on the second. Fourteen bindings do not fit on one row
of an 80-column terminal, and a truncated key map teaches half the tool.

`R` re-reads the snapshot and re-asks Hive without losing your batch selection
— the snapshot regenerates every 15 minutes and any merge invalidates it at
once. `u` runs `gh pr update-branch`, which is the button GitHub shows on a
pull request that is behind its base, for the whole batch if one is selected.

### Merging a batch, and when one refuses

`b` selects; `m` merges the whole selection, one typed-number gate per pull
request. `a` batches the same way.

GitHub refuses merges for reasons that are usually fixable, and a batch that
stops dead on the first refusal is worse than no batch — you are several
confirmations past it by the time you read the error. A refusal is offered as
a choice instead:

```
 projectbluefin/bluefinctl#31 did not merge:
 Pull request is not mergeable: the base branch is out of date

   [1] update the branch, then merge again
   [2] approve and queue it for the sweep instead
   [3] try the merge again
   [4] leave it queued and move on

 esc keeps it in the queue
```

What is offered depends on why GitHub said no — behind the base gets the
update, blocked on review gets the sweep, a conflict gets handed to a human in
the browser — and retry and keep-it-queued are always there. Updating the
branch runs `gh pr update-branch` and the merge behind one gate, so it is one
decision like every other sequence.

Anything not fixed **stays selected and stays marked**: the row carries
`✗ DID NOT MERGE`, the status line counts them, and the batch moves on to the
next pull request rather than stopping. Putting it back in the queue is the
default, not something you have to remember to redo.

### Leaving a review

`r` starts the agent review so you can see the pull request judged; `v` shows
the diff. Neither submits anything. When you have an opinion, `L` records it
on GitHub — approve, request changes, or comment — from the dashboard or from
the review screen with the draft still on it.

That is deliberately none of the other things: it does not merge, and it does
not apply `lgtm`, so you can say "this is wrong, here is why" or "this looks
right to me" without landing the change or arming the sweep. A verdict other
than an approval must carry a reason; an empty one submits nothing.

The evidence pane shows who has already reviewed and what their word is worth:

```
 reviews  2 (1 maintainer, 1 community)
          hanthor maintainer APPROVED
          passerby community CHANGES_REQUESTED
```

Maintainer or community comes from GitHub's own author association — `OWNER`,
`MEMBER` and `COLLABORATOR` carry write access, everything else does not.
GitHub's single `reviewDecision` field cannot tell you which kind of reviewer
approved, or that three other people also looked.

### Asking Hive

The status line used to read `Hive: not consulted`, permanently — a dashboard
that never asked. It asks now, on startup and again on `H`, and reports what
the hub said: `online · 185 actionable · 2 working`, or plainly `unreachable`
or `not configured`. The hub URL is not written down here; it comes from
`HIVE_HUB`, which the image's Hive entrypoint hook owns and exports.

The part worth having is per-stop. When a contributor's *current* task is the
pull request you are looking at, the context pane says so:

```
 hive     joshyorko is working on THIS now (ct-projectbluefin/bluefin-981-…)
```

That is the one thing worth interrupting a review for — the diff on screen is
about to be stale. Otherwise it tells you nobody is on it.

Consulting Hive is strictly read-only and never fatal: an unreachable or
unauthenticated hub is reported as unreachable, not raised, and the queue
still works. Hive remains the sole authority for selecting and assigning
contributor tasks; nothing here claims, reorders, or declines one.

Every state-changing key prints the exact commands and runs them only after you
type the pull request number; empty aborts, and there is no y/yes shortcut. One
decision is one gate: an action that takes several `gh` calls — queueing
(approval + `lgtm`), rejecting (comment + close), resolving a cluster — shows
every command it will run in that one gate and then runs the whole sequence in
the background. You are never asked to confirm the same decision twice, and a
failed step stops the rest instead of asking again.
Queueing a merge with `a` is the factory's automated merge path: it posts the
exact approval Hive's governor sweep re-verifies (`Approved by @<you> for Hive
auto-merge on green CI.`) and adds the `lgtm` label the sweep scans for. The
sweep — not this tool — performs the squash merge, and it independently
enforces the self-merge ban, requires mergeable plus all checks green, and
ignores drafts.

`lgtm` is an **opt-in to that automation, not a toll on merging**. When you
have read the diff and simply want the change in, `m` merges it now: the same
typed-number gate, the same squash the sweep would perform, no label, and no
robot armed. That is a maintainer power — the dashboard asks GitHub whether
you hold the `push` permission on that repository and refuses if you do not,
which is why it can never become a contributor agent's path (agents work from
forks and have no such permission). It never overrides branch protection:
there is no bypass flag, so a repository that requires review or green checks
still refuses, and you are told why.

`h` is read-only: it copies the handoff text through OSC 52, which reaches
your system clipboard when the attached terminal supports that sequence
(modern terminals and tmux's default `set-clipboard` do); otherwise the
dashboard reports that it could not reach the clipboard.

### Steering a review

The box along the bottom steers the next review of the highlighted pull
request. `/` focuses it, Esc returns to the queue — the queue keeps the
single-key bindings, because a focused input would swallow them. Enter starts
the review with what you typed carried as `BLUEFIN_REVIEW_STEER`, which
`bluefin-review` adds to the review as one more check
(`maintainer-steering`) alongside the Bluefin doctrine. It is additive by
construction: steering directs attention within a review and can never
replace the doctrine or license an approval, a merge, or any other state
change. The steer is recorded with the review's outcome in the trace.

### How busy the repository is

Each stop also shows the merge queue of its own repository, so you can tell a
repository with one thing left from one with thirty:

```
 merge queue projectbluefin/bluefin — 5 open
   ████████████████████████
   1 review · 1 CI · 1 conflicts · 2 unclear
```

Segments carry the same colours as the rows, in the order a maintainer drains
them: `queued` (already labelled `lgtm`, waiting on the sweep), `ready`,
`review`, `CI`, `conflicts`, `unclear`. First match wins — anything handed to
the sweep counts as queued whatever else is true of it, and a conflict
outranks a failing check because the check cannot mean anything until the
conflict is gone.

Every non-empty segment gets at least one cell. One pull request waiting on
the sweep behind sixty unclear ones is exactly what you need to see, and
proportional rounding is what would hide it.

The meter counts **all** open pull requests in that repository, including your
own — "how busy is this repository" is a different question from "what is left
for me to review", and only the second one excludes your own work.

### Duplicates

The queue holds real duplicates, so each stop also reports a pull request's
near-neighbours in the same repository — with enough of each to choose
between them without opening a browser tab per candidate:

```
 dupe-of  2 doing the same work — M resolves the cluster
   #26 chore(deps): update actions/checkout action to v8
      by renovate · approved, 4 files, 2026-08-05
      same dependency (actions/checkout)
   #24 chore(deps): update actions/checkout action to v7
      by renovate · draft, conflicting, 2 files, 2026-08-01
      same dependency (actions/checkout)
 overlaps 3 touching the same files (ordering hazard, not duplication)
```

Which of three duplicates to keep is the whole decision, and a bare list of
numbers said only that a decision was required. The listing this is built from
already carries the titles and states, so the summary costs nothing extra.

`dupe-of` means the two are the *same work*: Renovate raises a digest bump and a
version bump for one dependency, and several agents can close one issue from
separate pull requests. `overlaps` means only that they touch a file in common —
an ordering hazard, not duplication.

Keeping those separate is the point. Across the live queue, shared files flag
174 pairs, mostly unrelated changes touching one busy workflow file, while the
same-dependency and same-issue tests find 13. Reporting the first as duplication
would bury the second.

And because duplicates are the same work, one review judges the whole cluster:
`r` fetches every duplicate's diff alongside the checkout, and the Review Draft
must name which pull request should merge and which should close as superseded,
with evidence. Overlaps are listed in the review as merge-ordering hazards but
keep their own stops.

Detection costs one `gh pr list` per repository, cached for the session, so
revisiting a repository does not refetch it.

```bash
bluefin-review pr projectbluefin/common 42   # review one pull request
bluefin-review main...HEAD                   # review a local diff
```

`bluefin-review` is the review engine both the dashboard's `r` key and a
terminal call go through. It clones each repository once into
`HIVE_WORKSPACE_DIR` (default `~/workspace`), checks the pull request out
there, and reviews `origin/<base>...HEAD`. The result is a Review Draft for
you to judge.

It has no approve, merge, comment, or close path of its own — those belong to
the dashboard, behind the typed-number gate — so running a review can never
change anything on GitHub. Its exit status is the review's verdict about
itself: `0` complete, `65` incomplete (a check returned no verdict, so the
finding count cannot be read as clean), anything else a failure.

### Review context

`goose review` does not read `~/.agents/skills`. Its own context comes from
`.agents/REVIEW.md` and `.agents/checks/*.md` **inside the repository being
reviewed**, so reviewing another project would otherwise get Goose's generic
prompt with none of Project Bluefin's review doctrine — and writing those files
into someone else's checkout is not an option.

`bluefin-review` therefore passes `--instructions` (additive) rather than
`--prompt` (which would replace Goose's default prompt), naming the doctrine on
disk instead of inlining it:

| Source | Path |
|---|---|
| `pr-review`, `queue-feed`, `hive-review`, `human-gates` | `~/.agents/skills/<id>/SKILL.md` and `references/` |
| Hive knowledge base | `~/agent.md`, refreshed from the hub every 10 minutes |

This mirrors `local-agent-policy.md`, which routes the agent into the skill
inventory rather than repeating it. Inlining those documents would add roughly
33 KB to every check subprocess of every review; the pointer costs under 1 KB.
Entries are omitted when their files are absent, so nothing dangles.

Override with `BLUEFIN_REVIEW_CONTEXT_SKILLS` (space-separated skill ids),
`BLUEFIN_REVIEW_SKILLS_ROOT`, or `BLUEFIN_REVIEW_KNOWLEDGE_FILE`.

Hive keeps running throughout: the dashboard is an ordinary command inside the
contributor container, so the knowledge base, its refresh loop, and the
`contributor` session are untouched. Run it in a second pane with
`podman exec -it <container> tmux attach -t contributor`, or in any shell in
that container.

The menu offers no approve and no unguarded merge. This is the Managed
Reviewer Client from
[`docs/factory/agentic-model.md`](docs/factory/agentic-model.md): it prepares a
Review Draft, and the `m`/`M` action keys execute the human's decision through
a single typed-number-confirmed call site that can only arm GitHub's own
auto-merge. Approval and review submission stay
human actions taken in GitHub, and `tests/bluefin-review.sh` fails if a merge
escapes that gate or a review-submission path is ever added. Queue mode needs an interactive
terminal and says so rather than looping silently without one.

## Configuration

All configuration is read at launch.

| Variable | Purpose |
|---|---|
| `REVIEW_CONTRIBUTOR_IMAGE` | Contributor image; defaults to `ghcr.io/projectbluefin/review:stable`. |
| `REVIEW_HIVE_COMMIT` | Full Hive commit used for contributor setup. |
| `REVIEW_CONTAINER_NAME` | Contributor container name; defaults to `review-container`. Give a second concurrent instance its own name. |
| `REVIEW_GH_TOKEN` | Optional GitHub token override for container-only mode. |
| `BLUEFIN_REVIEW_QUEUE_URL` | Queue snapshot the dashboard reads; defaults to the published `queue.json`. |
| `BLUEFIN_REVIEW_CONTEXT_SKILLS` | Skill ids named as review context; defaults to `pr-review queue-feed hive-review human-gates`. |
| `BLUEFIN_REVIEW_SKILLS_ROOT` | Projected org skills root; defaults to `~/.agents/skills`. |
| `BLUEFIN_REVIEW_KNOWLEDGE_FILE` | Hive knowledge export named as review context; defaults to `~/agent.md`. |
| `GOOSE_PROVIDER` | Unset or `github_copilot`. |
| `GOOSE_MODEL` | Optional GitHub Copilot model override. |
| `GOOSE_THINKING_EFFORT` | Optional Copilot reasoning-effort override. |
| `GITHUB_COPILOT_TOKEN` | Optional Copilot credential override. |
| `TOOL` | Agent backend selector; only `goose` is accepted. |

`~/.config/review/last-selections.env` stores launcher configuration
state such as the last Goose/provider selection between runs.

`~/.local/state/review/` stores the pinned Hive checkout.
It is the only state this launcher owns. Goose and provider
selection is recomputed from the environment at every launch and never written
to disk. `~/.config/hive/contributor.env` is host state that `review` reads but
does not own; Hive's upstream setup creates it and owns its format. No other
launcher state persists.

The image's controlled Goose configuration sets `GOOSE_MODE: auto`, so the
agent runs its tools without a per-tool confirmation prompt. This is required,
not a convenience: Hive drives the CLI by simulated keystrokes, so a
confirmation prompt blocks the agent and the human at the terminal
indefinitely. The compensating control is credential scope — the agent holds a
contributor GitHub token and runs unprivileged inside a disposable container,
so its blast radius is that container plus whatever that token can reach.
Prefer a `REVIEW_GH_TOKEN` limited to `public_repo` or `repo`.

`stable` is the default contributor-image tag and is pulled at each launch.
Use an immutable `sha-<commit>` tag or digest with
`REVIEW_CONTRIBUTOR_IMAGE` when a reproducible image is required.

## Image and context

The image derives from the digest-pinned Project Bluefin FSDK lab runner and
layers the pinned Hive runtime at `98781c252cefb2f2193832a701abd8d0728ea18b`,
the current Goose canary snapshot, GitHub CLI, tmux, uv with the Textual
dashboard runtime, hooks, generated
organization skills, and the pinned `projectbluefin/lab` skills (projected as
`lab-<id>` so the Ghost Cluster operating knowledge rides along). Goose
publishes that snapshot from its active `main`
branch; each archive is verified against GitHub's signed build provenance
before installation.

The relay uses the root `package-lock.json` to install only the exact `ws`
dependency with `npm ci --omit=dev --ignore-scripts`. The official,
checksum-verified Node archive remains intact as a JavaScript runtime: `node`,
`npm`, and `corepack` stay available. Only its headers, documentation, and the
now-unused npm download cache are removed. Those fixed Node, CLI, tmux, and
relay inputs are built before the mutable Goose refresh layer, so a Goose-only
refresh reuses them.

That Hive SHA is load-bearing, not decorative. It is the third of three copies
of the same pin: `hive_commit` in the `justfile`, `ARG HIVE_COMMIT` in
`image/Containerfile`, and the one above. All three must move together, and CI
fails if they disagree. Renovate proposes them as a single change, so take its
pull request whole rather than editing any copy by hand.

Goose's `canary` name is mutable, so it is not an artifact identity. CI
resolves the official `unknown-linux-musl` archive digest for each architecture
immediately before building; the image checks that digest and Goose's signed
attestation, then records both digests in its configuration and build
provenance. A moved canary archive therefore fails the build rather than
silently changing an image. Use an immutable contributor image digest or
`sha-<commit>` image tag when a fixed artifact is required.

Every published contributor digest carries review-specific OCI title,
description, project URL/source, revision, version, creation time, license,
and exact FSDK base name/digest metadata in both platform labels and manifest
annotations. Publishing produces maximal BuildKit provenance and an SBOM, then
adds a GitHub artifact attestation for that exact digest. CI verifies the FSDK
input's GitHub attestation and its linux/amd64+linux/arm64 manifest before a
build; after publication it verifies both review attestations, labels,
annotations, subject digest, and exactly those two platforms.

Before `:stable` advances, `publish-compat-image.yml` runs the native
`arm64-runtime` job on GitHub's `ubuntu-24.04-arm` runner. It proves the host,
Docker engine, and container architecture, builds an immutable arm64 smoke
image from the same resolved Goose identities as the final publish, and runs
the existing base and derived image audits. The generated arm64 audit report in
the GitHub Actions step summary is the native acceptance artifact; local amd64
validation cannot supply that evidence.

The pinned Hive runtime preserves an existing `~/.config/goose/config.yaml`.
The image still uses `GOOSE_PATH_ROOT=/opt/bluefin/goose` to keep controlled
Goose policy, data, and state separate from Hive's runtime-owned config. Hive
now links its refreshed knowledge export to Goose-native `AGENTS.md` and
`.goosehints`, so no filename compatibility override is needed.

Organization skills are generated at image build time from
`projectbluefin/common`'s `docs/skills/index.json` into Goose's global skill
directory. Repositories may route agents to their own skill catalog, but
per-repository skills are not automatically discovered at session startup.

The base image ships the full ncurses terminfo database, so the caller's
terminal type is the truth inside the container. tmux panes run
`tmux-direct`, so 24-bit color is a terminfo fact and tmux passes RGB
through to terminals that support it, downsampling only for weaker attach
clients. For a terminal newer than the base's ncurses (e.g.
`xterm-ghostty`), the entrypoint falls back to `xterm-direct` for a
truecolor caller (`COLORTERM`) and to `xterm-256color` otherwise.

The pinned FSDK base ships GNU findutils 4.10.0 and diffutils 3.12, so the
image uses those directly. It previously installed Python `find` and `cmp`
shims into `/usr/local/bin`, which precedes `/usr/sbin` on `PATH` and so
shadowed the real tools; the `find` shim also got `-o` precedence wrong and
deleted `*.out` of any age where GNU `find` deleted only old `*.html`,
destroying fresh agent output. Both shims are gone. Use the tools the image
ships; if one is missing, fix it at the FSDK seam rather than reimplementing
it here.

Context7 serves the agent through two seams: the Hive hub queries it
server-side and folds the result into its knowledge export, and the image's
controlled Goose config enables the `context7` extension against the keyless
public endpoint for on-demand documentation lookups. The agent policy routes
external API questions through it before memory.

`worktree-guard` (at `/usr/local/bin/worktree-guard`) runs an agent command
in an ephemeral git worktree and enforces hygiene: a run that leaves the
tree dirty is reported, purged, and failed, and the agent's own exit code is
never masked. When bubblewrap is available it adds a read-only-root sandbox
around the run (fsdk-containers#109 tracks shipping bwrap in the base).

Git hooks at `/opt/bluefin/git-hooks` are ergonomics only; GitHub rulesets and
required checks enforce repository policy.

## Development

### Iterating on the contributor image

Prototype image-owned behavior in this checkout, then build the commit you
have under the same immutable tag CI mints for it:

```bash
ref="ghcr.io/projectbluefin/review:sha-$(git rev-parse HEAD)"
GH_TOKEN="$(gh auth token)" podman build \
  --secret id=github_token,env=GH_TOKEN \
  --build-arg GOOSE_REFRESH="$(date +%s)" \
  -f image/Containerfile -t "$ref" .
```

Use that tag for a container-only trial without publishing it:

```bash
REVIEW_CONTRIBUTOR_IMAGE="$ref" just review-container
```

An `sha-<commit>` tag names exactly one build, so the launcher never re-pulls
over it and the local copy is the one that runs. It also says which commit is
in the image, which a made-up local name cannot.
After the change is ready, commit it and use the normal publish workflow; CI
publishes immutable `sha-<commit>` and version tags and advances `:stable`
from `main`.
The build secret exists only while GitHub CLI verifies Goose's signed
provenance and is never included in an image layer. The checked-in checksums
make this local command use the known canary snapshot; to refresh it, resolve
the two official release-asset digests and pass
`GOOSE_X86_64_SHA256` and `GOOSE_AARCH64_SHA256` as build arguments.

### Validation

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/hive-compatibility.sh
bash tests/bluefin-review.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
```

`tests/image-audit.sh` inspects a real image, so it needs a container engine
and network access. It defaults to `docker`; on a podman host set
`CONTAINER_ENGINE=podman`. Use `--verify-base-evidence` to check the pinned
FSDK input alone, or `--derived <image>` to audit a build. The report records
each platform's runtime evidence as native or unavailable — never QEMU —
and `--report image-audit-report.md` writes it to a git-ignored file. Native
arm64 acceptance comes from the `arm64-runtime` job in
`.github/workflows/publish-compat-image.yml` and its generated step summary:

```bash
CONTAINER_ENGINE=podman bash tests/image-audit.sh \
  --derived "ghcr.io/projectbluefin/review:sha-$(git rev-parse HEAD)"
```

`pre-commit run --all-files` runs socket-free hygiene checks locally.
ShellCheck remains required in CI, where the validate workflow invokes its
manual container-backed hook explicitly.

See [`AGENTS.md`](AGENTS.md) for contributor boundaries and
[`docs/SKILL.md`](docs/SKILL.md) for task-specific documentation.

## License

Licensed under the [Apache License 2.0](LICENSE).
