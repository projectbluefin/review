import os
import random
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

DEFAULT_READ_TIMEOUT: float = 60.0
DEFAULT_MUTATION_TIMEOUT: float = 60.0


class Dependency(Enum):
    GITHUB = "github"
    HIVE = "hive"
    MODEL_PROVIDER = "model_provider"


def _resolve_dependency(dep: Dependency | str) -> Dependency:
    if isinstance(dep, Dependency):
        return dep
    try:
        return Dependency(dep)
    except ValueError:
        raise ValueError(f"Unknown dependency: {dep}") from None


@dataclass(frozen=True)
class BreakerState:
    blocked: bool
    retry_at: float | None = None
    reason: str = ""


class GhProcessTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, output=None, stderr=None):
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.killed_process_group = True


class BreakerRegistry:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._states = {
            dependency: BreakerState(False) for dependency in Dependency
        }

    def open(self, dependency: Dependency | str, seconds: float, reason: str = "") -> None:
        dep = _resolve_dependency(dependency)
        retry_at = self._clock() + max(0.0, seconds)
        state = self.state(dep)
        if state.retry_at is not None:
            retry_at = max(retry_at, state.retry_at)
        self._states[dep] = BreakerState(True, retry_at, reason)

    def close(self, dependency: Dependency | str) -> None:
        dep = _resolve_dependency(dependency)
        self._states[dep] = BreakerState(False)

    def reset_all(self) -> None:
        for dep in Dependency:
            self._states[dep] = BreakerState(False)

    def state(self, dependency: Dependency | str) -> BreakerState:
        dep = _resolve_dependency(dependency)
        state = self._states[dep]
        if state.retry_at is not None and self._clock() >= state.retry_at:
            state = BreakerState(False)
            self._states[dep] = state
        return state


def compute_backoff(
    attempt: int,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: Callable[[float], float] | None = None,
) -> float:
    capped = min(cap, base * (2 ** attempt))
    half = capped / 2
    j = jitter(half) if jitter else random.uniform(0, half)
    return half + j


class GhClient:
    def __init__(
        self,
        *,
        run: Callable[[Sequence[str], float], subprocess.CompletedProcess] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] | None = None,
        breakers: BreakerRegistry | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
    ):
        self._run = run or run_process_group
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter or (lambda limit: random.uniform(0, limit))
        self._breakers = breakers or BreakerRegistry(clock=clock)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    def read(self, *args: str, timeout: float = DEFAULT_READ_TIMEOUT, attempts: int = 4) -> subprocess.CompletedProcess:
        return self._call(args, timeout=timeout, attempts=attempts, retry=True, is_mutation=False)

    def mutation(
        self,
        *args: str,
        timeout: float = DEFAULT_MUTATION_TIMEOUT,
        attempts: int = 4,
        idempotent: bool = False,
    ) -> subprocess.CompletedProcess:
        return self._call(args, timeout=timeout, attempts=attempts, retry=idempotent, is_mutation=True)

    def state(self, dependency: Dependency | str = Dependency.GITHUB) -> BreakerState:
        return self._breakers.state(dependency)

    def _call(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        attempts: int,
        retry: bool,
        is_mutation: bool = False,
    ) -> subprocess.CompletedProcess:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        _validate_timeout(timeout)
        command = _gh_command(args)
        final_attempt = max(1, attempts)
        for attempt in range(final_attempt):
            self._wait_for_breaker()
            try:
                result = self._run(command, timeout)
            except GhProcessTimeout:
                # Ambiguous timeout on a mutation is NEVER retried
                if (not is_mutation) and retry and attempt < final_attempt - 1:
                    self._pause(self._backoff_delay(attempt), "GitHub subprocess timed out")
                    continue
                raise
            rate_limited = is_rate_limited(result)
            delay = parse_rate_limit(result, self._clock) if rate_limited else None
            if rate_limited and delay is None:
                delay = self._backoff_delay(attempt)
            result = _strip_included_headers(result) if args and args[0] == "api" else result
            if not rate_limited:
                return result
            self._breakers.open(Dependency.GITHUB, delay, "GitHub rate limit")
            if retry and attempt < final_attempt - 1:
                self._sleep(delay)
                continue
            return result
        return result

    def _wait_for_breaker(self) -> None:
        state = self._breakers.state(Dependency.GITHUB)
        if state.blocked and state.retry_at is not None:
            self._sleep(max(0.0, state.retry_at - self._clock()))

    def _pause(self, delay: float, reason: str) -> None:
        self._breakers.open(Dependency.GITHUB, delay, reason)
        self._sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        return compute_backoff(
            attempt=attempt,
            base=self._backoff_base,
            cap=self._backoff_cap,
            jitter=self._jitter,
        )


