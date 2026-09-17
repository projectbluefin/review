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

## Product boundary

Review is a GitHub-first core. On any GitHub repository the operator can access,
pull requests and issues are first-class objects, and the workbench offers only
the actions its backend can run: PR review, diff, fix, and slay; issue
inspection, implementation, and fix. Review owns the generic core — PRs, issues,
queues, search, reading, inspection, review, repair, implementation, and landing
— subject to GitHub access, operator permissions, execution requirements, and
safety checks.

When Hive is missing or unreachable, queue order falls back to GitHub, and
review, fix, and slay remain available without Hive; the workbench is not
browse-only. OMP owns sessions, agents, execution, and traces. [Bluefin](https://projectbluefin.io)
adds its doctrine, specialized reviewers, labels, conventions, and repository
admission rules. [Hive](https://hive.projectbluefin.io) adds ordering, claims,
stages, curated knowledge, and contributor coordination through its MCP server.
Neither integration is required for the core GitHub workflows: GitHub defines
what work exists, Review defines what can be done with it, Hive may prioritize
and coordinate it, and Bluefin may specialize its policy. The boundary and its
follow-up work — including a GitHub-only mode that selects Hive or the plain
GitHub toolchain — are tracked in [#591](https://github.com/projectbluefin/review/issues/591).

## Installation

Install `bluefin-contributor-tools` in one command from the [Universal Blue experimental tap](https://github.com/ublue-os/homebrew-experimental-tap), which automatically trusts the formula:

```bash
brew install ublue-os/experimental-tap/bluefin-contributor-tools
bluefin-contribute
```

This installs the `bluefin` CLI with both `review` and `contribute` subcommands (as well as `bluefin-contribute`):

Maintainers:
```bash
# Implement issues or review and land pull requests
bluefin review
```

> **Note:** `bluefin` prefers rootless Podman with the `krun` OCI runtime. If Podman, `krun`, or `/dev/kvm` is unavailable, it reports why and falls back to isolated Apptainer execution; the Linux Homebrew formula declares Apptainer as a dependency.

[Installation](#installation) · [Quick start](#quick-start) · [Using the OMP workbench](#using-the-omp-workbench) · [Run a worker](#run-a-worker) · [Guides](#guides)

## Quick start

You need **Linux and GitHub CLI (`gh`)**. For hardware isolation, install
rootless Podman with the `krun` runtime and grant read/write access to
`/dev/kvm`. Otherwise the launcher reports the unavailable KVM prerequisite and
uses Apptainer, which the Linux Homebrew formula installs as a dependency.
Run `bluefin doctor` to check the machine without starting an agent. From a
checkout, the compatible `just review-appliance` and `just review-doctor`
developer recipes invoke the same runtime contracts.

### 1. Get the launcher and sign in to GitHub

```bash
git clone https://github.com/projectbluefin/review.git
cd review
gh auth login --web --hostname github.com --scopes repo,read:org,workflow
```

### 2. Open the OMP workbench

`bluefin review` opens the distroless OMP maintainer appliance; there is no
alternate maintainer UI:

```bash
bluefin review                    # the whole organization queue
bluefin review owner/repo         # review one repository
bluefin review owner/repo 1284    # preselect one pull request
bluefin review --issues           # start on issues
```

Checkout users may use the compatible `just review-queue` and
`just review-appliance` developer recipes. `review-queue` delegates to `review-appliance`.

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

**Hive orders queued project work when `HIVE_HUB` is set.** Pull requests
authored by the current user with requested changes form a local repair-only
lane before that work; Hive's relative order remains unchanged behind it. The
queue uses Hive's work and triage positions directly, without recomputing or
writing them. Claims, assignment, contributor completion, and Hive priority
stay with Hive; merge decisions stay with the maintainer. Without a
hub the queue is classified from live GitHub evidence using the policy layer's
actions — `repair-requested`, `ready-for-human-merge`, `review`,
`resolve-conflicts`, `fix-ci`, `investigate`, `triage` — so an unorchestrated
project still opens on work that needs the maintainer instead of whatever
GitHub touched last.

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
| `s` | Slay selected PRs through review/repair/landing, or implement selected issues through submitted PRs |
| `alt+s` | Repair returned PRs first, then implement the visible issue backlog in bounded waves |
| `alt+b` | Select / clear the focused repository group |
| `f` | Fix selected items in isolated workspaces |
| `d` | Inspect bounded evidence (PR diff, issue discussion) |
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

Slay preserves order and partitions work into bounded, type-homogeneous
repository waves. Pull requests returned to the authenticated author with
requested changes form the first repair lane and are complete when a corrected
head is pushed; the coordinator never reviews, approves, or merges its own PR.
Issue waves read the issue plus Hive queue and knowledge evidence, then ask OMP
workflowz to run one isolated `task` item per issue. An issue slay is complete
only after GitHub shows a submitted pull request for every issue. OMP owns agent
execution, task concurrency, task state, tools, sessions, and cancellation; the
extension owns queue projection, durable intent, GitHub mutation guards, and
presentation.
The mode also ships the `bluefin-doctrine` and `bluefin-ci-triage` task agents.

### 3. Start with one repository, or browse the organization

The commands above open the whole Project Bluefin queue. To narrow it, append
a repository—for example, `bluefin review projectbluefin/review`. The developer
recipe `just review-queue projectbluefin/review` uses
`ghcr.io/projectbluefin/review:stable`, the same OMP configuration, and the same
single-screen workbench.

## Using the OMP workbench

The queue and execution trace remain visible beside the prompt. Navigate and
select work with the keys above; `s` applies the matching PR or issue lifecycle.
Without an explicit issue-only start, `--autoslay` first repairs every visible
pull request authored by the current user with requested changes, then switches
to issues and processes them in bounded workflowz batches. Ordinary PR slay
reviews the exact head, repairs findings in isolation, reviews the repaired head
afresh, then approves and asks GitHub to squash-merge when its live rules permit.
Reviewer subagents remain read-only so verdict and mutation authorities stay
separate; the coordinator executes only the maintainer-confirmed approval and
landing lifecycle. OMP's advisor is enabled for every review session and
resolves through `modelRoles.advisor` → `@default`, following the maintainer's
selected model without pinning a provider.
The [workbench guide](docs/skills/review-dashboard.md) documents the
authority model.

## Run a worker

Hive assigns contributor work; the OMP workbench is the maintainer surface.
Both contributor convenience commands launch the same OMP worker:

```bash
bluefin contribute
bluefin contribute projectbluefin/server
```

From a checkout, `just contribute` and `just review-container` are compatible
developer recipes for the same Hive-authorized OMP worker.

Choose provider, model, and effort inside OMP. The launcher does not interpret
profiles or export `AGENT_MODEL` / `AGENT_REASONING_EFFORT`.

Each contributor invocation prefers a foreground libkrun microVM and falls back
gracefully to an isolated foreground Apptainer container. An optional `org/repo`
argument names the isolated instance and selects
`~/.config/hive/contributor.<org-repo>.env`; Hive still chooses and assigns the
actual work. Different instance names use different persistent OMP volumes and
unique container names. Set `BLUEFIN_INSTANCE` to split concurrent runs for the
same target.

`bluefin setup [instance]` performs the attended Hive registration that writes
those files. The registration decides which hive the worker joins, so a bare
`bluefin contribute` does the work of whatever `~/.config/hive/contributor.env`
names — pass an instance, or repoint that file, to switch projects. Each launch
prints the hub it is joining before the container starts.

Keep the launching terminal open. **Ctrl-C stops only that invocation.**
Detached contributor containers are unsupported (`REVIEW_DETACH=1` is rejected).

Kubernetes users can scale workers with `bluefin cluster scale [N]` and stop
them with `bluefin cluster stop`. The compatible developer recipes are
`just contribute cluster [N]` and `just review-stop cluster`. Start with the
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

The contributor image layers the pinned Hive runtime at `a9f5ed287438df93194d80d0330866fd210ef50b`; it contains no maintainer UI. `ghcr.io/projectbluefin/contribute` is the separate distroless Hive + OMP worker, and `ghcr.io/projectbluefin/review` is the maintainer-facing OMP appliance.
See [image architecture and validation](docs/image-and-development.md).

</details>

Licensed under [Apache 2.0](LICENSE). [Visual credits](docs/images/README.md).
