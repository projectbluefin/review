# review
enslaving the oppressors since 2026

**TLDR**: Automated VM/container designed to put the clankers to work. They're not going away, let's put them to work. Powered by [Kubestellar Hive's Contributor Relay](https://hive.kubestellar.io/) (ClankR). We did not make that up, real dads made these jokes.

![img](https://github.com/user-attachments/assets/6b8425b8-dedf-4dc9-aa54-60fa9e6cfd91)

`review` comes with goose prebundled and will passthrough client creds. PRs accepted for other clients, the design supports doing local side containers - but we don't want to ship a huge container either.

**These are NOT anonymous "donations"** - it's tied to the person's github account, reputation in the queue is based on your real life reputation in the project. The cream will rise to the top.

The Bluefin Hive will send these agents work and coordinate - which will dole out work based on your standing in the project. New contributors will be given easier tasks until they level up, and maintainers are given more important tasks. Everything in here is `clanker-queue` only, the `human-queue` is not managed here.

It owns only VM boot, credential handoff, and review context. Hive owns the
contributor protocol, task selection, the `contributor` tmux session, prompt
injection, and output capture.

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
Run `just review`, `just review-container`, and `just review-doctor` from the
repository root. If you ship it in a custom image, keep those same recipes
available through the installed root Justfile.

## Installing this into your own setup

NOTE: WIP - you want to run this in projectbluefin/common: the container AUTOMOUNTS the repo's agentic skills in the container so that the project context is given to every client. This is important because this let's us make more things deterministic. The more docs and scripts we can put in this thing the easier it is for less capable models to do this work. Local models are VIABLE!

For a checkout, run the recipes directly:

```bash
just --list
just review
```

## Commands

`justfile` is the installable artifact and exposes exactly
three public recipes:

| Command | Purpose |
|---|---|
| `just review` | Run the contributor through a foreground QEMU VM. |
| `just review-container [profile] [effort]` | Run the contributor container directly, without a VM. |
| `just review-doctor` | Perform read-only launch diagnostics. |

Every run remains attached to its originating terminal. Ctrl-C or closing that
terminal stops it; the launcher provides no lifecycle commands or daemon.
Detaching tmux (`prefix`, then `d`) detaches the view only—the originating
terminal remains responsible for the foreground run.

### The two launch modes are not interchangeable

`just review` and `just review-container` are two different products with
different capabilities. Read this before choosing one:

| | `just review` (VM) | `just review-container` |
|---|---|---|
| Credential channel | one-shot `0600` AF_UNIX JSON bootstrap | inherited `--env NAME` |
| Copilot `provider_secret` | yes | yes |
| GitHub identity (`GH_TOKEN`) | **no — structurally impossible** | yes |
| Fork, push, open a pull request | **no** | yes |
| Host mounts | none | `~/.config/hive`, read-only |
| Host requirements | qemu, qemu-img, UEFI firmware, python3, curl, zstd, `/dev/kvm`, podman | podman |
| First-run download | ~1.4 GB VM raw image | contributor image |

**VM mode cannot fork, push, or open a pull request.** The current guest has no
mapping from the bootstrap channel to a GitHub identity, so the launcher
reports that block unconditionally on both VM branches. Hive's task prompt is
unconditional and instructs the agent to fork, push, and open a pull request
with `GH_TOKEN`, so VM mode can be assigned work it cannot structurally
complete; Hive then books a failure cooldown on that issue. Use
`review-container` for any contributor work that must produce a pull request.
This is tracked by issue #50 and blocked on a republished VM guest artifact
this repository does not own. Do not attempt to fix it by mounting GitHub
configuration or adding an unconsumed bootstrap field, and do not filter or
decline assignments — Hive is the sole authority for task selection.

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
  prerequisite for both launch modes.
- `podman` for either launch mode.
- Readable, writable `/dev/kvm` for VM mode.
- For a local raw VM: `qemu-system-<host-arch>`, `qemu-img`, matching UEFI
  firmware, `curl`, and `zstd`.
- Goose configured for GitHub Copilot, or `GITHUB_COPILOT_TOKEN`.
- For container-only Git operations, a separate GitHub token via
  `REVIEW_GH_TOKEN`.

Goose is the only agent backend and GitHub Copilot is the only supported
provider. `GOOSE_PROVIDER` may be unset or `github_copilot`; `GOOSE_MODEL`
optionally overrides the `gpt-5.6-luna` default, and
`GOOSE_THINKING_EFFORT` optionally overrides the default `max` reasoning
effort for `review-container` (the VM default remains `high`). A `gh auth
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

The VM guest has no GitHub identity mapping; see
[The two launch modes are not interchangeable](#the-two-launch-modes-are-not-interchangeable).

Run `just review-doctor` to check the selected VM path, including
local tools, firmware, raw-artifact availability, contributor image, Hive
setup, and credentials. It never starts a VM or container. A normal attended
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

### Walking the queue

`just review-queue` runs the walk in the contributor container — no Hive
registration, no VM. It needs only a GitHub token (the walk reads live
pull-request state) and, for `r`, the same Copilot credential the other launch
paths pass through. Arguments pass straight through to `bluefin-review queue`:

```bash
just review-queue                      # everything the queue marks 'review'
just review-queue --repo bluefin       # one repository
just review-queue --all                # every action
```

`q` or Ctrl-C stops. `REVIEW_QUEUE_NAME=review-queue-2 just review-queue`
runs a second walk beside the first, like `REVIEW_CONTAINER_NAME` does for
`review-container`.

`bluefin-review queue` walks the public PR queue one pull request at a time.
Each stop prints read-only Review Evidence — author, draft state, review
decision, mergeability, size, and check totals — read live from GitHub rather
than from the snapshot, because a stale "clean" reading is the one most likely
to mislead a reviewer. Then it offers a menu:

| Key | Action |
|---|---|
| `Enter` | next pull request |
| `r` | check the pull request out and review it through `goose review` |
| `d` | show the diff |
| `o` | open it in a browser |
| `c` | leave a comment |
| `p` | previous pull request |
| `q` | stop |

### Duplicates

The queue holds real duplicates, so each stop also reports a pull request's
near-neighbours in the same repository:

```
 dupe-of  #26 (same dependency actions/checkout)
 overlaps #25, #24, #22
```

`dupe-of` means the two are the *same work*: Renovate raises a digest bump and a
version bump for one dependency, and several agents can close one issue from
separate pull requests. `overlaps` means only that they touch a file in common —
an ordering hazard, not duplication.

Keeping those separate is the point. Across the live queue, shared files flag
174 pairs, mostly unrelated changes touching one busy workflow file, while the
same-dependency and same-issue tests find 13. Reporting the first as duplication
would bury the second.

Detection costs one `gh pr list` per repository, cached for the walk, so
revisiting a repository does not refetch it.

```bash
bluefin-review queue                      # everything the queue marks 'review'
bluefin-review queue --repo common        # one repository
bluefin-review queue --action fix-ci      # a different recommended action
bluefin-review queue --all                # every action
```

`r` clones each repository once into `HIVE_WORKSPACE_DIR` (default
`~/workspace`), checks the pull request out there, and reviews
`origin/<base>...HEAD`. The result is a Review Draft for you to judge.

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

Hive keeps running throughout: the queue walk is an ordinary command inside the
contributor container, so the knowledge base, its refresh loop, and the
`contributor` session are untouched. Run it in a second pane with
`podman exec -it <container> tmux attach -t contributor`, or in any shell in
that container.

The menu deliberately offers no approve and no merge. This is the Managed
Reviewer Client from
[`docs/factory/agentic-model.md`](docs/factory/agentic-model.md): it prepares a
Review Draft and never claims review, approval, or merge authority. Those stay
human actions taken in GitHub, and `tests/bluefin-review.sh` fails if a merge
or review-submission path is ever added. Queue mode needs an interactive
terminal and says so rather than looping silently without one.

## Configuration

All configuration is read at launch.

| Variable | Purpose |
|---|---|
| `REVIEW_VM_RAW` | Verified local raw disk; its `.sha256` sidecar is required. |
| `REVIEW_VM_VERSION` | Raw-release version used when neither VM override is set. |
| `REVIEW_CONTRIBUTOR_IMAGE` | Contributor image; defaults to `ghcr.io/projectbluefin/review:stable`. |
| `REVIEW_HIVE_COMMIT` | Full Hive commit used for contributor setup. |
| `REVIEW_CONTAINER_NAME` | Contributor container name; defaults to `review-container`. Give a second concurrent instance its own name. |
| `REVIEW_GH_TOKEN` | Optional GitHub token override for container-only mode. |
| `BLUEFIN_REVIEW_QUEUE_URL` | Queue snapshot `bluefin-review queue` reads; defaults to the published `queue.json`. |
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

`~/.local/state/review/` stores the pinned Hive checkout and verified
VM artifact cache. It is the only state this launcher owns. Goose and provider
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

VM selection prefers an explicit raw disk, then an exact
version-and-architecture raw release. Raw images are checksum
verified, boot through disposable overlays, and cached by version and
architecture. Once the requested raw image is verified, older caches for that
architecture are removed.

`stable` is the default contributor-image tag and is pulled at each launch.
Use an immutable `sha-<commit>` tag or digest with
`REVIEW_CONTRIBUTOR_IMAGE` when a reproducible image is required.

## Image and context

The image derives from the digest-pinned Project Bluefin FSDK lab runner and
layers the pinned Hive runtime at `98781c252cefb2f2193832a701abd8d0728ea18b`,
the current Goose canary snapshot, GitHub CLI, tmux, hooks, and generated
organization skills. Goose publishes that snapshot from its active `main`
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

Context7 is Hive's, not this image's. The hub queries Context7 server-side and
delivers the result through its knowledge export, so the image never
configures a Context7 extension and CI forbids one.

Git hooks at `/opt/bluefin/git-hooks` are ergonomics only; GitHub rulesets and
required checks enforce repository policy.

## Development

### Iterating on the contributor image

Prototype image-owned behavior in this checkout, then build a local tag:

```bash
GH_TOKEN="$(gh auth token)" podman build \
  --secret id=github_token,env=GH_TOKEN \
  --build-arg GOOSE_REFRESH="$(date +%s)" \
  -f image/Containerfile -t localhost/review:dev .
```

Use that tag for a container-only trial without publishing it:

```bash
REVIEW_CONTRIBUTOR_IMAGE=localhost/review:dev \
  just review-container
```

The launcher prefers a fresh copy of moving tags, but falls back to an already
present local image when no registry copy is available. After the change is
ready, commit it and use the normal publish workflow; CI publishes immutable
`sha-<commit>` and version tags and advances `:stable` from `main`.
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
and `--report image-audit-report.md` writes it to a git-ignored file:

```bash
CONTAINER_ENGINE=podman bash tests/image-audit.sh --derived localhost/review:dev
```

`pre-commit run --all-files` runs socket-free hygiene checks locally.
ShellCheck remains required in CI, where the validate workflow invokes its
manual container-backed hook explicitly.

See [`AGENTS.md`](AGENTS.md) for contributor boundaries and
[`docs/SKILL.md`](docs/SKILL.md) for task-specific documentation.

## License

Licensed under the [Apache License 2.0](LICENSE).
