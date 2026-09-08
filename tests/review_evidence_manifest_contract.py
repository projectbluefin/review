"""Contract tests for the versioned review evidence manifest."""

from __future__ import annotations

import unittest
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image.tui.review_evidence_manifest import ReviewRequest


class ReviewRequestContractTests(unittest.TestCase):
    def test_request_preserves_full_base_and_head_identity(self) -> None:
        request = ReviewRequest(
            owner="example",
            repository="project",
            pull_request_number=17,
            base_sha="0123456789abcdef0123456789abcdef01234567",
            head_sha="89abcdef0123456789abcdef0123456789abcdef",
            actor="maintainer",
            tenant="example-tenant",
            generated_at="2026-08-11T00:00:00Z",
        )

        self.assertEqual(request.base_sha, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(request.head_sha, "89abcdef0123456789abcdef0123456789abcdef")

    def test_fixture_describes_a_generic_github_request(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/review_evidence_manifest.json").read_text()
        )

        request = ReviewRequest(**fixture)

        self.assertEqual((request.owner, request.repository), ("octo", "sample"))
        self.assertEqual(request.installation, "github-installation-7")

    def test_identity_strings_have_finite_boundaries(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/review_evidence_manifest.json").read_text()
        )
        for field, limit in (
            ("owner", 256), ("repository", 256), ("actor", 256),
            ("tenant", 256), ("installation", 256), ("generated_at", 128),
        ):
            with self.subTest(field=field):
                ReviewRequest(**{**fixture, field: "x" * limit})
                with self.assertRaises(ValueError):
                    ReviewRequest(**{**fixture, field: "x" * (limit + 1)})


if __name__ == "__main__":
    unittest.main()
