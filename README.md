# Bluefin Review

> Enslaving the oppressors since 2026

**Put the clankers to work. SETI@Home for Agents**
We use this to put dinosaurs in linux. See instructions below:

## Workflow

We are working towards:

```
$ bluefin contribute                          # Work on anything the project needs
$ bluefin contribute projectbluefin/server    # Use that isolated Hive registration.
$ bluefin review                              # Review a PR as a maintainer.
$ bluefin review projectbluefin/server        # Review one component
```

Review gates required +2 reviews to merge, so a maintainer running both can never self-loop and merge. Ideally 2 or more maintainers run both concurrently to implement and review each other's work. The [Bluefin Hive](https://hive.projectbluefin.io) coordinates work and ensure each agent is given appropriate work. WORKS AWESOME WITH LOCAL MODELS. TRY IT.

What we have now:

## Installation

Install `bluefin-contributor-tools` in one command from the [Universal Blue experimental tap](https://github.com/ublue-os/homebrew-experimental-tap), which automatically trusts the formula:

```bash
brew install ublue-os/experimental-tap/bluefin-contributor-tools
bluefin-contribute
```

This installs the `bluefin` CLI with both `review` and `contribute` subcommands (as well as `bluefin-contribute`):

Maintainers:
```bash
# Review pull requests and inspect CI failures
bluefin review
```

> **Note:** `bluefin` prefers rootless Podman with the `krun` OCI runtime. If Podman, `krun`, or `/dev/kvm` is unavailable, it reports why and falls back to isolated Apptainer execution; the Linux Homebrew formula declares Apptainer as a dependency.

[Installation](#installation) · [Quick start](#quick-start) · [Using the OMP workbench](#using-the-omp-workbench) · [Run a worker](#run-a-worker) · [Guides](#guides)

## Quick start

You need **Linux and GitHub CLI (`gh`)**. For hardware isolation, install
rootless Podman with the `krun` runtime and grant read/write access to
`/dev/kvm`. Otherwise the launcher reports the unavailable KVM prerequisite and
uses Apptainer, which the Linux Homebrew formula installs as a dependency.
From a checkout, `just review-appliance` opens the same image;
`just review-doctor` reports which runtime will be used without starting it.

### 1. Get the launcher and sign in to GitHub

```bash
git clone https://github.com/projectbluefin/review.git
cd review
gh auth login --web --hostname github.com --scopes repo,read:org,workflow
```

### 2. Open the OMP workbench

`review-queue` is a convenience name for the same distroless OMP appliance as
`review-appliance`; there is no alternate maintainer UI:

```bash
just review-queue                 # the whole organization queue
just review-queue owner/repo      # review one repository
just review-queue --pr 1284       # preselect one pull request
just review-queue --issues        # start on issues
```

`ghcr.io/projectbluefin/review` carries the OMP review extension, `omp`, `gh`,
`git`, Python, and the review validators `actionlint`, `shellcheck`, `yq`, `jq`,
and `just`. The launcher uses `podman run --runtime=krun`, a unique container
name, and target-specific persistent state. Two repository invocations therefore
run concurrently without replacing or sharing each other's OMP sessions.


Without a checkout, the same thing is one `podman run`:

```bash
podman run --runtime=krun --rm -it --name "bluefin-review-example-$(date +%s)-$$" \
  --userns keep-id:uid=65532,gid=65532 \
  --volume bluefin-review-example-home:/home/bluefin \
  --volume bluefin-review-example-workspace:/workspace \
  --env GH_TOKEN ghcr.io/projectbluefin/review:stable
```

Its version combines the pinned FSDK series with `image/appliance/REVISION`;
every fetched artifact is pinned by digest. See the
[appliance guide](docs/appliance.md).

The same mode runs against a locally installed `omp` with `bin/omp-review`,
which takes the same shortcuts: `bin/omp-review owner/repo`, `bin/omp-review 1284`,
`bin/omp-review issues`.

**Hive orders the queue when `HIVE_HUB` is set.** The queue uses Hive's work
positions, and a pull request that closes queued work inherits that
position — so reviewing through this tool contributes to what the project
already decided matters. The mode only reads: task selection, assignment and
priority stay with Hive, and merge decisions stay with the maintainer. Without a
hub the queue is classified from live GitHub evidence using the policy layer's
actions — `ready-for-human-merge`, `review`, `resolve-conflicts`, `fix-ci`,
`investigate`, `triage` — so an unorchestrated project still opens on what it
can land instead of on whatever GitHub touched last.

The OMP extension is one permanent workbench, not a second dashboard layered
over the prompt. Its queue and live Dagger-style execution trace stay on the
same screen. The top gauge shows mode, queue position, repository, outcomes,
freshness, Hive ordering, and actionable count; the bottom gauge shows Hive
connectivity, selection count, and the active workflowz slay. It registers no
slash commands.

| Key | Action |
| --- | --- |
| `tab` | Toggle pull requests/issues and the complete cool/warm palette |
| `j` / `k` | Next / previous queue item |
| `space` | Select / deselect the focused item |
| `A` / `x` | Select the filtered slice / clear selection |
| `s` | Slay selected pull requests through review, repair, and landing |
| `alt+s` | Autoslay the selected or visible slice through the same lifecycle |
| `alt+b` | Select / clear the focused repository group |
| `f` | Fix selected items in isolated workspaces |
| `d` | Inspect bounded diff evidence |
| `p` | Pause / resume starting later repository waves |
| `r` | Refetch GitHub and Hive projections |
| `o` | Change repository or organization scope |
| `/` | Filter the visible queue |
| `H` / `L` | Toggle Hive-only rows / step through Hive stages |
| `t` | Focus the execution trace |
| `g` / `G` | Jump to the first / last row |
| `h` / `l` | Collapse / expand the focused trace span |
| `c` | Comment on the captured target after live revalidation |
| `enter` | Cite the focused item in the prompt |
| `?` | Show the in-app key guide |
| `q` / `Esc` | Close the workbench |

Slay preserves Hive order, partitions selected work by repository, and asks OMP
workflowz to run one bounded `task` batch with a fresh `bluefin-reviewer` item
per pull request. Later repository waves do not start until the prior repository
settles, avoiding cross-repository context churn. OMP owns agent execution, task
concurrency, task state, tools, sessions, and cancellation; the extension only
owns Hive's queue projection, durable intent, GitHub mutation guards, and
presentation.
The mode also ships the `bluefin-doctrine` and `bluefin-ci-triage` task agents.

### 3. Start with one repository, or browse the organization

The commands above open the whole Project Bluefin queue. To narrow it, append
a repository—for example, `just review-queue projectbluefin/review`.
`review-queue` delegates to `review-appliance`, so both commands use
`ghcr.io/projectbluefin/review:stable`, the same OMP configuration, and the same
single-screen workbench.

## Using the OMP workbench

The queue and execution trace remain visible beside the prompt. Navigate and
select work with the keys above; `s` slays the selected repository waves, and
`--autoslay` starts the visible slice immediately. The appliance exists to
review **and land** code changes. A slay is one maintainer-authorized lifecycle:
review the exact head, repair findings in isolation, review the repaired head
afresh, then approve and ask GitHub to squash-merge when its live rules permit.
Reviewer subagents remain read-only so the verdict and mutation authorities are
separate; the appliance's coordinator owns approval and landing. Autoslay also
enables OMP's advisor on the coordinator session, resolving its model through
`modelRoles.advisor` → `@default` so it follows the maintainer's selected model
without pinning a provider. The [workbench guide](docs/skills/review-dashboard.md)
documents the authority model.


## Run a worker

Hive assigns contributor work; the OMP workbench is the maintainer surface.
Both contributor convenience commands launch the same OMP worker:

```bash
just contribute
just review-container
```

Choose provider, model, and effort inside OMP. The launcher does not interpret
profiles or export `AGENT_MODEL` / `AGENT_REASONING_EFFORT`.

Each contributor invocation prefers a foreground libkrun microVM and falls back
gracefully to an isolated foreground Apptainer container:

```bash
bluefin contribute
bluefin contribute projectbluefin/server
```

An optional `org/repo` argument names the isolated instance and selects
`~/.config/hive/contributor.<org-repo>.env`; Hive still chooses and assigns the
actual work. Different instance names use different persistent OMP volumes and
unique container names. Set `BLUEFIN_INSTANCE` to split concurrent runs for the
same target.

Keep the launching terminal open. **Ctrl-C stops only that invocation.**
Detached contributor containers are unsupported (`REVIEW_DETACH=1` is rejected).

Kubernetes users can scale workers with `just contribute cluster [N]` and
stop them with `just review-stop cluster`. Start with the
[cluster guide](docs/skills/cluster-workers.md); a cluster is not required for
the OMP workbench, and opening the workbench never starts a worker.

## Guides

- **Setup and credentials:** [Launcher](docs/skills/launcher.md)
- **Review controls and evidence:** [OMP workbench](docs/skills/review-dashboard.md)
- **Worker troubleshooting:** [Hive triage](docs/skills/hive-triage.md)
- **Build and verify images:** [Image and development](docs/image-and-development.md)
- **Architecture and authority boundaries:** [Agentic model](docs/factory/agentic-model.md)
- **Contribute:** [Contribution culture](docs/skills/contribution-culture.md) · [Agent contract](AGENTS.md)
- **All documentation:** [Documentation index](docs/SKILL.md)

## What this is for

Reduce maintainer toil: broken builds, stale pins, drifted documentation, and
unreproduced reports. Planned documentation assistance is tracked in
[#134](https://github.com/projectbluefin/review/issues/134); the feedback loop
is tracked in [#135](https://github.com/projectbluefin/review/issues/135).

<details>
<summary>Image provenance</summary>

The contributor image layers the pinned Hive runtime at `bc929a202199fbed5ae43dbe50e58a98930f3bc1`; it contains no maintainer UI. `ghcr.io/projectbluefin/contribute` is the separate distroless Hive + OMP worker, and `ghcr.io/projectbluefin/review` is the maintainer-facing OMP appliance.
See [image architecture and validation](docs/image-and-development.md).

</details>

Licensed under [Apache 2.0](LICENSE). [Visual credits](docs/images/README.md).