def run_process_group(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    _validate_timeout(timeout)
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_process_group(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise GhProcessTimeout(command, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _validate_timeout(timeout: float) -> None:
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("timeout must be a positive deadline in seconds")


def _kill_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _gh_command(args: Sequence[str]) -> list[str]:
    command = ["gh", *args]
    if args and args[0] == "api" and "--include" not in args and "-i" not in args:
        command.insert(2, "--include")
    return command


def parse_rate_limit(
    result: subprocess.CompletedProcess,
    clock: Callable[[], float] = time.time,
) -> float | None:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    if result.returncode == 0 or not _looks_rate_limited(text):
        return None
    retry_after = _header(text, "retry-after")
    if retry_after is None:
        match = re.search(r"(?im)\bretry[- ]after[:\s]+(\d+)", text)
        if match:
            retry_after = match.group(1)
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    reset = _header(text, "x-ratelimit-reset")
    if reset is not None:
        try:
            return max(0.0, float(reset) - clock())
        except ValueError:
            pass
    return None


def is_rate_limited(result: subprocess.CompletedProcess) -> bool:
    return result.returncode != 0 and _looks_rate_limited(
        f"{result.stdout or ''}\n{result.stderr or ''}"
    )


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return (
        "rate limit" in lowered
        or "abuse detection" in lowered
        or "too many requests" in lowered
        or bool(re.search(r"(?im)^x-ratelimit-remaining:\s*0\s*$", text))
        or bool(re.search(r"(?im)\bretry[- ]after[:\s]", text))
        or bool(re.search(r"(?im)\bhttp(?:/\S+)?\s+429\b|\(http\s+429\)", text))
    )


def _header(text: str, name: str) -> str | None:
    match = re.search(rf"(?im)\b{re.escape(name)}:\s*([^\r\n,;]+)", text)
    return match.group(1).strip() if match else None


def _strip_included_headers(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    lines = (result.stdout or "").splitlines(keepends=True)
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("[HTTP/", ",HTTP/")):
            kept.append(line[0])
            line = line[1:]
        if line.startswith("HTTP/"):
            i += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            if i < len(lines):
                i += 1
            continue
        kept.append(line)
        i += 1
    return subprocess.CompletedProcess(result.args, result.returncode, "".join(kept), result.stderr)


# Module-level defaults and convenience entry points for minimal-churn wiring
default_breakers = BreakerRegistry()
default_client = GhClient(breakers=default_breakers)


def gh(*args: str, timeout: float = DEFAULT_READ_TIMEOUT, attempts: int = 4) -> subprocess.CompletedProcess:
    return default_client.read(*args, timeout=timeout, attempts=attempts)


def gh_mutation(
    *args: str,
    timeout: float = DEFAULT_MUTATION_TIMEOUT,
    attempts: int = 4,
    idempotent: bool = False,
) -> subprocess.CompletedProcess:
    return default_client.mutation(*args, timeout=timeout, attempts=attempts, idempotent=idempotent)


def run_mutation(
    command: Sequence[str],
    timeout: float = DEFAULT_MUTATION_TIMEOUT,
    idempotent: bool = False,
) -> subprocess.CompletedProcess:
    if command and command[0] == "gh":
        return default_client.mutation(*command[1:], timeout=timeout, idempotent=idempotent)
    return run_process_group(command, timeout=timeout)


def get_breaker(dependency: Dependency | str = Dependency.GITHUB) -> BreakerState:
    return default_breakers.state(dependency)
