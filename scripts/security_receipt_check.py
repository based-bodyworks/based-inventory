#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def load_event() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return {}
    return json.loads(Path(event_path).read_text())


EVENT = load_event()
BASE_SHA = (
    os.environ.get("BASE_SHA")
    or EVENT.get("pull_request", {}).get("base", {}).get("sha")
    or "origin/main"
)
HEAD_SHA = (
    os.environ.get("HEAD_SHA") or EVENT.get("pull_request", {}).get("head", {}).get("sha") or "HEAD"
)
PR_BODY = (
    Path(os.environ["PR_BODY_FILE"]).read_text()
    if os.environ.get("PR_BODY_FILE")
    else EVENT.get("pull_request", {}).get("body") or ""
)

EXECUTABLE_SURFACE_PATTERNS = [
    re.compile(
        r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|"
        r"pnpm-lock\.yaml|yarn\.lock|bun\.lockb?)$"
    ),
    re.compile(
        r"(^|/)(requirements.*\.txt|pyproject\.toml|poetry\.lock|"
        r"Pipfile|Pipfile\.lock|uv\.lock|go\.mod|go\.sum|Cargo\.toml|"
        r"Cargo\.lock)$"
    ),
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.github/actions/"),
    re.compile(r"(^|/)(Dockerfile|docker-compose\.ya?ml|Makefile|Procfile)$"),
    re.compile(
        r"(^|/)(firebase\.json|apphosting\.ya?ml|vercel\.json|"
        r"netlify\.toml|wrangler\.toml|fly\.toml|render\.ya?ml|"
        r"railway\.json)$"
    ),
    re.compile(r"(^|/)(\.npmrc|\.yarnrc(\.yml)?|\.pypirc|pip\.conf)$"),
    re.compile(r"(^|/)scripts/.*\.(js|cjs|mjs|ts|sh|py|rb|go)$"),
]

RECEIPT_PATTERNS = [
    re.compile(r"##\s*Security receipt", re.I),
    re.compile(r"-\s*\[x\]\s*I reviewed executable-surface changes", re.I),
    re.compile(r"-\s*\[x\]\s*I reviewed install/build behavior", re.I),
    re.compile(r"-\s*\[x\]\s*I reviewed network egress and secrets exposure", re.I),
]


def changed_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{BASE_SHA}...{HEAD_SHA}"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_executable_surface(path: str) -> bool:
    return any(pattern.search(path) for pattern in EXECUTABLE_SURFACE_PATTERNS)


surfaces = [path for path in changed_files() if is_executable_surface(path)]

if not surfaces:
    print("No executable-surface changes detected.")
    sys.exit(0)

print("Executable-surface changes detected:")
for surface in surfaces:
    print(f"- {surface}")

if all(pattern.search(PR_BODY) for pattern in RECEIPT_PATTERNS):
    print("Security receipt found in PR body.")
    sys.exit(0)

print(
    """
Missing completed security receipt in the PR body.

Add this section and check each item after review:

## Security receipt
- [x] I reviewed executable-surface changes
- [x] I reviewed install/build behavior
- [x] I reviewed network egress and secrets exposure
- Notes: <what changed and why it is safe>
""",
    file=sys.stderr,
)
sys.exit(1)
