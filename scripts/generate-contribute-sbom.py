#!/usr/bin/env python3
"""Write the SPDX manifest for the distroless Hive + OMP contributor image."""
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


def package(name: str, version: str, download: str, checksum: str | None = None) -> dict:
    item = {
        "SPDXID": "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]", "-", name),
        "name": name,
        "versionInfo": version,
        "downloadLocation": download,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
    }
    if checksum:
        item["checksums"] = [{"algorithm": "SHA256", "checksumValue": sha(checksum, name)}]
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
            package("omp", args.omp_version, f"https://github.com/can1357/oh-my-pi/releases/download/v{args.omp_version}/", args.omp_sha256),
            package("node", args.node_version, f"https://nodejs.org/dist/v{args.node_version}/", args.node_sha256),
            package("gh", args.gh_version, f"https://github.com/cli/cli/releases/download/v{args.gh_version}/", args.gh_sha256),
            package("tmux", args.tmux_version, f"https://github.com/tmux/tmux-builds/releases/download/v{args.tmux_version}/", args.tmux_sha256),
            package("ws", args.ws_version, "https://registry.npmjs.org/ws"),
            package("hive-contributor-runtime", hive, f"https://github.com/hivecommons/hive/tree/{hive}/bin"),
        ],
    }
    with open(args.out, "w", encoding="utf-8") as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
