# tests/model_profiles_contract.py
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.model_profiles import (
    DEPENDENCY_TITLE,
    GEMINI_TRIPLE,
    HIGH_ASSURANCE_MODELS,
    KIMI_TRIPLE,
    OPUS_TRIPLE,
    SOL_TRIPLE,
    classify_batch,
    escalation_triple,
    final_environment,
    final_triple,
    is_high_assurance,
    is_low_risk,
)


class ModelProfilesContractTests(unittest.TestCase):
    def test_triple_constants(self):
        self.assertEqual(GEMINI_TRIPLE, ("goose", "gemini-3.8-flash", "high"))
        self.assertEqual(SOL_TRIPLE, ("goose", "gpt-5.6-sol", "medium"))
        self.assertEqual(OPUS_TRIPLE, ("goose", "claude-opus-5", "high"))
        self.assertEqual(KIMI_TRIPLE, ("goose", "kimi-k3", "high"))

    def test_explicit_policies_choose_expected_review_triple(self):
        cases = {
            "gemini": GEMINI_TRIPLE,
            "opus": OPUS_TRIPLE,
            "sol": SOL_TRIPLE,
            "gpt-sol": SOL_TRIPLE,
            "k3": KIMI_TRIPLE,
            "kimi": KIMI_TRIPLE,
            "unknown": GEMINI_TRIPLE,
        }
        for policy, triple in cases.items():
            with self.subTest(policy=policy):
                self.assertEqual(final_triple(policy, "mixed", "final-review"), triple)

    def test_automatic_uses_dependency_classification_for_cheaper_reviewer(self):
        self.assertEqual(final_triple("automatic", "dependency", "final-review"), KIMI_TRIPLE)
        self.assertEqual(final_triple("automatic", "mixed", "final-review"), GEMINI_TRIPLE)

    def test_fixing_and_cleanup_always_use_kimi(self):
        for policy in ("gemini", "opus", "sol", "gpt-sol", "k3", "kimi", "automatic", "unknown"):
            with self.subTest(policy=policy, phase="fixing"):
                self.assertEqual(final_triple(policy, "mixed", "fixing"), KIMI_TRIPLE)
            with self.subTest(policy=policy, phase="cleanup"):
                self.assertEqual(final_triple(policy, "dependency", "cleanup"), KIMI_TRIPLE)

    def test_classify_batch_accepts_all_dependency_conventional_commit_titles(self):
        stops = [
            SimpleNamespace(title="chore(deps): bump foo", labels=[]),
            SimpleNamespace(title="build(deps-dev)!: bump bar", labels=[]),
            SimpleNamespace(title="  chore: update generated pins", labels=[]),
        ]
        self.assertEqual(classify_batch(stops), "dependency")
        self.assertTrue(DEPENDENCY_TITLE.match("build(deps): bump baz"))

    def test_classify_batch_accepts_all_dependency_labels(self):
        stops = [
            SimpleNamespace(title="anything", labels=["dependencies"]),
            SimpleNamespace(title="feat: not dependency by title", labels=["Dependencies"]),
        ]
        self.assertEqual(classify_batch(stops), "dependency")

    def test_classify_batch_falls_back_to_mixed_for_unknown_or_empty_batches(self):
        self.assertEqual(
            classify_batch([SimpleNamespace(title="update pin", labels=[])]),
            "mixed",
        )
        self.assertEqual(classify_batch([]), "mixed")

    def test_final_environment_sets_goose_variables_only_for_goose_rounds(self):
        default_env = final_environment(OPUS_TRIPLE)
        self.assertEqual(default_env["BLUEFIN_REVIEW_BACKEND"], "goose")
        self.assertEqual(default_env["GOOSE_MODEL"], "claude-opus-5")
        self.assertEqual(default_env["GOOSE_THINKING_EFFORT"], "high")

        goose_env = final_environment(OPUS_TRIPLE, "goose")
        self.assertEqual(goose_env["BLUEFIN_REVIEW_BACKEND"], "goose")
        self.assertEqual(goose_env["GOOSE_MODEL"], "claude-opus-5")
        self.assertEqual(goose_env["GOOSE_THINKING_EFFORT"], "high")

        codex_env = final_environment(OPUS_TRIPLE, "codex")
        self.assertEqual(codex_env["BLUEFIN_REVIEW_BACKEND"], "codex")
        self.assertEqual(codex_env["BLUEFIN_REVIEW_FINAL_MODEL"], "claude-opus-5")
        self.assertEqual(codex_env["BLUEFIN_REVIEW_FINAL_EFFORT"], "high")
        self.assertNotIn("GOOSE_MODEL", codex_env)
        self.assertNotIn("GOOSE_THINKING_EFFORT", codex_env)

    def test_high_assurance_and_cheap_classification(self):
        self.assertTrue(is_high_assurance(SOL_TRIPLE))
        self.assertTrue(is_high_assurance(OPUS_TRIPLE))
        self.assertTrue(is_high_assurance(KIMI_TRIPLE))
        self.assertFalse(is_high_assurance(GEMINI_TRIPLE))
        self.assertFalse(is_high_assurance("gemini-3.8-flash"))
        self.assertTrue(is_high_assurance("gpt-5.6-sol"))
        self.assertTrue(is_high_assurance("claude-opus-5"))
        self.assertIn("gpt-5.6-sol", HIGH_ASSURANCE_MODELS)

    def test_escalation_triple_always_resolves_high_assurance(self):
        cases = {
            ("automatic", "mixed"): SOL_TRIPLE,
            ("automatic", "dependency"): KIMI_TRIPLE,
            ("opus", "mixed"): OPUS_TRIPLE,
            ("sol", "mixed"): SOL_TRIPLE,
            ("gpt-sol", "mixed"): SOL_TRIPLE,
            ("k3", "mixed"): KIMI_TRIPLE,
            ("kimi", "mixed"): KIMI_TRIPLE,
        }
        for (policy, classification), expected in cases.items():
            with self.subTest(policy=policy, classification=classification):
                triple = escalation_triple(policy, classification)
                self.assertEqual(triple, expected)
                self.assertTrue(is_high_assurance(triple))

    def test_is_low_risk_identifies_deterministic_class_never_clean_verdict(self):
        self.assertTrue(is_low_risk(SimpleNamespace(title="chore(deps): bump foo", labels=[])))
        self.assertTrue(is_low_risk(SimpleNamespace(title="build(deps-dev)!: bump bar", labels=[])))
        self.assertTrue(is_low_risk(SimpleNamespace(title="feat: anything", labels=["dependencies"])))
        # Model clean verdict is NEVER in the low-risk class
        self.assertFalse(is_low_risk(SimpleNamespace(
            title="feat: major feature", labels=[], review_status="complete", is_clean=True
        )))
        self.assertFalse(is_low_risk(SimpleNamespace(
            title="fix: crash on boot", labels=[]
        )))
    def test_high_assurance_fails_closed_for_unknown_models(self):
        # An unrecognised model must never authorise a merge. Escalation exists
        # because a weak model's false negative reports clean, so anything not
        # on the allowlist is treated as not high assurance.
        for unknown in (
            "",
            "gpt-5-nano",
            "some-model-nobody-classified",
            "claude-opus-5-typo",
            "CLAUDE-OPUS-5",
        ):
            self.assertFalse(is_high_assurance(unknown), unknown)
        self.assertFalse(is_high_assurance(None))
        self.assertFalse(is_high_assurance(GEMINI_TRIPLE))

        for triple in (SOL_TRIPLE, OPUS_TRIPLE, KIMI_TRIPLE):
            self.assertTrue(is_high_assurance(triple), triple)

        # Every model escalation can select must itself be high assurance, or
        # escalation would re-escalate forever.
        for policy in ("automatic", "opus", "sol", "gpt-sol", "k3", "kimi", "unknown"):
            for classification in ("dependency", "mixed"):
                triple = escalation_triple(policy, classification)
                self.assertTrue(is_high_assurance(triple), (policy, classification))


if __name__ == "__main__":
    unittest.main()
