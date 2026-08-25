#!/usr/bin/env python3
"""Write the SPDX manifest for review-owned, archive-installed components.

The publish workflow scans the built image with syft, which inventories
package-manager metadata (npm packages, Python dist-info, binary classifiers)
but cannot see the components this image installs directly from release
archives or fetched source files: Goose, the GitHub CLI, tmux, Codex,
ripgrep, the pinned Hive contributor runtime files, the generated skill
bundles, and the review-owned git hooks. Those are the load-bearing parts of
the derived image, so an SBOM without them is an incomplete supply-chain
record (#78).

This script runs inside the image build, where every pin is in scope as a
resolved build argument -- including the Goose asset digests that CI resolves
from the release API immediately before building. It writes a standalone SPDX
2.3 JSON document to ``/opt/bluefin/sbom/review-components.spdx.json``. The
publish workflow's syft run ingests that document through its sbom-cataloger
(enabled via ``SYFT_SELECT_CATALOGERS``), so the attested SBOM names each
component with the strongest immutable identity the build possesses: the
pinned version and versioned download URL everywhere, plus the verified
archive SHA-256 where the pin is a build argument.
``tests/image-audit.sh --require-attestations`` then fails publication when
any of these components is missing from the attached SPDX evidence, so the
coverage cannot silently regress.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")

# Each publisher names architectures differently in its release assets; the
# build passes uname output and the per-component token is resolved here.
GH_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
TMUX_ARCH = {"x86_64": "x86_64", "aarch64": "arm64"}
RG_ARCH = {
    "x86_64": "x86_64-unknown-linux-musl",
    "aarch64": "aarch64-unknown-linux-musl",
}


def with_checksum_qualifier(purl: str, sha256: str) -> str:
    # syft's sbom-cataloger preserves only name, version and externalRefs when
    # merging an embedded document, so the verified asset digest rides through
    # to the published SBOM as the purl spec's standard checksum qualifier.
    if not sha256:
        return purl
    return f"{purl}?checksum=sha256:{sha256}"


def package(
    name: str,
    version: str,
    download_url: str,
    purl: str,
    comment: str,
    sha256: str = "",
) -> dict:
    entry = {
        "name": name,
        "SPDXID": f"SPDXRef-Package-{name}",
        "versionInfo": version,
        "downloadLocation": download_url,
        "filesAnalyzed": False,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": with_checksum_qualifier(purl, sha256),
            }
        ],
        "comment": comment,
    }
    if sha256:
        entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": sha256}]
    return entry


def require_sha256(value: str, label: str) -> str:
    if not SHA256_PATTERN.fullmatch(value):
        raise SystemExit(f"{label} must be a lowercase SHA-256 hex digest, got: {value!r}")
    return value


def require_commit(value: str, label: str) -> str:
    if not COMMIT_PATTERN.fullmatch(value):
        raise SystemExit(f"{label} must be a 40-character commit SHA, got: {value!r}")
    return value


def require_non_empty(value: str, label: str) -> str:
    if not value:
        raise SystemExit(f"{label} must not be empty")
    return value


def per_arch_sha256(args: argparse.Namespace, prefix: str, arch: str) -> str:
    # Normalized arch is exactly "x86_64" or "aarch64", matching the suffix of
    # each per-architecture checksum option.
    return require_sha256(vars(args)[f"{prefix}_{arch}"], f"{prefix} for {arch}")


def build_packages(args: argparse.Namespace, arch: str) -> list[dict]:
    goose_sha = per_arch_sha256(args, "goose_sha256", arch)
    codex_sha = per_arch_sha256(args, "codex_sha256", arch)
    code_mode_host_sha = per_arch_sha256(args, "codex_code_mode_host_sha256", arch)
    ripgrep_sha = per_arch_sha256(args, "ripgrep_sha256", arch)

    hive_commit = require_commit(args.hive_commit, "hive commit")
    skills_commit = require_commit(args.skills_commit, "organization skills commit")
    lab_skills_commit = require_commit(args.lab_skills_commit, "lab skills commit")
    goose_channel = require_non_empty(args.goose_channel, "goose channel")
    gh_version = require_non_empty(args.gh_version, "gh version")
    tmux_version = require_non_empty(args.tmux_version, "tmux version")
    codex_version = require_non_empty(args.codex_version, "codex version")
    ripgrep_version = require_non_empty(args.ripgrep_version, "ripgrep version")

    hive_raw = f"https://raw.githubusercontent.com/kubestellar/hive/{hive_commit}"
    return [
        package(
            "goose",
            goose_channel,
            "https://github.com/aaif-goose/goose/releases/download/"
            f"{goose_channel}/goose-{arch}-unknown-linux-musl.tar.gz",
            f"pkg:github/aaif-goose/goose@{goose_channel}",
            "The canary channel name is mutable; the asset SHA-256 here is the"
            " immutable identity, resolved from the release API at build time"
            " and verified with the aaif-goose/goose canary attestation before"
            " the archive was opened. Installed to /usr/local/bin/goose.",
            sha256=goose_sha,
        ),
        package(
            "gh",
            gh_version,
            "https://github.com/cli/cli/releases/download/"
            f"v{gh_version}/gh_{gh_version}_linux_{GH_ARCH[arch]}.tar.gz",
            f"pkg:github/cli/cli@{gh_version}",
            "Installed to /usr/local/bin/gh from the pinned cli/cli release"
            " archive, SHA-256 verified against the pin in image/Containerfile.",
        ),
        package(
            "tmux",
            tmux_version,
            "https://github.com/tmux/tmux-builds/releases/download/"
            f"v{tmux_version}/tmux-{tmux_version}-linux-{TMUX_ARCH[arch]}.tar.gz",
            f"pkg:github/tmux/tmux-builds@{tmux_version}",
            "Installed to /usr/local/bin/tmux from the pinned tmux-builds"
            " release archive, SHA-256 verified against the pin in"
            " image/Containerfile.",
        ),
        package(
            "codex",
            codex_version,
            "https://github.com/openai/codex/releases/download/"
            f"rust-v{codex_version}/codex-{arch}-unknown-linux-musl.tar.gz",
            f"pkg:github/openai/codex@{codex_version}",
            "Installed to /usr/local/bin/codex from the pinned openai/codex"
            " release archive.",
            sha256=codex_sha,
        ),
        package(
            "codex-code-mode-host",
            codex_version,
            "https://github.com/openai/codex/releases/download/"
            f"rust-v{codex_version}/codex-code-mode-host-{arch}-unknown-linux-musl.tar.gz",
            f"pkg:github/openai/codex@{codex_version}",
            "Installed to /usr/local/bin/codex-code-mode-host from the pinned"
            " openai/codex release archive.",
            sha256=code_mode_host_sha,
        ),
        package(
            "ripgrep",
            ripgrep_version,
            "https://github.com/BurntSushi/ripgrep/releases/download/"
            f"{ripgrep_version}/ripgrep-{ripgrep_version}-{RG_ARCH[arch]}.tar.gz",
            f"pkg:github/burntsushi/ripgrep@{ripgrep_version}",
            "Installed to /usr/local/bin/rg from the pinned BurntSushi/ripgrep"
            " release archive.",
            sha256=ripgrep_sha,
        ),
        package(
            "contributor-agent.sh",
            hive_commit,
            f"{hive_raw}/bin/contributor-agent.sh",
            f"pkg:github/kubestellar/hive@{hive_commit}",
            "Hive contributor runtime entrypoint, installed to"
            " /usr/local/bin/contributor-agent.sh at the pinned Hive commit.",
        ),
        package(
            "contributor-relay.sh",
            hive_commit,
            f"{hive_raw}/bin/contributor-relay.sh",
            f"pkg:github/kubestellar/hive@{hive_commit}",
            "Hive contributor relay, installed to"
            " /usr/local/bin/contributor-relay.sh at the pinned Hive commit.",
        ),
        package(
            "backends.conf",
            hive_commit,
            f"{hive_raw}/config/backends.conf",
            f"pkg:github/kubestellar/hive@{hive_commit}",
            "Hive backend registry, installed to /usr/local/etc/hive/"
            "backends.conf at the pinned Hive commit.",
        ),
        package(
            "bluefin-organization-skills",
            skills_commit,
            "https://github.com/projectbluefin/common/tree/"
            f"{skills_commit}/docs/skills",
            f"pkg:github/projectbluefin/common@{skills_commit}",
            "Organization skills generated into /home/dev/.agents/skills at"
            f" build time by projectbluefin/review@{args.revision}.",
        ),
        package(
            "bluefin-lab-skills",
            lab_skills_commit,
            "https://github.com/projectbluefin/lab/tree/"
            f"{lab_skills_commit}/docs/skills",
            f"pkg:github/projectbluefin/lab@{lab_skills_commit}",
            "Lab skills projected into /home/dev/.agents/skills at build time"
            f" by projectbluefin/review@{args.revision}.",
        ),
        package(
            "review-git-hooks",
            args.revision,
            f"https://github.com/projectbluefin/review/tree/{args.revision}/image/git-hooks",
            f"pkg:github/projectbluefin/review@{args.revision}",
            "Review-owned git hooks copied from image/git-hooks to"
            " /opt/bluefin/git-hooks at the source revision.",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", required=True, help="uname -m of the build host")
    parser.add_argument("--revision", required=True, help="review source revision (build arg REVIEW_REVISION)")
    parser.add_argument("--out", required=True, type=pathlib.Path, help="output SPDX JSON path")
    parser.add_argument("--goose-channel", required=True)
    parser.add_argument("--goose-sha256-x86-64", required=True)
    parser.add_argument("--goose-sha256-aarch64", required=True)
    parser.add_argument("--gh-version", required=True)
    parser.add_argument("--tmux-version", required=True)
    parser.add_argument("--codex-version", required=True)
    parser.add_argument("--codex-sha256-x86-64", required=True)
    parser.add_argument("--codex-sha256-aarch64", required=True)
    parser.add_argument("--codex-code-mode-host-sha256-x86-64", required=True)
    parser.add_argument("--codex-code-mode-host-sha256-aarch64", required=True)
    parser.add_argument("--ripgrep-version", required=True)
    parser.add_argument("--ripgrep-sha256-x86-64", required=True)
    parser.add_argument("--ripgrep-sha256-aarch64", required=True)
    parser.add_argument("--hive-commit", required=True)
    parser.add_argument("--skills-commit", required=True)
    parser.add_argument("--lab-skills-commit", required=True)
    args = parser.parse_args()

    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(args.arch)
    if arch is None:
        raise SystemExit(f"unsupported architecture: {args.arch}")

    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "projectbluefin-review-components",
        "documentNamespace": "https://github.com/projectbluefin/review/sbom/"
        f"review-components-{args.revision}-{arch}",
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: projectbluefin-review-generate-sbom-manifest"],
        },
        "packages": build_packages(args, arch),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
