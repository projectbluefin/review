You are my landing coordinator for projectbluefin/review.

Read the attached September 8 landing brief, then rediscover current
GitHub state and read the repository's current instructions. Treat the
brief's SHAs and CI results as an audit snapshot, not live authority.

Current accepted queue authority is main `5f5e1aa5084d67d7d2f3af580d4be067a9a782ab`:

- Completed and already on main: #420 at `a91e6c49`, #398 at `34f81eba`,
  #418 at `c8244270`, #404 at `b08a941a`, #427 at `e0942c0e`, and #416 at
  `5f5e1aa5`.
- Held: #366, head `e8197c553bd472c0102e9e08b2059565e1f1ed90`; keep it draft
  and held.

The completed entries are evidence to verify, never reasons to reopen, re-land,
or fan out work. Do not add #425 or #426 work.

Priority one is the existing PR queue. The ordinary PR queue is complete;
leave #366 draft/held while its host-runtime proof gate remains unmet.

Use one integration writer. Parallelize read-only reviews where useful,
but serialize shared dashboard/import/landing and launcher files.
Preserve unrelated worktrees and contributor ownership.

Remaining security hold:
- #366: Common #1039 closed unmerged. Replace the obsolete dependency
  with the accepted host-image delivery/proof owner when one exists.
  Inventory all current execution routes, including Kubernetes and
  broker jobs. Do not weaken runtime policy or claim native acceptance.

Land eligible PRs only through the normal authorized review/check/merge
process on the actual current head. No admin bypass, force-push, fake
independent approval, or unreviewed conflict resolution.

All ten named issue outcomes are now closed/completed: #397, #417, #392,
#400, #393, #403, #395, #359, #360, and #405. Reproduce source evidence only
when a prompt needs correction; do not reopen them, create issue fanout, or
delete live capabilities to satisfy old plans. The remaining objective is the
#366 hold/proof gate plus warranted source-backed prompt guidance.

Return exact heads, repairs, tests, hosted checks, review status, merge
SHAs, and issue dispositions. Distinguish merged, verified/closed,
updated-blocked, and not-attempted. A held #366 is an acceptable outcome;
a falsely "green" security claim is not.

## ADDENDUM — RUNSC / GVISOR OWNERSHIP CORRECTION

**This section supersedes all earlier instructions in the handoff that say runsc provisioning should live in `projectbluefin/common`, `projectbluefin/bluefin`, or wait for a Bluefin/Common image publication.**

The previous analysis got the ownership boundary wrong.

Jorge rejected the Common implementation and subsequently said the capability should move to the image/product layer rather than remain a host-distribution provisioning workaround. For this work, treat **`projectbluefin/review` as the owner of the runsc capability**.

Josh controls Review and does not want Review's local security boundary blocked on changes in repositories he does not control.

### Architectural decision

Review owns:

1. the pinned gVisor release identity;
2. amd64/arm64 artifact identities and verification;
3. acquisition and installation of the complete required gVisor runtime bundle;
4. lifecycle/update/removal of the Review-owned installation;
5. runtime discovery;
6. rootless Podman compatibility probing;
7. explicit runtime selection;
8. positive `OCIRuntime` verification;
9. failure classification and remediation instructions;
10. the tests and documentation for that contract.

Do **not** resurrect `projectbluefin/common#1039`.

Do **not** wait for a Bluefin Testing image to contain runsc.

Do **not** require a merge in `projectbluefin/bluefin` or `projectbluefin/common` before the Review-local runsc boundary can land.

### Important physical boundary

"Review owns runsc" does **not** mean that the only copy of `runsc` may live inside the Review workload container.

Podman must execute the OCI runtime in order to create the Review container. Therefore a `runsc` binary that exists only inside that not-yet-started container cannot provide its outer isolation boundary.

Review should instead own an explicit **host-side Review runtime installation** from the Review repository.

Use a Review-owned host path rather than `/usr/local`.

A suitable shape is a root-owned, versioned directory below writable host state such as:

`/var/lib/bluefin-review/runtime/gvisor/<release>/`

The complete release layout required by current gVisor must remain together, including `runsc` and its adjacent `gvisor-bin/` payload. Preserve any other files required by the selected upstream release.

Directory and executable permissions must allow the runtime's required re-execution behavior; do not repeat Common #1039's `0700` publication defect.

Publication should be atomic and ownership-protected.

### Launcher integration

Do not depend on `command -v runsc`.

Resolve the exact Review-owned runtime path and invoke Podman using that absolute path:

`podman --runtime=<absolute-review-owned-runsc-path> ...`

The launcher must still fail closed:

