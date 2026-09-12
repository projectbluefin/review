# The review appliance

`ghcr.io/projectbluefin/review` is one container that reviews Project Bluefin
pull requests. It has no host dependencies beyond a container engine, and it is
the same on your laptop, a maintainer's workstation, and a cluster node.

```bash
podman run --rm -it \
  --userns keep-id:uid=65532,gid=65532 \
  --volume bluefin-review-home:/home/bluefin \
  --volume "$PWD:/workspace:rw,z" \
  --env GH_TOKEN \
  ghcr.io/projectbluefin/review:stable
```

or, from a checkout, `just review-appliance`.

## What is inside, and why

The base is `ghcr.io/projectbluefin/base` — Project Bluefin's distroless FSDK
image: glibc, CA certificates, tzdata, and the full terminfo database including
`xterm-ghostty`. No shell, no package manager, no distro userland.

On top of it sit exactly four fetched artifacts and one staged closure:

| Component | Why it is here |
| --- | --- |
| `omp` | The agent. A single Bun executable with its own embedded runtime; the image's entrypoint. |
| `pi` | The upstream coding-agent CLI, available for direct use. |
| `node` | Present only to execute `pi`. `omp` does not use it. |
| `gh` | The appliance reviews, approves and merges through it. |
| `bash`, `git`, and eleven utilities | The shell `omp`'s `bash` tool spawns, and what a shell one-liner assumes exists. |

Every fetched artifact is verified against a SHA-256 recorded in the
Containerfile before it is allowed to become executable, and the two FSDK images
are pinned as tag *and* digest — the digest is what builds, the tag is what
Renovate can compare against.

### The one deviation from distroless

A shell is present, deliberately. `omp`'s `bash` tool spawns one, and an agent
that cannot run `gh pr checks` is not a review appliance. FSDK's own container
standard treats a shell as the named exception rather than a contradiction; this
image keeps that exception down to one binary and a dozen small utilities
(`grep`, `sed`, `gawk`, `find`, `xargs`, `tar`, `gzip`, `diff`, `less`, `curl`)
instead of a userland. Nothing inside can install anything: there is no `dnf`,
`apt`, `apk`, `pip`, or `npm`, and `tests/appliance-contract.sh` fails the build
if one appears.

### What is deliberately absent

`ssh` — the appliance talks to GitHub over HTTPS with a token. `python` — the
Textual dashboard and the Hive contributor worker live in the separate
`ghcr.io/projectbluefin/review-contributor` image, which needs a whole userland
and is not this. `strip` — stripping `omp` produces a binary that still runs and
silently reports Bun's version instead of its own, which is worse than the 8 MiB
it saves.

## Versioning

FSDK's scheme with one component added: `<fsdk-series>.<tool-revision>`.

```
ghcr.io/projectbluefin/base:26.08.0   +   image/appliance/REVISION = 3   ->   26.08.03
```

The series is never written down twice. `scripts/review-appliance-version.sh`
reads it from the pinned base in `image/appliance/Containerfile`, so rebasing
onto a new freedesktop-sdk release moves the appliance version with it and cannot
drift from what actually shipped. The only hand-maintained number is the
revision, bumped when the appliance changes on an unchanged base.

Published tags:

| Tag | Meaning |
| --- | --- |
| `26.08.03` | Immutable. The publish workflow refuses to overwrite an existing one. |
| `stable` | Moving alias for the newest published build. |
| `sha-<commit>` | Immutable, published for every build including branches. |

## Running it

State lives under `/home/bluefin`: sessions, logs, caches, the model credential,
and the review receipts the trace pane reads. **Mount a named volume there or
every run starts blank** — including the provider sign-in below. `/workspace` is
the working directory; mount the repository you are reviewing, or nothing if you
are only working through the GitHub API.

### First run signs in

