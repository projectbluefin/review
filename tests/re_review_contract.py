"""Contract tests for exact-head re-review delta classification."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "image" / "tui"))

from re_review import (  # noqa: E402
    DeltaInput,
    FallbackReason,
    FindingDisposition,
    FindingEvidence,
    H1Evidence,
    PriorFinding,
    Region,
    classify_head_delta,
)
from review_evidence_manifest import (  # noqa: E402
    ReviewEvidenceManifest,
    ReviewRequest,
)


H0 = "0" * 40
H1 = "1" * 40
BASE = "2" * 40


def manifest(*, base_sha: str = BASE, head_sha: str = H1) -> ReviewEvidenceManifest:
    return ReviewEvidenceManifest(
        ReviewRequest("octo", "sample", 17, base_sha, head_sha, "actor", "tenant", generated_at="now")
    )


class ReReviewContractTests(unittest.TestCase):
    def test_current_h1_manifest_must_bind_both_exact_heads(self) -> None:
        for kwargs in (
            {"head_sha": "3" * 40},
            {"base_sha": "4" * 40},
        ):
            with self.subTest(kwargs):
                with self.assertRaises(ValueError):
                    classify_head_delta(
                        DeltaInput(
                            reviewed_head_sha=H0,
                            current_head_sha=H1,
                            reviewed_merge_base_sha=BASE,
                            current_merge_base_sha=BASE,
                            current_h1_manifest=manifest(**kwargs),
                        )
                    )

    def test_omitted_current_evidence_is_unmappable(self) -> None:
        result = classify_head_delta(
            DeltaInput(
                reviewed_head_sha=H0,
                current_head_sha=H1,
                reviewed_merge_base_sha=BASE,
                current_merge_base_sha=BASE,
                prior_findings=(PriorFinding("old", FindingEvidence("old.py", 7, 7)),),
                current_h1_manifest=manifest(),
            )
        )

        self.assertEqual(result.findings[0].disposition, FindingDisposition.INVALIDATED_UNMAPPABLE)

    def test_delta_binds_exact_heads_and_classifies_explicit_evidence(self) -> None:
        current = manifest()
        result = classify_head_delta(
            DeltaInput(
                reviewed_head_sha=H0,
                current_head_sha=H1,
                reviewed_merge_base_sha=BASE,
                current_merge_base_sha=BASE,
                changed_regions=(Region("changed.py", 10, 20),),
                prior_findings=(
                    PriorFinding("changed", FindingEvidence("changed.py", 12, 12)),
                    PriorFinding("unchanged", FindingEvidence("same.py", 4, 4)),
                    PriorFinding("stale", FindingEvidence("same.py", 8, 8)),
                    PriorFinding("unmappable", None),
                ),
                evidence=(
                    FindingEvidence("changed.py", 12, 12, stale=False),
                    FindingEvidence("same.py", 4, 4, stale=False),
                    FindingEvidence("same.py", 8, 8, stale=True),
                ),
                newly_supported=(H1Evidence("new-proof", "new.py", 3),),
                current_h1_manifest=current,
            )
        )

        self.assertEqual((result.reviewed_head_sha, result.current_head_sha), (H0, H1))
        self.assertEqual(result.current_h1_manifest, current)
        self.assertEqual(
            {item.finding_id: item.disposition for item in result.findings},
            {
                "changed": FindingDisposition.CHANGED_REGION,
                "unchanged": FindingDisposition.UNCHANGED_REGION,
                "stale": FindingDisposition.STALE_REEVALUATE,
                "unmappable": FindingDisposition.INVALIDATED_UNMAPPABLE,
            },
        )
        self.assertEqual(result.newly_supported[0].finding_id, "new-proof")

    def test_uncertainty_and_sensitive_surfaces_require_explicit_full_review(self) -> None:
        for changes, reason in (
            ({"mapping_uncertain": True}, FallbackReason.MAPPING_UNCERTAIN),
            ({"sensitive_surfaces_changed": True}, FallbackReason.SENSITIVE_SURFACE),
            ({"prior_review_complete": False}, FallbackReason.PRIOR_REVIEW_INCOMPLETE),
            ({"bounded_risk_exceeded": True}, FallbackReason.RISK_BOUND_EXCEEDED),
            ({"capability_available": False}, FallbackReason.CAPABILITY_ABSENT),
            ({"current_merge_base_sha": "3" * 40}, FallbackReason.MERGE_BASE_CHANGED),
        ):
            with self.subTest(changes):
                values = dict(
                    reviewed_head_sha=H0,
                    current_head_sha=H1,
                    reviewed_merge_base_sha=BASE,
                    current_merge_base_sha=BASE,
                    current_h1_manifest=manifest(),
                )
                values.update(changes)
                if "current_merge_base_sha" in changes:
                    values["current_h1_manifest"] = manifest(
                        base_sha=changes["current_merge_base_sha"]
                    )
                result = classify_head_delta(DeltaInput(**values))
                self.assertTrue(result.full_review_required)
                self.assertIn(reason, result.fallback_reasons)

    def test_h0_authority_never_appears_in_h1_contract(self) -> None:
        result = classify_head_delta(
            DeltaInput(
                reviewed_head_sha=H0,
                current_head_sha=H1,
                reviewed_merge_base_sha=BASE,
                current_merge_base_sha=BASE,
                current_h1_manifest=manifest(),
                prior_authority={
                    "approval": "APPROVED",
                    "clean_verdict": True,
                    "merge_authorized": True,
                    "queue_state": "queued",
                },
            )
        )

        self.assertIsNone(result.authority)
        encoded = result.to_dict()
        self.assertNotIn("authority", encoded)
        self.assertNotIn("approval", encoded)
        self.assertNotIn("merge_authorized", encoded)
        self.assertNotIn("queue_state", encoded)


if __name__ == "__main__":
    unittest.main()
