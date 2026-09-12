#!/usr/bin/env python3
"""Contract tests for scripts/generate-appliance-sbom.py.

The generator writes the SPDX document that carries the appliance's four
fetched components — omp, node, the pi npm tarball and gh — into the attested
SBOM. syft only inventories package-manager metadata, so if this document is
wrong those components are either missing from the attestation or described
with a digest the build never verified, and nothing in the image build fails.

Until this suite the script had no executed coverage at all: the only reference
to it anywhere under tests/ was a `.dockerignore` string check in
tests/appliance-contract.sh, and the one assertion that reads its output runs
solely in the runtime half, which is skipped without --image.

Covered here:
- architecture selection, including the arm64 -> aarch64 alias and the refusal
  of an unsupported arch
- per-architecture digest routing: an x86_64 build must not carry the aarch64
  digests, and vice versa
- SHA-256 validation (case, length, alphabet, surrounding whitespace) and the
  non-empty version checks, each naming the argument that failed
- the download URLs, which must stay identical to the ones the Containerfile
  actually fetches, per architecture
- purl locators, the ?checksum=sha256: qualifier, and the checksums block
  appearing only for components whose digest the build verifies
- SPDXID sanitisation for the scoped npm name
- document shape: SPDX-2.3, namespace, UTC creation timestamp, trailing newline
- that the output directory is created, and that argparse refuses a missing
  required argument
- that the Containerfile still passes every required argument
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
SCRIPT = REPO_ROOT / "scripts" / "generate-appliance-sbom.py"
CONTAINERFILE = REPO_ROOT / "image" / "appliance" / "Containerfile"

OMP_X86 = "a" * 64
OMP_ARM = "b" * 64
NODE_X86 = "c" * 64
NODE_ARM = "d" * 64
GH_X86 = "e" * 64
GH_ARM = "f" * 64

BASE_ARGS = {
    "--version": "26.08.03",
    "--revision": "0123456789abcdef0123456789abcdef01234567",
    "--omp-version": "1.2.3",
    "--omp-sha256-x86-64": OMP_X86,
    "--omp-sha256-aarch64": OMP_ARM,
    "--pi-version": "4.5.6",
    "--node-version": "24.9.0",
    "--node-sha256-x86-64": NODE_X86,
    "--node-sha256-aarch64": NODE_ARM,
    "--gh-version": "2.80.1",
    "--gh-sha256-x86-64": GH_X86,
    "--gh-sha256-aarch64": GH_ARM,
}


def run_generator(arch: str, out: pathlib.Path, **overrides: str):
    """Run the generator; return the CompletedProcess without raising."""
    args = dict(BASE_ARGS)
    args.update(overrides)
    argv = [sys.executable, str(SCRIPT), "--arch", arch, "--out", str(out)]
    for flag, value in args.items():
        argv += [flag, value]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def generate(testcase: unittest.TestCase, arch: str, **overrides: str) -> dict:
    """Run the generator, assert success, and return the parsed document."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "nested" / "sbom.spdx.json"
        result = run_generator(arch, out, **overrides)
        testcase.assertEqual(
            result.returncode, 0, f"generator failed: {result.stderr or result.stdout}"
        )
        # The Containerfile writes into a directory it has not created itself.
        testcase.assertTrue(out.is_file(), "generator did not create its output path")
        raw = out.read_text(encoding="utf-8")
    testcase.assertTrue(raw.endswith("\n"), "SPDX JSON must end with a newline")
    return json.loads(raw)


def packages_by_name(document: dict) -> dict:
    return {package["name"]: package for package in document["packages"]}