A fresh container has no model credential, so the first launch opens omp's own
five-step setup and asks which provider to sign in with. That is one time per
volume, not per run: the credential is written under `/home/bluefin` and the next
launch goes straight to the queue. Skip the wizard entirely by passing a key the
provider accepts from the environment:

```bash
podman run --rm -it \
  --userns keep-id:uid=65532,gid=65532 \
  --volume bluefin-review-home:/home/bluefin \
  --env GH_TOKEN --env ANTHROPIC_API_KEY \
  ghcr.io/projectbluefin/review:stable
```

`just review-appliance` passes `GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`,
`GITHUB_COPILOT_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `HIVE_HUB`
through by name, and resolves `GH_TOKEN` from `gh auth token` when it is unset.

### The agents it carries

`bluefin-doctrine` and `bluefin-ci-triage` ship inside the image under
`/usr/share/bluefin/review/extension/agents` and are discovered by omp as task
agents. Both pin `github-copilot/gemini-3.8-flash` — triage at the default
effort, doctrine review at high — because they run on machines that have no
project configuration to resolve a role alias against.

### Hive decides the order

With `HIVE_HUB` set — or a registration mounted at
`$HOME/.config/hive/contributor.env` — the queue is ordered by Hive's own work
queue and triage view, in Hive's positions. The appliance only reads: it never
assigns, completes, or reprioritizes anything, because that is Hive's job and
the maintainer's. Without a hub the queue is classified from live GitHub
evidence instead, using the same action vocabulary as the Textual dashboard.
The header always names which one ran.

Hive's queued work is in the queue whether or not a GitHub search would have
found it. The search covers what is recent; anything Hive ranked is then
fetched by name and added, because a backlog is rarely the most recently
updated thing in an organization. The header counts it as `N/M queued`, so a
queue that is short because Hive's work could not be resolved on GitHub — a
closed item, a repository the token cannot read — is visibly different from a
queue that is short because Hive has little to do.

On an issue, `s` means ship it: implement what the issue asks, run the smallest
test covering the change, and open a pull request that closes it. Review and
merge stay with a human, and an issue that cannot be finished gets an evidenced
finding instead of a pull request.

### Working the backlog down

The loop is narrow, select, dispatch, and it is three keys:

1. `L` steps the queue through Hive's own triage stages — `triaging`, `ready`,
   `implementing`, `reviewing`, `closed` — and back to all of it. `/` narrows
   further by title, repository, author, label or number.
2. `A` selects every row the filters left on screen, up to 25. Pressing it on a
   fully selected slice clears it.
3. `s` dispatches the slice. Issues become one pull request each; pull requests
   get the landing pass.

A dispatched slice is worked **concurrently** — one agent per item, in a single
wave, not one item per turn — and every item reports its own outcome, so a batch
that half failed cannot report as a success. Twenty-five is the ceiling because
the wave is real concurrency, not a longer list.

The detail pane names the contributor whose worker holds an item right now, from
Hive's live contributor state. Two people burning the same queue down do not
need to negotiate; they can see what is already taken.

### Issue implementation admission

Queue-derived issue implementation in `projectbluefin/review` is gated on fresh,
explicit GitHub admission: dispatching an implementation action (`slay`, `fix`, `docs`)
requires a fresh GraphQL read confirming the exact `3-clanker-queue` label and the
absence of `hold` and `blocked`. Any closed, unadmitted, held, blocked, unreadable,
or incompletely read issue refuses dispatch for the entire batch. Direct human
instructions outside the queue workflow remain a separate authority path; browsing
unadmitted backlog issues remains available.

Credentials are inherited by name (`--env GH_TOKEN`), never passed as arguments
and never baked into a layer. The mode resolves a token from `GH_TOKEN`,
`GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`, or `gh auth token` in that order.

Arguments reach `omp` directly, so the mode's flags work as documented:

```bash
podman run --rm -it … ghcr.io/projectbluefin/review:stable --pr 1284
podman run --rm -it … ghcr.io/projectbluefin/review:stable --issues
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
