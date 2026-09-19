#!/usr/bin/env python3
"""Write the SPDX manifest for the distroless Hive + OMP contributor image.

The publish workflow ingests this document through syft's sbom-cataloger, which
keeps name, version and externalRefs when it merges an embedded document, and
drops the rest. A package therefore has to carry a purl locator to survive the
merge with its identity intact, and the verified digest has to ride inside that
locator as the purl spec's checksum qualifier rather than only in the SPDX
``checksums`` block the merge discards. scripts/generate-appliance-sbom.py emits
the same shape for the same reason; the two generators stay separate files
because each is COPY-ed alone into its build stage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def sha(value: str, name: str) -> str:
    if not SHA256.fullmatch(value):
        raise SystemExit(f"{name} must be a lowercase SHA-256 digest")
    return value


def with_checksum_qualifier(purl: str, checksum: str | None) -> str:
    """Attach a verified digest to a purl as the spec's checksum qualifier."""
    return f"{purl}?checksum=sha256:{checksum}" if checksum else purl


def package(
    name: str,
    version: str,
    download: str,
    purl: str,
    checksum: str | None = None,
) -> dict:
    if checksum:
        checksum = sha(checksum, name)
    item = {
        "SPDXID": "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]", "-", name),
        "name": name,
        "versionInfo": version,
        "downloadLocation": download,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": with_checksum_qualifier(purl, checksum),
            }
        ],
    }
    if checksum:
        item["checksums"] = [{"algorithm": "SHA256", "checksumValue": checksum}]
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--hive-commit", required=True)
    parser.add_argument("--omp-version", required=True)
    parser.add_argument("--omp-sha256", required=True)
    parser.add_argument("--node-version", required=True)
    parser.add_argument("--node-sha256", required=True)
    parser.add_argument("--gh-version", required=True)
    parser.add_argument("--gh-sha256", required=True)
    parser.add_argument("--tmux-version", required=True)
    parser.add_argument("--tmux-sha256", required=True)
    parser.add_argument("--ws-version", required=True)
    args = parser.parse_args()

    hive = args.hive_commit
    if not re.fullmatch(r"[0-9a-f]{40}", hive):
        raise SystemExit("hive commit must be a full lowercase SHA")
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "bluefin-contribute",
        "documentNamespace": f"https://projectbluefin.org/spdx/contribute/{args.version}/{args.revision}",
        "creationInfo": {"creators": ["Tool: generate-contribute-sbom.py"], "created": "1970-01-01T00:00:00Z"},
        "packages": [
            package(
                "omp",
                args.omp_version,
                f"https://github.com/can1357/oh-my-pi/releases/download/v{args.omp_version}/",
                f"pkg:github/can1357/oh-my-pi@v{args.omp_version}",
                args.omp_sha256,
            ),
            package(
                "node",
                args.node_version,
                f"https://nodejs.org/dist/v{args.node_version}/",
                f"pkg:generic/node@{args.node_version}",
                args.node_sha256,
            ),
            package(
                "gh",
                args.gh_version,
                f"https://github.com/cli/cli/releases/download/v{args.gh_version}/",
                f"pkg:github/cli/cli@v{args.gh_version}",
                args.gh_sha256,
            ),
            package(
                "tmux",
                args.tmux_version,
                f"https://github.com/tmux/tmux-builds/releases/download/v{args.tmux_version}/",
                f"pkg:github/tmux/tmux-builds@v{args.tmux_version}",
                args.tmux_sha256,
            ),
            package(
                "ws",
                args.ws_version,
                "https://registry.npmjs.org/ws",
                f"pkg:npm/ws@{args.ws_version}",
            ),
            package(
                "hive-contributor-runtime",
                hive,
                f"https://github.com/hivecommons/hive/tree/{hive}/bin",
                f"pkg:github/hivecommons/hive@{hive}",
            ),
        ],
    }
    with open(args.out, "w", encoding="utf-8") as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