class ArchitectureSelection(unittest.TestCase):
    def test_arm64_is_an_alias_for_aarch64(self):
        alias = generate(self, "arm64")
        native = generate(self, "aarch64")
        self.assertEqual(alias["packages"], native["packages"])
        self.assertTrue(alias["documentNamespace"].endswith("-aarch64"))

    def test_unsupported_architecture_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sbom.spdx.json"
            result = run_generator("riscv64", out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported architecture: riscv64", result.stderr)
            self.assertFalse(out.exists(), "a refused build must not leave an SBOM")


class PerArchitectureDigests(unittest.TestCase):
    """The digests are the point: a build must carry its own, and only its own."""

    def test_x86_64_carries_only_the_x86_64_digests(self):
        found = packages_by_name(generate(self, "x86_64"))
        self.assertEqual(found["omp"]["checksums"][0]["checksumValue"], OMP_X86)
        self.assertEqual(found["node"]["checksums"][0]["checksumValue"], NODE_X86)
        self.assertEqual(found["gh"]["checksums"][0]["checksumValue"], GH_X86)
        serialised = json.dumps(found)
        for foreign in (OMP_ARM, NODE_ARM, GH_ARM):
            self.assertNotIn(foreign, serialised, "an aarch64 digest reached an x86_64 SBOM")

    def test_aarch64_carries_only_the_aarch64_digests(self):
        found = packages_by_name(generate(self, "aarch64"))
        self.assertEqual(found["omp"]["checksums"][0]["checksumValue"], OMP_ARM)
        self.assertEqual(found["node"]["checksums"][0]["checksumValue"], NODE_ARM)
        self.assertEqual(found["gh"]["checksums"][0]["checksumValue"], GH_ARM)
        serialised = json.dumps(found)
        for foreign in (OMP_X86, NODE_X86, GH_X86):
            self.assertNotIn(foreign, serialised, "an x86_64 digest reached an aarch64 SBOM")

    def test_only_verified_components_declare_a_checksum(self):
        found = packages_by_name(generate(self, "x86_64"))
        for name in ("omp", "node", "gh"):
            self.assertEqual(found[name]["checksums"][0]["algorithm"], "SHA256")
        for name in ("@earendil-works/pi-coding-agent", "bluefin-review-mode"):
            self.assertNotIn(
                "checksums",
                found[name],
                f"{name} has no digest the build verifies; claiming one would be false",
            )


class DigestValidation(unittest.TestCase):
    def assert_rejected(self, fragment: str, **overrides: str):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sbom.spdx.json"
            result = run_generator("x86_64", out, **overrides)
            self.assertNotEqual(result.returncode, 0, f"accepted a bad value: {overrides}")
            self.assertIn(fragment, result.stderr)
            self.assertFalse(out.exists())

    def test_uppercase_digest_is_refused(self):
        self.assert_rejected("omp_sha256 for x86_64", **{"--omp-sha256-x86-64": "A" * 64})

    def test_short_digest_is_refused(self):
        self.assert_rejected("node_sha256 for x86_64", **{"--node-sha256-x86-64": "c" * 63})

    def test_long_digest_is_refused(self):
        self.assert_rejected("gh_sha256 for x86_64", **{"--gh-sha256-x86-64": "e" * 65})

    def test_non_hex_digest_is_refused(self):
        self.assert_rejected("omp_sha256 for x86_64", **{"--omp-sha256-x86-64": "z" * 64})

    def test_trailing_newline_in_digest_is_refused(self):
        # sha256sum output pasted into a build arg is the realistic way this
        # goes wrong, and a digest with whitespace around it would sail into
        # the SBOM as a value no sha256sum --check would ever accept.
        self.assert_rejected("omp_sha256 for x86_64", **{"--omp-sha256-x86-64": "a" * 64 + "\n"})

    def test_digest_with_prefix_is_refused(self):
        self.assert_rejected(
            "gh_sha256 for x86_64", **{"--gh-sha256-x86-64": "sha256:" + "e" * 64}
        )

    def test_empty_versions_are_refused_by_name(self):
        for flag, label in (
            ("--omp-version", "omp version"),
            ("--pi-version", "pi version"),
            ("--node-version", "node version"),
            ("--gh-version", "gh version"),
        ):
            with self.subTest(flag=flag):
                self.assert_rejected(f"{label} must not be empty", **{flag: ""})

    def test_missing_required_argument_is_refused(self):
        argv = [sys.executable, str(SCRIPT), "--arch", "x86_64"]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)


class DownloadLocations(unittest.TestCase):
    """The URLs must stay identical to the ones the Containerfile fetches."""

    def test_x86_64_download_urls(self):
        found = packages_by_name(generate(self, "x86_64"))
        self.assertEqual(
            found["omp"]["downloadLocation"],
            "https://github.com/can1357/oh-my-pi/releases/download/v1.2.3/omp-linux-x64",
        )
        self.assertEqual(
            found["node"]["downloadLocation"],
            "https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-x64.tar.xz",
        )
        self.assertEqual(
            found["gh"]["downloadLocation"],
            "https://github.com/cli/cli/releases/download/v2.80.1/gh_2.80.1_linux_amd64.tar.gz",
        )

    def test_aarch64_download_urls(self):
        found = packages_by_name(generate(self, "aarch64"))
        self.assertEqual(
            found["omp"]["downloadLocation"],
            "https://github.com/can1357/oh-my-pi/releases/download/v1.2.3/omp-linux-arm64",
        )
        self.assertEqual(
            found["node"]["downloadLocation"],
            "https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-arm64.tar.xz",
        )
        self.assertEqual(
            found["gh"]["downloadLocation"],
            "https://github.com/cli/cli/releases/download/v2.80.1/gh_2.80.1_linux_arm64.tar.gz",
        )

    def test_npm_and_source_download_urls(self):
        found = packages_by_name(generate(self, "x86_64"))
        self.assertEqual(
            found["@earendil-works/pi-coding-agent"]["downloadLocation"],
            "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/"
            "pi-coding-agent-4.5.6.tgz",
        )
        self.assertEqual(
            found["bluefin-review-mode"]["downloadLocation"],
            "https://github.com/projectbluefin/review/tree/"
            f"{BASE_ARGS['--revision']}/image/extension/bluefin-review",
        )