* no `crun` fallback;
* no `runc` fallback;
* no Podman-default-runtime fallback;
* no `--ignore-cgroups` workaround;
* no host-network workaround merely to make the probe pass.

Before credential resolution or agent execution:

1. verify the pinned Review-owned runtime installation;
2. verify `runsc --version`;
3. verify rootless Podman;
4. launch the credential-free disposable probe through that exact absolute runtime path;
5. inspect the running probe;
6. require positive runtime identity evidence;
7. clean the probe with exact ownership identity;
8. only then proceed to credential staging and an agent-capable launch.

The real Review workload must also explicitly select the same absolute runtime path.

### Provisioning UX

Provisioning must be an explicit Review operation, not an invisible side effect of starting an agent.

Add one small Review-owned runtime-management surface consistent with the repository's launcher conventions. A shape such as:

`just review-runtime install`
`just review-runtime update`
`just review-runtime remove`

is acceptable if it fits the current Justfile design after rebasing.

`review-doctor` should remain diagnostic. When the Review-owned runtime is missing it should identify that state and point to the explicit Review command that installs it.

Installation should:

* select a pinned gVisor point release;
* support the architectures Review actually claims;
* verify the downloaded artifact before parsing/extracting it;
* validate archive members;
* preserve the required `gvisor-bin/` sibling layout;
* reject foreign/symlink-confused target directories;
* publish atomically;
* use explicit safe permissions;
* never modify Podman's default runtime;
* never rely on an unpinned `latest`;
* never silently download missing runtime components during an agent launch.

Reuse the security lessons from Common #1039 without reusing its ownership or broken filesystem assumptions.

### PR #366

Rebase and substantially update #366.

Its current hold conditions are obsolete because Common #1039 closed unmerged and Jorge rejected that ownership model.

Remove these as merge prerequisites:

* Common #1039 merging;
* Common publishing the runtime layer;
* Bluefin consuming that Common layer;
* a Bluefin Testing image being the mechanism by which Review obtains runsc.

Replace them with Review-owned gates:

1. Review's pinned host-runtime provisioning is implemented and tested.
2. The deterministic fail-closed launcher contract is rebased onto current `main`.
3. Native rootless Podman using the Review-owned absolute runsc path succeeds on an available supported Bluefin host.
4. The actual Review container reports the intended runtime identity.
5. Normal non-host networking works.
6. Credentials remain strictly after runtime proof.
7. attended, detached, queue, doctor, cancellation, interruption, and cleanup behavior remain correct.
8. architecture evidence is reported honestly; unavailable architecture proof is not fabricated.
9. a fresh independent review evaluates the exact candidate head.

The goal is no longer "hold the old implementation until another repository provisions runsc."

The goal is:

> **Make Review self-contained with respect to its mandatory outer OCI isolation dependency.**

### Kubernetes / remote execution

Do not block the Review-local runsc implementation on Kubernetes cluster runtime configuration.

The Review-owned host runtime proves the local Podman execution boundary only.

A Kubernetes workload executes on a cluster node and therefore cannot inherit proof from the launcher's local runsc binary. Track cluster isolation as its own execution-capability contract.

Do not claim that local `OCIRuntime=runsc` proves a Kubernetes Job uses gVisor.

Likewise, do not disable unrelated working cluster behavior merely to make this PR appear universally complete without first reconciling its product contract.

For this landing pass:

* make local Review Podman execution genuinely fail-closed under the Review-owned runsc runtime;
* explicitly identify Kubernetes/remote runtime proof as a separate boundary;
* prevent documentation from conflating the two.

### Authority / blocker policy

The implementation agent may change `projectbluefin/review`.

It may inspect Bluefin/Common/gVisor/Podman for evidence.

It must **not make completion of the Review-owned runsc path conditional on another Project Bluefin repository accepting a change.**

If an upstream gVisor or Podman defect genuinely prevents Review's native runtime from functioning, record the precise external blocker with reproduction.

A preference about where Project Bluefin globally packages gVisor is no longer a blocker to Review shipping its own supported runtime dependency.

### Revised objective

The desired stack is:

`Review launcher`
→ `Review-owned pinned host gVisor bundle`
→ `rootless Podman --runtime=<absolute runsc path>`
→ `Review OCI image`
→ `Hive / review harness`
→ `agent`

Repository ownership and physical execution location are deliberately different:

* **Repository owner:** `projectbluefin/review`
* **Physical OCI runtime:** host
* **Workload image:** `ghcr.io/projectbluefin/review`
* **External Bluefin/Common provisioning dependency:** none

Treat this addendum as authoritative anywhere it conflicts with the earlier September 8 triage handoff.
