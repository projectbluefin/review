#!/usr/bin/env python
"""Capture real dashboard screens with local fixtures.

The app is imported from ``image/tui`` and driven with Textual's Pilot.  No
fixture is allowed to contain credentials or prompt text; the same redaction
check is applied to every written artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TUI_DIR = ROOT / "image" / "tui"
DEFAULT_OUT = ROOT / ".cache" / "tui-evidence"
SIZES = {"compact": (80, 24), "standard": (120, 40), "desktop": (180, 52)}
SCENARIOS = (
    "queue", "landing-progress", "ci-failure", "editor-slow-provider",
    "focus-resize", "worker-status",
)
SENSITIVE = re.compile(
    r"(?i)(?:-----BEGIN [^-]+ KEY-----|(?:api[_-]?key|access[_-]?token|secret|password|authorization|bearer)\s*[:=]|(?:sk|gh[pousr]|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,})"
)


def fixture_items() -> list[dict[str, Any]]:
    return [
        {"repository": "projectbluefin/review", "number": 101,
         "title": "fix: queue evidence fixture", "author": "other-user",
         "recommended_action": "review"},
        {"repository": "projectbluefin/review", "number": 102,
         "title": "fix: failed CI fixture", "author": "other-user",
         "recommended_action": "fix-ci"},
        {"repository": "projectbluefin/review", "number": 103,
         "title": "chore: slow provider fixture", "author": "other-user",
         "recommended_action": "investigate"},
    ]


def assert_safe(value: str, label: str) -> None:
    if SENSITIVE.search(value):
        raise RuntimeError(f"refusing sensitive {label}")


def write_safe(path: Path, content: str) -> None:
    assert_safe(content, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fixture_graphql() -> str:
    nodes = []
    evidence = {"review": ("REVIEW_REQUIRED", "MERGEABLE", "SUCCESS"),
                "fix-ci": ("REVIEW_REQUIRED", "MERGEABLE", "FAILURE"),
                "investigate": ("REVIEW_REQUIRED", "UNKNOWN", "SUCCESS")}
    for item in fixture_items():
        review, mergeable, checks = evidence[item["recommended_action"]]
        nodes.append({
            "number": item["number"], "title": item["title"],
            "updatedAt": "2026-09-08T00:00:00Z",
            "author": {"login": item["author"]},
            "repository": {"nameWithOwner": item["repository"]},
            "labels": {"nodes": []}, "reviewDecision": review,
            "baseRefOid": "a" * 40, "headRefOid": str(item["number"]).zfill(40),
            "mergeable": mergeable,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks}}}]},
            "reviews": {"nodes": []},
        })
    return json.dumps([{"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}])


def make_gh_stub(directory: Path) -> None:
    script = f'''#!/bin/sh
if [ "$1" = "api" ] && [ "$*" != "" ] && printf '%s' "$*" | grep -q 'user'; then printf '%s\\n' fixture-user; exit 0; fi
if [ "$1" = "api" ] && printf '%s' "$*" | grep -q 'graphql'; then printf '%s\\n' '{fixture_graphql()}'; exit 0; fi
if [ "$1 $2" = "pr list" ]; then printf '%s\\n' '[]'; exit 0; fi
if [ "$1 $2" = "pr view" ]; then printf '%s\\n' '{{}}'; exit 0; fi
exit 0
'''
    path = directory / "gh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def provenance(scenario: str, size_name: str, dimensions: tuple[int, int], command: str,
               image: str | None = None) -> dict[str, Any]:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    image_id = "unavailable"
    if image:
        result = subprocess.run(["podman", "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True)
        image_id = result.stdout.strip() if result.returncode == 0 else "unavailable"
    return {"revision": revision, "image": image or "unavailable", "image_id": image_id,
            "terminal": {"name": size_name, "width": dimensions[0], "height": dimensions[1]},
            "command": command, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repository": str(ROOT), "head": revision, "repository_read_only": True,
            "head_read_only": True, "fixture": scenario}


def png_converter() -> str | None:
    for name in ("rsvg-convert", "magick", "convert", "inkscape"):
        found = shutil.which(name)
        if found:
            return found
    return None


def svg_to_png(svg: Path, png: Path) -> None:
    try:
        import cairosvg  # type: ignore
    except ImportError:
        cairosvg = None
    if cairosvg is not None:
        cairosvg.svg2png(bytestring=svg.read_bytes(), write_to=str(png))
        return
    converter = png_converter()
    if not converter:
        raise RuntimeError("PNG capture unavailable: install a local SVG converter")
    if Path(converter).name == "rsvg-convert":
        command = [converter, str(svg), "-o", str(png)]
    elif Path(converter).name == "inkscape":
        command = [converter, str(svg), "--export-type=png", f"--export-filename={png}"]
    else:
        command = [converter, str(svg), str(png)]
    subprocess.run(command, check=True, capture_output=True, text=True)


async def capture(args: argparse.Namespace) -> list[Path]:
    sys.path.insert(0, str(TUI_DIR.parent))
    import tui.bluefin_review_tui as tui  # type: ignore
    from tui import landing  # type: ignore

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="tui-evidence-") as temporary:
        stub_dir = Path(temporary)
        make_gh_stub(stub_dir)
        old_path = os.environ.get("PATH", "")
        old_state_home = os.environ.get("XDG_STATE_HOME")
        old_instance = os.environ.get("BLUEFIN_REVIEW_INSTANCE")
        os.environ["PATH"] = f"{stub_dir}{os.pathsep}{old_path}"
        os.environ["XDG_STATE_HOME"] = str(stub_dir / "state")
        os.environ["BLUEFIN_REVIEW_INSTANCE"] = "tui-evidence"
        os.environ.pop("HIVE_API_BASE", None)
        try:
            for size_name in args.sizes:
                dimensions = SIZES[size_name]
                for scenario in args.scenarios:
                    app = tui.ReviewDashboard()
                    async with app.run_test(size=dimensions) as pilot:
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        if not app.stops:
                            raise RuntimeError("fixture queue did not reach the real app")
                        apply_scenario(app, scenario, dimensions, landing)
                        await pilot.pause()
                        if scenario == "landing-progress":
                            app.action_agents()
                            await pilot.pause()
                            if not app.query_one("#landing-pause").has_focus:
                                raise RuntimeError(
                                    "landing-progress scenario did not focus the landing controls"
                                )
                        if app.current is not None:
                            app.render_evidence(app.current)
                        stem = f"{scenario}-{dimensions[0]}x{dimensions[1]}"
                        svg = output / f"{stem}.svg"
                        write_safe(svg, app.export_screenshot(title=stem))
                        png = output / f"{stem}.png"
                        svg_to_png(svg, png)
                        artifacts.extend((svg, png))
                        meta = output / f"{stem}.json"
                        data = provenance(scenario, size_name, dimensions, " ".join(sys.argv))
                        data["interaction"] = (
                            "action_agents -> persistent landing control focus"
                            if scenario == "landing-progress"
                            else "real Textual Pilot app with bounded local fixture state"
                        )
                        write_safe(meta, json.dumps(data, indent=2) + "\n")
                        artifacts.append(meta)
        finally:
            os.environ["PATH"] = old_path
            if old_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_state_home
            if old_instance is None:
                os.environ.pop("BLUEFIN_REVIEW_INSTANCE", None)
            else:
                os.environ["BLUEFIN_REVIEW_INSTANCE"] = old_instance
    return artifacts


def apply_scenario(
    app: Any, scenario: str, dimensions: tuple[int, int], landing: Any
) -> None:
    evidence = {
        "review": ("REVIEW_REQUIRED", "MERGEABLE", "SUCCESS"),
        "fix-ci": ("REVIEW_REQUIRED", "MERGEABLE", "FAILURE"),
        "investigate": ("REVIEW_REQUIRED", "UNKNOWN", "SUCCESS"),
    }
    # The capture is a bounded local fixture journey. Keep the details pane's
    # live-shaped state aligned with the queue fixture instead of letting the
    # stubbed GitHub enrichment replace it with an empty response.
    for candidate in app.stops:
        review, mergeable, conclusion = evidence[candidate.action]
        candidate.check_state = (
            "failure" if conclusion == "FAILURE" else "success"
        )
        candidate.live = {
            **candidate.live,
            "repository": candidate.repository,
            "number": candidate.number,
            "headRefOid": candidate.head_sha,
            "state": "OPEN",
            "isDraft": False,
            "mergeable": mergeable,
            "mergeStateStatus": "CLEAN",
            "reviewDecision": review,
            "statusCheckRollup": [{
                "name": "fixture-ci",
                "state": "COMPLETED",
                "conclusion": conclusion,
                "headSha": candidate.head_sha,
            }],
        }
    app.show_evidence = lambda selected, open_decision=False: app.render_evidence(selected)
    app.refresh_rows()
    if scenario == "landing-progress":
        stop = next(
            (candidate for candidate in app.stops if candidate.number == 101),
            app.stops[0],
        )
        task = landing.new_task([stop], "fixture-user")
        task.model = "fixture-model"
        task.process = object()
        task.started = time.monotonic() - 7
        stop.check_state = "pending"
        stop.live = {
            **stop.live,
            "repository": stop.repository,
            "number": stop.number,
            "headRefOid": stop.head_sha,
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [{
                "name": "fixture-ci",
                "state": "IN_PROGRESS",
                "headSha": stop.head_sha,
            }],
        }
        Path(task.status_path).write_text(
            json.dumps(
                {
                    "pr": stop.key,
                    "state": "waiting-ci",
                    "note": "CI is still running",
                    "watch": {
                        "repository": stop.repository,
                        "pull_request": stop.number,
                        "head_sha": stop.head_sha,
                        "run_id": 101,
                        "attempt": 1,
                        "status": "in_progress",
                        "observed_at": time.time() - 2,
                        "deadline": time.time() + 300,
                    },
                    "ts": int(time.time()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        app.landing_queue.append(task)
        app.refresh_rows()
        queue = app.query_one("#queue")
        queue.index = next(
            index for index, candidate in enumerate(app.stops) if candidate is stop
        )
        app.render_evidence(stop)
        app.refresh_status()
        details = str(app.query_one("#details").render())
        if stop.key not in details or "1 pending" not in details:
            raise RuntimeError(
                "landing fixture did not select the active target details"
            )
    elif scenario == "ci-failure":
        app.current.failure = "CI failed · local fixture"
        app.render_evidence(app.current)
    elif scenario == "editor-slow-provider":
        app.source_message = "editor slow provider · local fixture"
        app.refresh_status()
        app.query_one("#details").update("[bold cyan]EDITOR / SLOW PROVIDER[/bold cyan]\n\nLocal provider is slow; dashboard remains responsive.")
    elif scenario == "focus-resize":
        app.query_one("#context-pane").focus()
        app.query_one("#context").update(f"FOCUS / RESIZE\nTerminal: {dimensions[0]}x{dimensions[1]}\nEvidence panes remain navigable.")
    elif scenario == "worker-status":
        app.hive_workers = [{"login": "fixture-worker", "task": {"repository": "projectbluefin/review", "number": 101}}]
        app.hive_state = "fixture hub · 1 working"
        app.refresh_activity()
        app.refresh_status()


def run_pty(image: str | None, output: Path) -> str:
    if not image:
        return "SKIPPED: no --pty-image supplied"
    if not shutil.which("podman"):
        return "SKIPPED: podman unavailable"
    result = subprocess.run(["podman", "run", "--rm", "-i", "--network=none", image],
                            input="\x03", capture_output=True, text=True, timeout=20)
    combined = result.stdout + result.stderr
    assert_safe(combined, "PTY output")
    path = output / "pty-smoke.txt"
    write_safe(path, combined[:4000])
    return f"{('PASS' if result.returncode == 0 else 'REPORT')} {path}"


def self_check() -> int:
    assert_safe("fixture-user / projectbluefin/review#101", "safe fixture")
    try:
        assert_safe("authorization: bearer fake-token-value", "unsafe fixture")
    except RuntimeError:
        print("self-check: PASS")
        return 0
    print("self-check: FAIL", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUT))
    parser.add_argument("--scenario", dest="scenarios", action="append", choices=SCENARIOS)
    parser.add_argument("--size", dest="sizes", action="append", choices=tuple(SIZES))
    parser.add_argument("--pty-image", help="optional shipped image for a foreground, network-free smoke")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    args.scenarios = args.scenarios or ["queue"]
    args.sizes = args.sizes or list(SIZES)
    try:
        artifacts = asyncio.run(capture(args))
        pty = run_pty(args.pty_image, Path(args.output).resolve())
        print("PTY:", pty)
        print("ARTIFACTS:")
        for artifact in artifacts:
            print(artifact)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"capture failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
