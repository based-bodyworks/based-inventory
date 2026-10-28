"""Run the real daily wrapper with local workloads and a fake Slack transport.

The wrapper must expose errors, preserve the workload's exit status, and use
Render's environment credentials without requiring a local .env file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "run_daily_velocity.sh"
DAY = "2026-10-27"


@pytest.fixture
def job(tmp_path):
    root = tmp_path / 'job with "quoted" path'
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "daily_sales_velocity.py").write_text(
        "import argparse, os, sys\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--start-date')\n"
        "p.add_argument('--end-date')\n"
        "p.add_argument('--out-dir')\n"
        "p.add_argument('--fresh', action='store_true')\n"
        "args = p.parse_args()\n"
        "assert args.start_date == args.end_date == '2026-10-27'\n"
        "assert args.fresh\n"
        "print('fetch stdout detail')\n"
        "print('fetch stderr detail', file=sys.stderr)\n"
        "code = int(os.environ['FETCH_EXIT'])\n"
        "if not code and os.environ.get('WRITE_XLSX', '1') == '1':\n"
        "    out = Path(args.out_dir)\n"
        "    slug = 'daily-sales-velocity_2026-10-27_2026-10-27'\n"
        "    (out / (slug + '.xlsx')).write_text('workbook fixture')\n"
        "sys.exit(code)\n"
    )
    (scripts / "post_velocity_to_slack.py").write_text(
        "import argparse, os, sys\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--xlsx')\n"
        "p.add_argument('--day')\n"
        "p.add_argument('--channel')\n"
        "args = p.parse_args()\n"
        "assert args.day == '2026-10-27'\n"
        "assert Path(args.xlsx).read_text() == 'workbook fixture'\n"
        "Path(os.environ['POST_MARKER']).touch()\n"
        "print('workbook post detail')\n"
        "sys.exit(int(os.environ.get('POST_EXIT', '0')))\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CURL_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "print(os.environ.get('SLACK_REPLY', '{\"ok\": true}'))\n"
        "sys.exit(int(os.environ.get('CURL_EXIT', '0')))\n"
    )
    curl.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYBIN": sys.executable,
        "VELOCITY_ROOT": str(root),
        "FETCH_EXIT": "17",
        "CURL_CAPTURE": str(tmp_path / "curl.json"),
        "POST_MARKER": str(tmp_path / "posted"),
        "SLACK_CHANNEL": "C_TEST",
    }
    return root, env


def run_job(job, **overrides):
    root, env = job
    result = subprocess.run(
        ["/bin/bash", str(WRAPPER), DAY],
        env=env | overrides,
        capture_output=True,
        text=True,
        timeout=10,
    )
    log = root / "data" / "sales-velocity" / "daily" / "logs" / f"daily-velocity_{DAY}.log"
    return result, log.read_text()


def test_failure_details_reach_render_output_and_local_log(job):
    result, log = run_job(job)
    output = result.stdout + result.stderr
    for detail in ("fetch stdout detail", "fetch stderr detail"):
        assert detail in output
        assert detail in log


def test_missing_credentials_preserve_original_failure(job):
    result, _ = run_job(job)
    assert result.returncode == 17
    assert not Path(job[1]["CURL_CAPTURE"]).exists()
    assert "SLACK_BOT_TOKEN" in result.stdout + result.stderr


def test_render_environment_token_is_used_and_failure_payload_is_encoded(job):
    result, _ = run_job(job, SLACK_BOT_TOKEN="test-render-token")
    assert result.returncode == 17
    args = json.loads(Path(job[1]["CURL_CAPTURE"]).read_text())
    assert "Authorization: Bearer test-render-token" in args
    assert "channel=C_TEST" in args
    assert any("FAILED for 2026-10-27 (exit 17)" in arg for arg in args)
    assert "--max-time" in args
    assert not Path(job[1]["POST_MARKER"]).exists()


@pytest.mark.parametrize("environment_token", ["", "test-render-token"])
def test_local_dotenv_fallback_does_not_override_environment(job, environment_token):
    (job[0] / ".env").write_text("SLACK_BOT_TOKEN=test-local-token\n")
    result, _ = run_job(job, SLACK_BOT_TOKEN=environment_token)
    assert result.returncode == 17
    args = json.loads(Path(job[1]["CURL_CAPTURE"]).read_text())
    expected = environment_token or "test-local-token"
    assert f"Authorization: Bearer {expected}" in args


@pytest.mark.parametrize(
    "overrides",
    [{"CURL_EXIT": "22"}, {"SLACK_REPLY": '{"ok": false, "error": "missing_scope"}'}],
)
def test_failed_slack_alert_is_visible_and_cannot_mask_original_failure(job, overrides):
    result, _ = run_job(job, SLACK_BOT_TOKEN="test-render-token", **overrides)
    assert result.returncode == 17
    assert "failure alert" in (result.stdout + result.stderr).lower()
    assert "test-render-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("post_exit", ["0", "7"])
def test_successful_fetch_keeps_workbook_and_post_failures_remain_nonfatal(job, post_exit):
    result, log = run_job(job, FETCH_EXIT="0", POST_EXIT=post_exit)
    assert result.returncode == 0
    assert Path(job[1]["POST_MARKER"]).exists()
    assert not Path(job[1]["CURL_CAPTURE"]).exists()
    workbook = job[0] / "data" / "sales-velocity" / "daily" / f"daily-velocity_{DAY}.xlsx"
    assert workbook.read_text() == "workbook fixture"
    assert "workbook post detail" in result.stdout + result.stderr
    assert "workbook post detail" in log


def test_missing_workbook_is_still_a_failed_run(job):
    result, _ = run_job(job, FETCH_EXIT="0", WRITE_XLSX="0")
    assert result.returncode == 1
    assert "no xlsx produced" in result.stdout + result.stderr
    assert not Path(job[1]["POST_MARKER"]).exists()
