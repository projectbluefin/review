import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image" / "tui"))

from review_result import MAX_RAW_CHARS, ReviewResult, adapt_current_engine, parse_review_result

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


class ReviewResultContractTests(unittest.TestCase):
    def test_round_trip_preserves_versioned_evidence_contract(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 1, "medium": 2, "low": 0},
            "findings": [
                {"severity": "high", "title": "unsafe path", "file": "x.py", "line": 7},
                {"severity": "medium", "title": "missing test", "file": "test_x.py", "line": 9},
                {"severity": "medium", "title": "weak assertion", "file": "test_x.py", "line": 12},
            ],
            "verification": [{"name": "unit", "state": "verified", "evidence": "pytest"}],
            "provenance": {"backend": "goose", "model": "gpt-5.6-luna"},
            "overlap": {"duplicates": [12], "shared_files": ["x.py"]},
            "live": {"ci": "failure", "mergeable": "MERGEABLE"},
            "raw_evidence": ["check output"],
        })
        encoded = json.loads(result.to_json())
        self.assertEqual(encoded["version"], 1)
        self.assertEqual(encoded["counts"]["high"], 1)
        self.assertEqual(encoded["findings"][0]["file"], "x.py")
        self.assertEqual(encoded["live"]["ci"], "failure")
        self.assertEqual(parse_review_result(result.to_json()).state, "findings")

    def test_malformed_or_inconsistent_contract_is_unparsable(self):
        malformed = ReviewResult.from_dict({"version": "not-a-version", "state": "complete"})
        self.assertEqual(malformed.state, "unparsable")
        inconsistent = ReviewResult.from_dict({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [{"severity": "high", "file": "x.py", "line": 7, "title": "x"}],
        })
        self.assertEqual(inconsistent.state, "unparsable")

    def test_malformed_nested_evidence_is_unparsable(self):
        base = {
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "findings": [{"severity": "high", "title": "x", "file": "x.py", "line": 7}],
            "verification": [{"name": "unit", "state": "verified", "evidence": "pytest"}],
            "provenance": {"backend": "goose", "model": "m"},
            "overlap": {"duplicates": [], "shared_files": []},
        }
        for finding in (
            {"severity": "high", "title": "x", "line": 7},
            {"severity": "high", "title": "x", "file": "x.py", "line": "7"},
        ):
            payload = {**base, "findings": [finding]}
            self.assertEqual(ReviewResult.from_dict(payload).state, "unparsable")
        invalid_verification = {**base, "verification": [{"name": "unit", "state": "maybe", "evidence": "x"}]}
        self.assertEqual(ReviewResult.from_dict(invalid_verification).state, "unparsable")
        incomplete_provenance = {**base, "provenance": {"backend": "goose"}}
        self.assertEqual(ReviewResult.from_dict(incomplete_provenance).state, "unparsable")
        incomplete_overlap = {**base, "overlap": {"duplicates": []}}
        self.assertEqual(ReviewResult.from_dict(incomplete_overlap).state, "unparsable")

    def test_malformed_jsonl_finding_is_unparsable_with_raw_evidence(self):
        output = "\n".join([
            "goose review: check 'unit' completed: pytest",
            '{"severity":"high","path":"x.py","line_start":"nope","summary":"x","check":"unit"}',
            "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))",
        ])
        result = adapt_current_engine(output, 0, {"backend": "goose", "model": "m"})
        self.assertEqual(result.state, "unparsable")
        self.assertEqual(result.raw_evidence, output.splitlines())

    def test_counts_and_findings_are_required_typed_fields(self):
        for field in ("counts", "findings"):
            for value in (None, False):
                payload = {"version": 1, "state": "complete", "counts": {}, "findings": []}
                payload[field] = value
                self.assertEqual(ReviewResult.from_dict(payload).state, "unparsable")
            payload = {"version": 1, "state": "complete", "counts": {}, "findings": []}
            payload.pop(field)
            self.assertEqual(ReviewResult.from_dict(payload).state, "unparsable")

    def test_malformed_raw_evidence_is_unparsable(self):
        payload = {
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
        }
        for value in ({"line": 1}, 7, ["ok", {"line": 1}]):
            self.assertEqual(ReviewResult.from_dict({**payload, "raw_evidence": value}).state, "unparsable")
            self.assertEqual(parse_review_result(json.dumps({**payload, "raw_evidence": value})).state, "unparsable")
        self.assertEqual(parse_review_result("not json", raw_evidence={"line": 1}).state, "unparsable")

    def test_truncated_output_never_becomes_clean(self):
        output = "\n".join(["goose review: orchestrator emitted 0 finding(s) from 0 check(s) (main: ran, 0 finding(s))"] + ["noise"] * 400)
        result = adapt_current_engine(output, 0)
        self.assertEqual(result.state, "unparsable")
        self.assertEqual(len(result.raw_evidence), 400)

    def test_default_adapter_provenance_round_trips(self):
        result = adapt_current_engine(fixture("goose-review-clean.txt"), 0)
        self.assertEqual(parse_review_result(result.to_json()).state, "complete")
        self.assertEqual(result.provenance["backend"], "goose")

    def test_huge_summary_count_is_unparsable(self):
        output = "goose review: orchestrator emitted " + ("9" * 10000) + " finding(s) from 0 check(s) (main: ran, 0 finding(s))"
        result = adapt_current_engine(output, 0)
        self.assertEqual(result.state, "unparsable")

    def test_disagreeing_total_and_main_counts_are_unparsable(self):
        output = "goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 99 finding(s))"
        result = adapt_current_engine(output, 0)
        self.assertEqual(result.state, "unparsable")

    def test_present_falsey_optional_fields_are_not_defaults(self):
        payload = {
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
        }
        for field, value in (("verification", {}), ("provenance", []), ("overlap", [])):
            self.assertEqual(ReviewResult.from_dict({**payload, field: value}).state, "unparsable")

    def test_complete_with_unverified_check_is_not_clean(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
            "verification": [{"name": "unit", "state": "unverified", "evidence": "failed"}],
        })
        self.assertEqual(result.state, "incomplete")
        self.assertFalse(result.is_clean)

    def test_oversized_and_deep_structured_payloads_are_unparsable(self):
        oversized = json.dumps({"version": 1, "state": "complete", "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}, "findings": [], "raw_evidence": ["x" * MAX_RAW_CHARS]})
        self.assertEqual(parse_review_result(oversized).state, "unparsable")
        deep = "[" * 2000 + "]" * 2000
        self.assertEqual(parse_review_result(deep).state, "unparsable")

    def test_main_count_matches_parsed_main_findings_and_progress(self):
        finding = '{"severity":"medium","path":"x.py","line_start":4,"summary":"main finding","check":"main"}'
        contradictory = "\n".join([finding, "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 0 finding(s))"])
        self.assertEqual(adapt_current_engine(contradictory, 0).state, "unparsable")
        progress_contradiction = "\n".join(["goose review: check 'main' completed: 0 finding(s)", finding, "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))"])
        self.assertEqual(adapt_current_engine(progress_contradiction, 0).state, "unparsable")

    def test_incomplete_and_unparsable_never_become_clean(self):
        for state in ("incomplete", "unparsable", "failed"):
            result = ReviewResult.from_dict({"version": 1, "state": state})
            self.assertNotEqual(result.state, "complete")
            self.assertFalse(result.is_clean)

    def test_malformed_input_is_explicit_unparsable_with_raw_evidence(self):
        result = parse_review_result("not json", raw_evidence="not json")
        self.assertEqual(result.state, "unparsable")
        self.assertEqual(result.raw_evidence, ["not json"])

    def test_current_engine_adapter_parses_real_jsonl_findings_and_checks(self):
        output = fixture("goose-review-findings.txt")
        result = adapt_current_engine(output, 0, {"backend": "goose", "model": "m"})
        self.assertEqual(result.state, "findings")
        self.assertEqual(result.counts["high"], 1)
        self.assertEqual(result.counts["medium"], 1)
        self.assertEqual(result.findings[0]["file"], "image/entrypoint.sh")
        self.assertEqual(result.findings[0]["line"], 87)
        self.assertEqual(result.verification[0]["name"], "bluefin-doctrine")
        self.assertEqual(result.verification[0]["state"], "verified")
        self.assertEqual(result.provenance["model"], "m")
        self.assertEqual(result.raw_evidence, output.splitlines())

    def test_current_engine_adapter_requires_structured_clean_summary(self):
        self.assertTrue(adapt_current_engine(fixture("goose-review-clean.txt"), 0).is_clean)
        result = adapt_current_engine("0 findings", 0)
        self.assertEqual(result.state, "unparsable")
        self.assertFalse(result.is_clean)

    def test_current_engine_adapter_keeps_failed_check_incomplete(self):
        result = adapt_current_engine(fixture("goose-review-incomplete.txt"), 65)
        self.assertEqual(result.state, "incomplete")
        self.assertIn("unverified", {item["state"] for item in result.verification})

    def test_nonzero_engine_exit_is_failed_even_when_output_mentions_zero(self):
        result = adapt_current_engine("0 finding(s)", 2)
        self.assertEqual(result.state, "failed")
        self.assertFalse(result.is_clean)


if __name__ == "__main__":
    unittest.main()
