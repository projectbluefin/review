# runsc / gVisor — decision receipt for the held Review PR

Date: September 8, 2026. Read-only assessment; no native runtime proof or repository mutation performed.

Current queue authority: main is `5f5e1aa5084d67d7d2f3af580d4be067a9a782ab`.
#420, #398, #418, #404, #427, and #416 are merged; #366 is the only held
open PR. The ten named issue outcomes are closed/completed. This receipt
preserves the #366 hold and does not reopen the completed queue or add
#425/#426 work.

## Decision

Do not resume Common #1039's runtime installer. Its final maintainer direction is to deliver runsc at the **host OS image layer**. Do not put the host OCI runtime inside the Review workload image. Keep Review #366 draft/held, but replace its obsolete dependency on merging Common #1039 with the actual accepted image-delivery work once an owner and change exist.

This direction solves the deployment ownership problem. It does **not** by itself establish rootless Podman/cgroup/network compatibility or prove every current Review execution path uses gVisor.

## Evidence and chronology

1. Common #1039 proposed a pinned, verified installer with architecture checks, archive allowlisting, and install/update/remove lifecycle. Initial review approved its supply-chain shape.
2. Josh recorded independent real-artifact SHA256 verification on September 3. Repeating that already-completed check is not today's main blocker.
3. Jorge's review at **September 7, 01:28 UTC (September 6, 9:28 p.m. EDT)** reported three defects: runtime writes into readonly verity-protected `/usr/local` on his Bluefin host; a published root-owned staging directory retaining mode 0700 and blocking another uid; and provisioning tests/helper checks absent from the actual CI enumeration.
4. His final comment at **September 7, 01:36 UTC (September 6, 9:36 p.m. EDT)** changed direction to image-layer delivery. That supersedes his earlier suggestion to relocate the runtime install under `/var`.
5. Common #1039 closed without merge. Review #416 merged into main at
   `5f5e1aa5`; Review #366, currently at
   `e8197c553bd472c0102e9e08b2059565e1f1ed90`, remains draft/held and still
   lacks an accepted host-image delivery owner. The linked native-acceptance
   discussion in Bluefin #1139 did not supply a completed acceptance receipt in
   this audit. No replacement image-layer PR was found in the searched
   linked/project runsc work; that is a search result, not proof no unpublished
   implementation exists.

## Runtime feasibility: separate three questions

**Delivery:** Can the supported Bluefin-family image supply a verified runsc payload, with ordinary-user traversal/execution and discoverability by supported rootless Podman, without runtime writes into readonly OS paths? Image composition is the appropriate seam; the exact packaging owner/location still needs accepted implementation.

**Native operation:** Can the real Review workload run with the required user mappings, cgroup manager, SELinux posture, and ordinary permitted networking? gVisor's official documentation distinguishes built-in `runsc --rootless` limitations from the caller-configured user namespace path used by Podman/Docker. Do not infer that all rootless gVisor requires host networking from the limitations of the first mode. Equally, documentation is not a Bluefin native proof.

**All-path coverage:** Does each current agent-capable route select and prove the required runtime before credentials and work? The old two-Podman-recipe assumption is stale. Audited main has `REVIEW_RUNTIME=k8s`; its dashboard Pod generator specifies non-root/seccomp/capability controls but no `runtimeClassName` and no positive runsc identity evidence. A cluster may have external runtime policy, but this repository source does not prove it. Inventory dashboard, contributor, broker-job, local, remote, detached, and fallback branches. Do not claim local-host runsc proves cluster-node isolation.

## Required hold gate

1. Accepted host-image delivery change, published immutable image identity, and actual consumption by the supported Bluefin image.
2. Ordinary-user executable/traversal and runtime-discovery checks against the image-delivered payload.
3. Native acceptance on each claimed architecture, recording host/image, kernel, Podman, runsc, cgroup, and security context.
4. Positive runtime evidence on the real product workload, not merely success from an argument-accepting fake or a standalone probe.
5. Proof before credential resolution/staging/Hive registration where the security contract requires it; no secret-bearing evidence.
6. Networking, readiness, cancellation, attached shutdown, detached stop, and exact-ownership cleanup evidence.
7. A reconciled policy and proof boundary for Kubernetes and remote routes before claiming universal coverage. Unproved paths must not silently inherit a “gVisor protected” claim. Do not disable useful routes or deploy RuntimeClasses without maintainer approval.
8. Fresh deterministic gates and independent exact-head review after the current launcher changes are reconciled.

No `ignore-cgroups=true`, host networking, default-runtime/crun/runc fallback, broad host-UDS allowance, or SELinux disabling to force a pass. Preserve #348's intended boundary while acknowledging #365 rolled enforcement back; #349's historical merge does not mean enforcement is live on main.

## Suggested bounded issue/PR update

> Common #1039 closed unmerged after the maintainer selected host-image runsc delivery instead of a runtime installer. The old “wait for #1039 to merge” prerequisite is superseded, not fulfilled. Keep #366 draft/held. Replace this edge with the accepted image-delivery owner/change once it exists, retain Bluefin #1139's native acceptance requirement, and reconcile every current agent-capable launch route, including `REVIEW_RUNTIME=k8s` and broker jobs. Main's green build/publication and a runsc binary's availability are not native/all-path isolation proof. The ordinary PR landing train is independent of this hold.

## Primary sources

- https://github.com/projectbluefin/common/pull/1039
- https://github.com/projectbluefin/common/pull/1039#pullrequestreview-5127325531
- https://github.com/projectbluefin/common/pull/1039#issuecomment-5563786674
- https://github.com/projectbluefin/review/pull/366
- https://github.com/projectbluefin/review/issues/348
- https://github.com/projectbluefin/review/issues/362
- https://github.com/projectbluefin/review/issues/351
- https://github.com/projectbluefin/bluefin/issues/1139
- https://github.com/projectbluefin/review/blob/5f5e1aa5084d67d7d2f3af580d4be067a9a782ab/justfile
- https://github.com/projectbluefin/review/blob/5f5e1aa5084d67d7d2f3af580d4be067a9a782ab/scripts/review-session-runtime.py
- https://gvisor.dev/docs/user_guide/rootless/
