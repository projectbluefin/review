# The review appliance

`ghcr.io/projectbluefin/review` is one OCI image that implements selected
Project Bluefin issues and reviews, repairs, and lands selected pull requests.
The `bluefin` launcher prefers a dedicated libkrun microVM and falls back
cleanly to an isolated Apptainer container when KVM is unavailable.

```bash
podman run --runtime=krun --rm -it \
  --name "bluefin-review-example-$(date +%s)-$$" \
  --userns keep-id:uid=65532,gid=65532 \
  --volume bluefin-review-example-home:/home/bluefin \
  --volume bluefin-review-example-workspace:/workspace \
  --env GH_TOKEN \
  ghcr.io/projectbluefin/review:stable
```

or, from a checkout, `just review-appliance`.

## What is inside, and why

The base is `ghcr.io/projectbluefin/base` — Project Bluefin's distroless FSDK
image: glibc, CA certificates, tzdata, and the full terminfo database including
`xterm-ghostty`. No shell, no package manager, no distro userland.

On top of it sit exactly two fetched artifacts and one staged closure:

| Component | Why it is present |
| --- | --- |
| `omp` | The sole agent runtime and extension host. |
| `gh` | The appliance reviews, approves and merges through it. |
| `bash`, `git`, `python3`, and core shell utilities | The shell `omp` spawns and the minimal execution substrate for repository inspection. |
| `actionlint`, `shellcheck`, `yq`, `jq`, `just` | Review-time validators already shipped by the pinned FSDK builder and staged into the appliance explicitly. |

Every fetched artifact is verified against a SHA-256 recorded in the
Containerfile before it is allowed to become executable, and the two FSDK images
are pinned as tag *and* digest — the digest is what builds, the tag is what
Renovate can compare against.

### The one deviation from distroless

A shell is present, deliberately. `omp`'s `bash` tool spawns one, and an agent
that cannot run `gh pr checks` is not a review appliance. FSDK's own container
standard treats a shell as the named exception rather than a contradiction. The
appliance stages the common shell utilities plus the builder's existing
`actionlint`, `shellcheck`, `yq`, `jq`, `just`, and `openssl` binaries; it does
not install a second package set. Nothing inside can install anything: there is
no `dnf`, `apt`, `apk`, `pip`, or `npm`, and `tests/appliance-contract.sh`
fails the build if one appears.

### What is deliberately absent

`pi` and `node` — OMP is the sole agent runtime and embeds Bun. `ssh` — the
appliance talks to GitHub over HTTPS with a token. `strip` — stripping `omp`
produces a binary that still runs and silently reports Bun's version instead of
its own, which is worse than the 8 MiB it saves.
Repository-specific test frameworks and host-service tools — including Bats,
Ruby, third-party Python packages, and `systemd-analyze` — are not a coherent
distroless runtime closure. Reviewers use the bundled static validators and
live hosted-check evidence, and report any local verification gap instead of
installing packages into the appliance.

## Versioning

FSDK's scheme with one component added: `<fsdk-series>.<tool-revision>`.

```
ghcr.io/projectbluefin/base:26.08.0   +   image/appliance/REVISION = 6   ->   26.08.06
```

The series is never written down twice. `scripts/review-appliance-version.sh`
reads it from the pinned base in `image/appliance/Containerfile`, so rebasing
onto a new freedesktop-sdk release moves the appliance version with it and cannot
drift from what actually shipped. The only hand-maintained number is the
revision, bumped when the appliance changes on an unchanged base.

Published tags:

| Tag | Meaning |
| --- | --- |
| `26.08.06` | Immutable. The publish workflow refuses to overwrite an existing one. |
| `stable` | Moving alias for the newest published build. |
| `sha-<commit>` | Immutable, published for every build including branches. |

OCI tags are replaced, never updated in place. The appliance disables omp's
startup update check, and `omp update` exits with instructions to pull a newer
image. This keeps the running artifact identical to what was verified and
published.

