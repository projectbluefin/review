#!/usr/bin/env python3
"""Contract tests for scripts/generate-contribute-sbom.py.

The generator writes the SPDX document that records the isolated contribute
image's fetched and runtime components (omp, node, gh, tmux, ws, and the Hive
contributor runtime) into the image SBOM.

Until this suite, scripts/generate-contribute-sbom.py had zero test invocations
in tests/ or CI (.github/workflows/validate.yml).

Covered here:
- Valid SPDX document envelope (SPDX-2.3, dataLicense CC0-1.0, SPDXRef-DOCUMENT,
  name, documentNamespace, creationInfo with Tool creator and ISO timestamp).
- Package metadata (SPDXID, name, versionInfo, downloadLocation, licenses,
  copyright, checksums for digest-pinned components).
- Hex SHA-256 validation (case, length, alphabet, whitespace) naming the failing argument.
- Full 40-character commit SHA validation for --hive-commit.
- Required argument enforcement.
- Output file creation, valid JSON formatting, and trailing newline.
- Containerfile argument wiring and flag consistency.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "generate-contribute-sbom.py"
CONTAINERFILE = REPO_ROOT / "image" / "contribute" / "Containerfile"

SAMPLE_SHA256 = "a" * 64
SAMPLE_HIVE_COMMIT = "928e81d846a9c12e0d46da168f868e4a97319718"

BASE_ARGS = {
    "--version": "26.08.01",
    "--revision": "fd4437560fb87eae4707070b224ab1901ab6f0c6",
    "--hive-commit": SAMPLE_HIVE_COMMIT,
    "--omp-version": "18.1.18",
    "--omp-sha256": SAMPLE_SHA256,
    "--node-version": "24.18.1",
    "--node-sha256": "b" * 64,
    "--gh-version": "2.97.0",
    "--gh-sha256": "c" * 64,
    "--tmux-version": "3.7b",
    "--tmux-sha256": "d" * 64,
    "--ws-version": "8.21.3",
}


def run_generator(out: pathlib.Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the generator script; return CompletedProcess without raising."""
    args = dict(BASE_ARGS)
    args.update(overrides)
    argv = [sys.executable, str(SCRIPT), "--out", str(out)]
    for flag, value in args.items():
        argv += [flag, value]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def generate(testcase: unittest.TestCase, **overrides: str) -> dict:
    """Run the generator, assert success, and return the parsed JSON document."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "sbom.spdx.json"
        result = run_generator(out, **overrides)
        testcase.assertEqual(
            result.returncode, 0, f"generator failed: {result.stderr or result.stdout}"
        )
        testcase.assertTrue(out.is_file(), "generator did not create output file")
        raw = out.read_text(encoding="utf-8")
    testcase.assertTrue(raw.endswith("\n"), "SPDX JSON must end with a newline")
    return json.loads(raw)


def packages_by_name(document: dict) -> dict:
    return {pkg["name"]: pkg for pkg in document["packages"]}


class DocumentEnvelopeContract(unittest.TestCase):
    """SPDX JSON schema and envelope metadata structure."""

    def test_spdx_document_root_fields(self):
        doc = generate(self)
        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        self.assertEqual(doc["dataLicense"], "CC0-1.0")
        self.assertEqual(doc["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(doc["name"], "bluefin-contribute")
        self.assertEqual(
            doc["documentNamespace"],
            f"https://projectbluefin.org/spdx/contribute/{BASE_ARGS['--version']}/{BASE_ARGS['--revision']}",
        )

    def test_creation_info(self):
        creation = generate(self)["creationInfo"]
        self.assertIn("Tool: generate-contribute-sbom.py", creation["creators"])
        self.assertEqual(creation["created"], "1970-01-01T00:00:00Z")


class PackageMetadataContract(unittest.TestCase):
    """Packages declared in the contribute SBOM."""

    def test_expected_package_names(self):
        pkgs = packages_by_name(generate(self))
        expected_names = {
            "omp",
            "node",
            "gh",
            "tmux",
            "ws",
            "hive-contributor-runtime",
        }
        self.assertEqual(set(pkgs.keys()), expected_names)

    def test_package_spdx_ids_are_unique_and_valid(self):
        doc = generate(self)
        spdx_ids = [pkg["SPDXID"] for pkg in doc["packages"]]
        self.assertEqual(len(spdx_ids), len(set(spdx_ids)))
        for spdx_id in spdx_ids:
            self.assertRegex(spdx_id, r"^SPDXRef-[A-Za-z0-9.-]+$")

    def test_package_versions_and_download_locations(self):
        pkgs = packages_by_name(generate(self))
        self.assertEqual(pkgs["omp"]["versionInfo"], BASE_ARGS["--omp-version"])
        self.assertEqual(
            pkgs["omp"]["downloadLocation"],
            f"https://github.com/can1357/oh-my-pi/releases/download/v{BASE_ARGS['--omp-version']}/",
        )
        self.assertEqual(pkgs["node"]["versionInfo"], BASE_ARGS["--node-version"])
        self.assertEqual(
            pkgs["node"]["downloadLocation"],
            f"https://nodejs.org/dist/v{BASE_ARGS['--node-version']}/",
        )
        self.assertEqual(pkgs["gh"]["versionInfo"], BASE_ARGS["--gh-version"])
        self.assertEqual(
            pkgs["gh"]["downloadLocation"],
            f"https://github.com/cli/cli/releases/download/v{BASE_ARGS['--gh-version']}/",
        )
        self.assertEqual(pkgs["tmux"]["versionInfo"], BASE_ARGS["--tmux-version"])
        self.assertEqual(
            pkgs["tmux"]["downloadLocation"],
            f"https://github.com/tmux/tmux-builds/releases/download/v{BASE_ARGS['--tmux-version']}/",
        )
        self.assertEqual(pkgs["ws"]["versionInfo"], BASE_ARGS["--ws-version"])
        self.assertEqual(pkgs["ws"]["downloadLocation"], "https://registry.npmjs.org/ws")
        self.assertEqual(
            pkgs["hive-contributor-runtime"]["versionInfo"], BASE_ARGS["--hive-commit"]
        )
        self.assertEqual(
            pkgs["hive-contributor-runtime"]["downloadLocation"],
            f"https://github.com/hivecommons/hive/tree/{BASE_ARGS['--hive-commit']}/bin",
        )

    def test_package_license_and_copyright_fields(self):
        doc = generate(self)
        for pkg in doc["packages"]:
            self.assertEqual(pkg.get("licenseConcluded"), "NOASSERTION")
            self.assertEqual(pkg.get("licenseDeclared"), "NOASSERTION")
            self.assertEqual(pkg.get("copyrightText"), "NOASSERTION")

    def test_checksum_recorded_for_verified_artifacts(self):
        pkgs = packages_by_name(generate(self))
        for name in ("omp", "node", "gh", "tmux"):
            self.assertIn("checksums", pkgs[name], f"missing checksums for {name}")
            self.assertEqual(pkgs[name]["checksums"][0]["algorithm"], "SHA256")
        self.assertEqual(
            pkgs["omp"]["checksums"][0]["checksumValue"], BASE_ARGS["--omp-sha256"]
        )
        self.assertEqual(
            pkgs["node"]["checksums"][0]["checksumValue"], BASE_ARGS["--node-sha256"]
        )
        self.assertEqual(
            pkgs["gh"]["checksums"][0]["checksumValue"], BASE_ARGS["--gh-sha256"]
        )
        self.assertEqual(
            pkgs["tmux"]["checksums"][0]["checksumValue"], BASE_ARGS["--tmux-sha256"]
        )
        # Packages without archive checksums
        self.assertNotIn("checksums", pkgs["ws"])
        self.assertNotIn("checksums", pkgs["hive-contributor-runtime"])

    def test_every_package_declares_a_package_manager_purl(self):
        """syft's sbom-cataloger keeps externalRefs and drops the rest.

        A package with no purl locator reaches the published attestation
        without an identity a scanner can match, so the pinned component is
        invisible to vulnerability matching through the image's own SBOM.
        """
        for pkg in generate(self)["packages"]:
            with self.subTest(package=pkg["name"]):
                refs = pkg.get("externalRefs")
                self.assertTrue(refs, f"{pkg['name']} declares no externalRefs")
                reference = refs[0]
                self.assertEqual(reference["referenceCategory"], "PACKAGE-MANAGER")
                self.assertEqual(reference["referenceType"], "purl")
                self.assertTrue(
                    reference["referenceLocator"].startswith("pkg:"),
                    f"{pkg['name']} locator is not a purl: "
                    f"{reference['referenceLocator']!r}",
                )

    def test_purls_carry_the_verified_digest_as_a_checksum_qualifier(self):
        """The SPDX ``checksums`` block does not survive the syft merge.

        The qualifier is the only place a verified digest reaches the
        attestation, so every component the build verifies must carry it.
        """
        pkgs = packages_by_name(generate(self))

        def locator(name: str) -> str:
            return pkgs[name]["externalRefs"][0]["referenceLocator"]

        self.assertEqual(
            locator("omp"),
            f"pkg:github/can1357/oh-my-pi@v{BASE_ARGS['--omp-version']}"
            f"?checksum=sha256:{BASE_ARGS['--omp-sha256']}",
        )
        self.assertEqual(
            locator("node"),
            f"pkg:generic/node@{BASE_ARGS['--node-version']}"
            f"?checksum=sha256:{BASE_ARGS['--node-sha256']}",
        )
        self.assertEqual(
            locator("gh"),
            f"pkg:github/cli/cli@v{BASE_ARGS['--gh-version']}"
            f"?checksum=sha256:{BASE_ARGS['--gh-sha256']}",
        )
        self.assertEqual(
            locator("tmux"),
            f"pkg:github/tmux/tmux-builds@v{BASE_ARGS['--tmux-version']}"
            f"?checksum=sha256:{BASE_ARGS['--tmux-sha256']}",
        )

    def test_unverified_components_carry_a_bare_purl(self):
        """No digest is verified for these two, so none may be claimed."""
        pkgs = packages_by_name(generate(self))

        def locator(name: str) -> str:
            return pkgs[name]["externalRefs"][0]["referenceLocator"]

        self.assertEqual(locator("ws"), f"pkg:npm/ws@{BASE_ARGS['--ws-version']}")
        self.assertEqual(
            locator("hive-contributor-runtime"),
            f"pkg:github/hivecommons/hive@{BASE_ARGS['--hive-commit']}",
        )
        for name in ("ws", "hive-contributor-runtime"):
            with self.subTest(package=name):
                self.assertNotIn("checksum=", locator(name))

    def test_purl_version_tracks_the_argument(self):
        """A bare-substring purl check would pass on a hardcoded version."""
        pkgs = packages_by_name(generate(self, **{"--gh-version": "9.9.9"}))
        self.assertEqual(
            pkgs["gh"]["externalRefs"][0]["referenceLocator"],
            f"pkg:github/cli/cli@v9.9.9?checksum=sha256:{BASE_ARGS['--gh-sha256']}",
        )


class InputValidationContract(unittest.TestCase):
    """Validation of digests, commits, and arguments."""

    def assert_rejected(self, expected_stderr: str, **overrides: str):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sbom.spdx.json"
            result = run_generator(out, **overrides)
            self.assertNotEqual(result.returncode, 0, f"generator unexpectedly succeeded: {overrides}")
            self.assertIn(expected_stderr, result.stderr)
            self.assertFalse(out.exists())

    def test_uppercase_sha256_rejected(self):
        self.assert_rejected(
            "omp must be a lowercase SHA-256 digest",
            **{"--omp-sha256": "A" * 64},
        )

    def test_short_sha256_rejected(self):
        self.assert_rejected(
            "node must be a lowercase SHA-256 digest",
            **{"--node-sha256": "b" * 63},
        )

    def test_long_sha256_rejected(self):
        self.assert_rejected(
            "gh must be a lowercase SHA-256 digest",
            **{"--gh-sha256": "c" * 65},
        )

    def test_non_hex_sha256_rejected(self):
        self.assert_rejected(
            "tmux must be a lowercase SHA-256 digest",
            **{"--tmux-sha256": "g" * 64},
        )

    def test_whitespace_sha256_rejected(self):
        self.assert_rejected(
            "omp must be a lowercase SHA-256 digest",
            **{"--omp-sha256": " " + "a" * 64},
        )

    def test_invalid_hive_commit_rejected(self):
        self.assert_rejected(
            "hive commit must be a full lowercase SHA",
            **{"--hive-commit": "deadbeef"},
        )
        self.assert_rejected(
            "hive commit must be a full lowercase SHA",
            **{"--hive-commit": "G" * 40},
        )

    def test_missing_required_flag_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sbom.spdx.json"
            argv = [sys.executable, str(SCRIPT), "--out", str(out)]
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required", result.stderr)


class ContainerfileWiringContract(unittest.TestCase):
    """Alignment between scripts/generate-contribute-sbom.py and image/contribute/Containerfile."""

    def get_containerfile_invocation(self) -> str:
        lines = CONTAINERFILE.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if "/usr/local/libexec/contribute-sbom" in line and not line.startswith("COPY"):
                block = []
                for cont in lines[idx:]:
                    block.append(cont)
                    if not cont.rstrip().endswith("\\"):
                        break
                return "\n".join(block)
        self.fail("contribute-sbom invocation not found in Containerfile")

    def test_containerfile_passes_all_required_flags(self):
        invocation = self.get_containerfile_invocation()
        for flag in BASE_ARGS.keys():
            self.assertIn(flag, invocation, f"Containerfile is missing required flag {flag}")
        self.assertIn("--out", invocation, "Containerfile is missing --out flag")

    def test_generator_declares_every_flag_passed_by_containerfile(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        declared = set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', script_text))
        passed = set(re.findall(r"(--[a-z0-9-]+)[ =]", self.get_containerfile_invocation()))
        self.assertTrue(passed, "No flags found in Containerfile contribute-sbom invocation")
        self.assertLessEqual(
            passed,
            declared,
            f"Containerfile passes undeclared flags: {passed - declared}",
        )


if __name__ == "__main__":
    unittest.main()