class PackageIdentity(unittest.TestCase):
    def test_the_five_load_bearing_components_are_present(self):
        found = packages_by_name(generate(self, "x86_64"))
        self.assertEqual(
            sorted(found),
            sorted(
                [
                    "@earendil-works/pi-coding-agent",
                    "bluefin-review-mode",
                    "gh",
                    "node",
                    "omp",
                ]
            ),
        )

    def test_versions_are_recorded(self):
        found = packages_by_name(generate(self, "x86_64"))
        self.assertEqual(found["omp"]["versionInfo"], "1.2.3")
        self.assertEqual(found["@earendil-works/pi-coding-agent"]["versionInfo"], "4.5.6")
        self.assertEqual(found["node"]["versionInfo"], "24.9.0")
        self.assertEqual(found["gh"]["versionInfo"], "2.80.1")
        self.assertEqual(found["bluefin-review-mode"]["versionInfo"], "26.08.03")

    def test_spdxids_are_unique_and_sanitised(self):
        found = packages_by_name(generate(self, "x86_64"))
        identifiers = [package["SPDXID"] for package in found.values()]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(
            found["@earendil-works/pi-coding-agent"]["SPDXID"],
            "SPDXRef-Package-earendil-works-pi-coding-agent",
        )
        for identifier in identifiers:
            self.assertRegex(identifier, r"^SPDXRef-[A-Za-z0-9.\-]+$")

    def test_purls_carry_the_verified_digest_as_a_checksum_qualifier(self):
        found = packages_by_name(generate(self, "aarch64"))

        def locator(name: str) -> str:
            return found[name]["externalRefs"][0]["referenceLocator"]

        self.assertEqual(
            locator("omp"), f"pkg:github/can1357/oh-my-pi@v1.2.3?checksum=sha256:{OMP_ARM}"
        )
        self.assertEqual(locator("node"), f"pkg:generic/node@24.9.0?checksum=sha256:{NODE_ARM}")
        self.assertEqual(locator("gh"), f"pkg:github/cli/cli@v2.80.1?checksum=sha256:{GH_ARM}")
        self.assertEqual(
            locator("@earendil-works/pi-coding-agent"),
            "pkg:npm/%40earendil-works/pi-coding-agent@4.5.6",
        )
        self.assertEqual(
            locator("bluefin-review-mode"),
            f"pkg:github/projectbluefin/review@{BASE_ARGS['--revision']}",
        )

    def test_external_refs_are_package_manager_purls(self):
        for package in generate(self, "x86_64")["packages"]:
            reference = package["externalRefs"][0]
            self.assertEqual(reference["referenceCategory"], "PACKAGE-MANAGER")
            self.assertEqual(reference["referenceType"], "purl")
            self.assertFalse(package["filesAnalyzed"])
            self.assertTrue(package["comment"].strip())


class DocumentShape(unittest.TestCase):
    def test_spdx_envelope(self):
        document = generate(self, "x86_64")
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        self.assertEqual(document["dataLicense"], "CC0-1.0")
        self.assertEqual(document["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(document["name"], "projectbluefin-review-appliance")

    def test_namespace_is_unique_per_version_revision_and_arch(self):
        document = generate(self, "x86_64")
        self.assertEqual(
            document["documentNamespace"],
            "https://github.com/projectbluefin/review/sbom/review-appliance-"
            f"26.08.03-{BASE_ARGS['--revision']}-x86_64",
        )

    def test_creation_info_is_utc_and_tool_attributed(self):
        creation = generate(self, "x86_64")["creationInfo"]
        self.assertRegex(creation["created"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            creation["creators"], ["Tool: projectbluefin-review-generate-appliance-sbom"]
        )


class ContainerfileWiring(unittest.TestCase):
    """A required argument the Containerfile stops passing fails the build."""

    def invocation(self) -> str:
        """The generator's RUN invocation: from its name to the end of the
        line-continuation run, so later instructions cannot leak in."""
        lines = CONTAINERFILE.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "/usr/local/libexec/appliance-sbom" in line and not line.startswith("COPY"):
                block = []
                for continued in lines[index:]:
                    block.append(continued)
                    if not continued.rstrip().endswith("\\"):
                        break
                return "\n".join(block)
        self.fail("the Containerfile no longer runs the generator")

    def test_containerfile_passes_every_required_argument(self):
        invocation = self.invocation()
        for flag in ["--arch", "--out", "--revision", *BASE_ARGS]:
            self.assertIn(flag, invocation, f"the Containerfile stopped passing {flag}")

    def test_generator_declares_every_flag_the_containerfile_passes(self):
        declared = set(
            re.findall(r'add_argument\("(--[a-z0-9-]+)"', SCRIPT.read_text(encoding="utf-8"))
        )
        passed = set(re.findall(r"(--[a-z0-9-]+)[ =]", self.invocation()))
        self.assertTrue(passed, "no flags parsed out of the Containerfile invocation")
        self.assertLessEqual(
            passed,
            declared,
            f"the Containerfile passes flags the generator does not declare: {passed - declared}",
        )


if __name__ == "__main__":
    unittest.main()
