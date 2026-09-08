# tests/gh_client_contract.py
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.gh_client import (
    BreakerRegistry,
    BreakerState,
    Dependency,
    GhClient,
    GhProcessTimeout,
    compute_backoff,
    get_breaker,
    gh,
    gh_mutation,
    is_rate_limited,
    parse_rate_limit,
    run_mutation,
    run_process_group,
)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class QueueRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command, timeout):
        self.calls.append((list(command), timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def rate_limited(stdout: str = "", stderr: str = "gh: secondary rate limit (HTTP 403)"):
    return completed(1, stdout, stderr)


class GhClientContractTests(unittest.TestCase):
    def test_retry_after_response_is_honoured_before_retry(self):
        limited = rate_limited(
            "HTTP/2.0 403 Forbidden\nRetry-After: 7\n\n{\"message\":\"slow down\"}\n"
        )
        runner = QueueRunner(limited, completed(0, "HTTP/2.0 200 OK\n\n{}\n"))
        clock = FakeClock()
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)

        result = client.read("api", "/user", timeout=9, attempts=2)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "{}\n")
        self.assertEqual(clock.sleeps, [7])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[0][0], ["gh", "api", "--include", "/user"])
        self.assertEqual(runner.calls[0][1], 9)

    def test_x_ratelimit_reset_is_honoured_when_retry_after_is_absent(self):
        limited = rate_limited(
            "HTTP/2.0 403 Forbidden\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1013\n\n{}\n",
            "gh: API rate limit exceeded (HTTP 403)",
        )
        runner = QueueRunner(limited, completed(0, "HTTP/2.0 200 OK\n\n{}\n"))
        clock = FakeClock(1000)
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)

        client.read("api", "/rate_limit", attempts=2)

        self.assertEqual(clock.sleeps, [13])
        self.assertEqual(len(runner.calls), 2)

    def test_backoff_is_exponential_capped_and_jittered_when_headers_are_absent(self):
        runner = QueueRunner(
            rate_limited(),
            rate_limited(),
            rate_limited(),
            rate_limited(),
            completed(0, "{}\n"),
        )
        clock = FakeClock()
        jitter_limits: list[float] = []

        def jitter(limit: float) -> float:
            jitter_limits.append(limit)
            return limit / 4

        client = GhClient(
            run=runner,
            clock=clock,
            sleep=clock.sleep,
            jitter=jitter,
            backoff_base=2,
            backoff_cap=5,
        )

        client.read("pr", "list", "--repo", "projectbluefin/review", attempts=5)

        self.assertEqual(len(clock.sleeps), 4)
        self.assertLess(clock.sleeps[0], clock.sleeps[1])
        self.assertLessEqual(clock.sleeps[2], 5)
        self.assertLessEqual(clock.sleeps[3], 5)
        self.assertEqual(jitter_limits[-2:], [2.5, 2.5])
        self.assertTrue(all(delay > floor for delay, floor in zip(clock.sleeps, [1, 2, 2.5, 2.5])))

    def test_read_retries_but_unproven_mutation_does_not(self):
        read_runner = QueueRunner(rate_limited(), completed(0, "{}\n"))
        read_clock = FakeClock()
        read_client = GhClient(run=read_runner, clock=read_clock, sleep=read_clock.sleep)

        self.assertEqual(read_client.read("pr", "view", "1", attempts=2).returncode, 0)
        self.assertEqual(len(read_runner.calls), 2)

        mutation_runner = QueueRunner(rate_limited(), completed(0, "{}\n"))
        mutation_clock = FakeClock()
        mutation_client = GhClient(run=mutation_runner, clock=mutation_clock, sleep=mutation_clock.sleep)

        result = mutation_client.mutation("api", "/repos/o/r/issues/1/comments", attempts=2)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(mutation_runner.calls), 1)
        self.assertEqual(mutation_clock.sleeps, [])
        self.assertTrue(mutation_client.state().blocked)

    def test_idempotent_mutation_retries_by_default(self):
        runner = QueueRunner(rate_limited(), completed(0, "{}\n"))
        clock = FakeClock()
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)

        result = client.mutation("api", "/repos/o/r/labels/x", idempotent=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.calls), 2)

    def test_mutation_never_retries_on_ambiguous_timeout_even_if_idempotent(self):
        # An ambiguous timeout on a mutation is NOT a licence to repeat it
        runner = QueueRunner(GhProcessTimeout(["gh"], 5), completed(0, "{}\n"))
        clock = FakeClock()
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)

        with self.assertRaises(GhProcessTimeout):
            client.mutation("api", "/repos/o/r/labels/x", idempotent=True, attempts=3)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_read_retries_on_subprocess_timeout(self):
        # Reads are safe to retry on timeout
        runner = QueueRunner(GhProcessTimeout(["gh"], 5), completed(0, "{}\n"))
        clock = FakeClock()
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)

        result = client.read("pr", "view", "1", attempts=2)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(clock.sleeps), 1)

    def test_rate_limit_detection_matches_standard_gh_errors(self):
        # Matches typical GitHub CLI error formats from REST, GraphQL, and proxy
        self.assertTrue(is_rate_limited(completed(1, "", "HTTP 429: Too Many Requests (https://api.github.com/...)")))
        self.assertTrue(is_rate_limited(completed(1, "", "HTTP 403: API rate limit exceeded (https://api.github.com/...)")))
        self.assertTrue(is_rate_limited(completed(1, "", "HTTP 403: You have exceeded a secondary rate limit.")))
        self.assertTrue(is_rate_limited(completed(1, "", "gh: secondary rate limit (HTTP 403)")))
        self.assertTrue(is_rate_limited(completed(1, "", "gh: Too Many Requests (HTTP 429)")))
        self.assertTrue(is_rate_limited(completed(1, "", "GraphQL: API rate limit exceeded")))
        self.assertFalse(is_rate_limited(completed(1, "", "HTTP 404: Not Found")))
        self.assertFalse(is_rate_limited(completed(0, "HTTP 429 in content", "")))

    def test_rate_limit_delay_extracts_from_text_or_headers(self):
        clock = FakeClock(1000.0)
        # 1. Header Retry-After
        p1 = completed(1, "HTTP/2.0 403 Forbidden\nRetry-After: 42\n\n{}", "")
        self.assertEqual(parse_rate_limit(p1, clock), 42.0)

        # 2. Textual retry after
        p2 = completed(1, "", "gh: rate limited; retry after 25")
        self.assertEqual(parse_rate_limit(p2, clock), 25.0)

        # 3. x-ratelimit-reset epoch timestamp
        p3 = completed(1, "x-ratelimit-reset: 1045\n", "HTTP 403: API rate limit exceeded")
        self.assertEqual(parse_rate_limit(p3, clock), 45.0)

    def test_breaker_registry_string_names_and_reset(self):
        clock = FakeClock(500.0)
        breakers = BreakerRegistry(clock=clock)

        self.assertFalse(breakers.state("github").blocked)
        self.assertIsNone(breakers.state("github").retry_at)

        breakers.open("github", 30.0, "rate limit")
        state = breakers.state("github")
        self.assertTrue(state.blocked)
        self.assertEqual(state.retry_at, 530.0)
        self.assertEqual(state.reason, "rate limit")

        # Independent states across canonical dependencies
        self.assertTrue(breakers.state(Dependency.GITHUB).blocked)
        self.assertFalse(breakers.state("hive").blocked)
        self.assertFalse(breakers.state("model_provider").blocked)

        breakers.close("github")
        self.assertFalse(breakers.state("github").blocked)
        self.assertIsNone(breakers.state("github").retry_at)

    def test_compute_backoff_cap_and_jitter(self):
        cap = 30.0
        base = 2.0
        # High attempt must be capped
        for att in range(10, 20):
            val = compute_backoff(attempt=att, base=base, cap=cap)
            self.assertLessEqual(val, cap)
            self.assertGreater(val, 0)

        # Jitter variation
        results = [compute_backoff(attempt=2, base=base, cap=cap) for _ in range(25)]
        self.assertGreater(len(set(results)), 1)

    def test_module_level_helpers_and_runner(self):
        self.assertEqual(run_process_group(["echo", "hello"], timeout=5).stdout.strip(), "hello")
        res = run_mutation(["echo", "mutated"], timeout=5)
        self.assertEqual(res.stdout.strip(), "mutated")
        self.assertIsInstance(get_breaker("github"), BreakerState)

    def test_every_call_requires_a_positive_deadline(self):
        client = GhClient(run=QueueRunner(completed(0)))

        with self.assertRaises(ValueError):
            client.read("api", "/user", timeout=None)
        with self.assertRaises(ValueError):
            client.mutation("api", "/user", timeout=0)

    def test_non_api_output_is_not_treated_as_included_headers(self):
        runner = QueueRunner(completed(0, "HTTP/2 is mentioned in this diff\n"))
        client = GhClient(run=runner)

        result = client.read("pr", "diff", "1")

        self.assertEqual(result.stdout, "HTTP/2 is mentioned in this diff\n")

    def test_slurped_api_output_strips_included_headers_inside_array(self):
        runner = QueueRunner(completed(
            0,
            "[HTTP/2.0 200 OK\n"
            "Content-Type: application/json\n"
            "\n"
            '{"data":{"search":{"nodes":[]}}}]',
        ))
        client = GhClient(run=runner)

        result = client.read("api", "graphql", "--paginate", "--slurp")

        self.assertEqual(result.stdout, '[{"data":{"search":{"nodes":[]}}}]')

    def test_paginated_slurped_api_output_strips_headers_after_commas(self):
        runner = QueueRunner(completed(
            0,
            "[HTTP/2.0 200 OK\n"
            "Content-Type: application/json\n"
            "\n"
            '{"data":{"page":1}}\n'
            ",HTTP/2.0 200 OK\n"
            "Content-Type: application/json\n"
            "\n"
            '{"data":{"page":2}}\n'
            "]",
        ))
        client = GhClient(run=runner)

        result = client.read("api", "graphql", "--paginate", "--slurp")

        self.assertEqual(
            result.stdout,
            '[{"data":{"page":1}}\n,{"data":{"page":2}}\n]',
        )

    def test_github_breaker_does_not_open_hive_or_model_provider_breakers(self):
        clock = FakeClock()
        breakers = BreakerRegistry(clock=clock)

        breakers.open(Dependency.GITHUB, 20, "limited")

        self.assertTrue(breakers.state(Dependency.GITHUB).blocked)
        self.assertFalse(breakers.state(Dependency.HIVE).blocked)
        self.assertFalse(breakers.state(Dependency.MODEL_PROVIDER).blocked)

    def test_open_breaker_exposes_blocked_and_retry_at_then_closes_after_pause(self):
        clock = FakeClock(100)
        breakers = BreakerRegistry(clock=clock)

        breakers.open(Dependency.GITHUB, 5, "limited")
        state = breakers.state(Dependency.GITHUB)

        self.assertTrue(state.blocked)
        self.assertEqual(state.retry_at, 105)

        clock.now = 106
        state = breakers.state(Dependency.GITHUB)

        self.assertFalse(state.blocked)
        self.assertIsNone(state.retry_at)

    def test_deadline_kills_the_whole_process_group(self):
        child = """
import os
import sys
import time
pid = os.fork()
if pid == 0:
    time.sleep(30)
else:
    print(pid, flush=True)
    time.sleep(30)
"""

        with self.assertRaises(GhProcessTimeout) as caught:
            run_process_group([sys.executable, "-c", child], timeout=0.5)

        output = caught.exception.output or ""
        grandchild = int(output.strip().splitlines()[0])
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"grandchild {grandchild} survived the process-group deadline kill")

    def test_dashboard_contains_no_bare_subprocess_run_bypassing_gh_client(self):
        tui_path = Path(__file__).parents[1] / "image" / "tui" / "bluefin_review_tui.py"
        content = tui_path.read_text()
        self.assertFalse(
            bool(re.search(r'subprocess\.run\(\s*\[\s*["\']gh["\']', content)),
            "bluefin_review_tui.py must contain no bare subprocess.run(['gh', ...]) bypassing gh_client",
        )
        gh_body = content.split("def gh(")[1].split("def _run_mutation")[0]
        self.assertNotIn(
            "return subprocess.run",
            gh_body,
            "gh() must delegate to gh_client rather than calling subprocess.run directly",
        )
        mut_body = content.split("def _run_mutation(")[1].split("def fetch_live_review")[0]
        self.assertNotIn(
            "return subprocess.run",
            mut_body,
            "_run_mutation() must delegate to gh_client rather than calling subprocess.run directly",
        )

    def test_mutation_does_not_retry_when_idempotency_not_proven(self):
        runner = QueueRunner(rate_limited(), completed(0, "{}\n"))
        clock = FakeClock()
        client = GhClient(run=runner, clock=clock, sleep=clock.sleep)
        result = client.mutation("pr", "merge", "31", attempts=4, idempotent=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(clock.sleeps), 0)


if __name__ == "__main__":
    unittest.main()