Because `stable` is a moving alias, the `bluefin` launcher verifies the image
before it runs: every pull of a `ghcr.io/projectbluefin/*` ref is checked
against the build-provenance attestation the publish workflow pushes to the
registry (`gh attestation verify oci://<ref>@<digest> --repo projectbluefin/review`),
and a launch that fails verification refuses to run the image. Locally built
`localhost/*` images and caller-supplied override refs are the operator's own
trust decision and are not verified.

## Running it

State lives under `/home/bluefin`: OMP sessions, logs, caches, provider
credentials, and workbench slay intent. The launcher derives persistent home,
workspace, and scratch storage from the selected repository; the scratch bind
replaces Apptainer's 64 MiB `/tmp` so repository clones and archive inspection
cannot exhaust it. A unique container name lets different repository targets
run concurrently without sharing state. Set `BLUEFIN_INSTANCE` to split the
same target.

### First run signs in

A fresh container has no model credential, so the first launch opens omp's own
five-step setup and asks which provider to sign in with. That is one time per
volume, not per run: the credential is written under `/home/bluefin` and the next
launch goes straight to the queue. Skip the wizard entirely by passing a key the
provider accepts from the environment:

```bash
podman run --runtime=krun --rm -it \
  --name bluefin-review-example \
  --userns keep-id:uid=65532,gid=65532 \
  --volume bluefin-review-example-home:/home/bluefin \
  --volume bluefin-review-example-tmp:/tmp \
  --volume bluefin-review-example-workspace:/workspace \
  --env GH_TOKEN --env ANTHROPIC_API_KEY --env CONTEXT7_API_KEY \
  ghcr.io/projectbluefin/review:stable
```

`just review-appliance` passes `GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`,
`GITHUB_COPILOT_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_OAUTH_TOKEN`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `CONTEXT7_API_KEY`, and `HIVE_HUB` through by
name, resolves `GH_TOKEN` from `gh auth token` when it is unset, and resolves an
unset `HIVE_HUB` from the host's default `$HOME/.config/hive/contributor.env`
without exposing the registration token.

The appliance uses its own `bluefin-review-appliance` OMP profile. Host OMP
configuration is not mounted by default, so host MCP entries cannot make the
appliance noisy or unusable. The packaged review extension enables three
Streamable HTTP MCP servers in every appliance: GitHub at
`https://api.githubcopilot.com/mcp/`, the public read-only Project Bluefin
service at `https://mcp.projectbluefin.io/mcp`, and Context7 at
`https://mcp.context7.com/mcp`. GitHub uses `GH_TOKEN` or `GITHUB_TOKEN` when
available. Context7 works keyless and uses `CONTEXT7_API_KEY` when provided for
higher rate limits.

To deliberately provide other host configuration, mount it into the
target-specific home and set `BLUEFIN_REVIEW_INHERIT_OMP_CONFIG=1`. The
appliance never edits host configuration directly. Git HTTPS requests use the
bundled `gh auth git-credential` helper, scoped to `github.com`; credential
values remain in the inherited environment and credential protocol, not image
layers or process arguments. The immutable invocation overlay enables fresh
workflowz agents, caps task concurrency at four and recursion at one, isolates
task worktrees without auto-applying them, uses a one-hour task deadline and a
bounded request budget, keeps tool intent traces out of model context, and
selects low text verbosity. OMP resolves every model and effort choice from the
user's active configuration; the appliance and its agents impose no model
mapping or filtering.


### The agents it carries

The agent definitions under `/usr/share/bluefin/review/extension/agents` ship
with the image and are discovered by OMP directly. They cover doctrine,
correctness, security, test coverage, simplicity, CI triage, queue triage, and
coordinated review. They deliberately omit model and effort fields, leaving both
choices to the user's active OMP configuration.

### Returned work first, then Hive order

Pull requests authored by the authenticated user with requested changes form a
local repair-only lane at the top; this never changes Hive's own priorities or
assignments. With `HIVE_HUB` set — or present in the host's default
`$HOME/.config/hive/contributor.env` — the remaining queue follows Hive's work
queue and triage positions exactly. The appliance only reads Hive: it never
assigns, completes, or reprioritizes contributor work. Without a hub the queue
is classified from live GitHub evidence using the policy layer's action
vocabulary. The header always names which authority ordered the queue.

Hive's queued work is in the queue whether or not a GitHub search would have
found it. The search covers what is recent; anything Hive ranked is then
fetched by name and added, because a backlog is rarely the most recently
updated thing in an organization. The header counts it as `N/M queued`, so a
queue that is short because Hive's work could not be resolved on GitHub — a
closed item, a repository the token cannot read — is visibly different from a
queue that is short because Hive has little to do.

### Working the backlog down

The loop is select, group, and dispatch:

1. `Tab` switches pull-request and issue mode. `L` steps through Hive's own
   stages; `/` filters by title, repository, author, label, or number.
2. `Space` toggles one item, `Alt-B` selects the focused repository group, and
   `A` selects the filtered slice up to the bounded slay limit.
3. `s` applies the selected entity's lifecycle. Ordinary pull requests go
   through review, repair, fresh review, and landing. Issues read their Hive
   queue and knowledge context, run one isolated workflowz `task` item per
   issue, and finish only after a review-ready closing pull request is submitted.
   `--autoslay` and `Alt-S` first repair pull requests returned to the
   authenticated author, then process the visible issue backlog in bounded,
   type-homogeneous repository waves. Returned PR repair ends at a new pushed
   head and never self-reviews, self-approves, or self-merges.
4. OMP's advisor is enabled for every review session and resolves through
   `@default`, following the maintainer's selected model.
5. `p` pauses admission of later repository waves without pretending to suspend
   agents already running.

The appliance handles both review-and-landing work and issue implementation.
The maintainer's ordinary PR slay delegates approval and merge execution to the
coordinator. Reviewer subagents deliberately lack mutation tools: they produce
independent evidence while the coordinator revalidates the live head and
repository rules. Issue and returned-PR workers may push changes, but never
approve or merge their own pull requests.

Issue implementation in managed repositories is gated on a fresh GitHub read
of the policy layer's admission and denial labels. Any closed, unadmitted,
held, blocked, unreadable, or incompletely read issue rejects the batch before
dispatch. A completed worker job is not sufficient: the workbench verifies a
submitted closing pull request before advancing the issue wave.

Credentials are inherited by name (`--env GH_TOKEN`), never passed as arguments
and never baked into a layer. The mode resolves a token from `GH_TOKEN`,
`GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`, or `gh auth token` in that order.

Arguments reach `omp` directly, so the mode's flags work as documented:

```bash
podman run --runtime=krun --rm -it … ghcr.io/projectbluefin/review:stable --pr 1284
podman run --runtime=krun --rm -it … ghcr.io/projectbluefin/review:stable --issues
```

The image runs as uid `65532` with a matching `/etc/passwd` entry, because a
numeric-only user with no passwd record breaks `getpwuid()` — which is exactly
what a rootless runtime calls. Under Kubernetes it satisfies `runAsNonRoot` with
`runAsUser: 65532`.

## Building and verifying it

```bash
just review-appliance-build                 # build, then hold it to its contract
bash tests/appliance-contract.sh            # static half only, no engine needed
```

The contract test is in two halves. The static half reads the Containerfile and
the version machinery. The runtime half runs the image: it executes every
bundled binary, makes `git` create a real commit, checks that the HTTPS remote
helper resolved its TLS closure, proves no package manager is reachable, reads
the in-image SBOM, and holds the image under a size ceiling. That ceiling is not
decoration — the first draft of this image shipped 284 MiB of esbuild binaries
for platforms it cannot execute.

The image carries its own SPDX document at
`/usr/share/bluefin/review/sbom.spdx.json`. syft only sees package-manager
metadata, and every component here arrived as a release archive, so without it
the attested SBOM would describe an image whose load-bearing parts are invisible.
The publish workflow ingests it through syft's `sbom-cataloger` and attaches the
result as an attestation.

The packaged launcher prefers rootless Podman's `krun` OCI runtime and
read/write access to `/dev/kvm`. If any KVM prerequisite is unavailable, it
reports the reason and uses the installed Apptainer fallback.
