"""Assertions that run only inside the canonical sanitized harness."""

import os
import subprocess
from pathlib import Path

import pytest


HARNESS = Path(__file__).parents[1] / "scripts" / "harness-check.sh"


def test_canonical_harness_exposes_only_synthetic_runtime_configuration():
    if os.getenv("HARNESS_SANITIZED") != "1":
        pytest.skip("environment assertions run through scripts/harness-check.sh")

    for inherited_name in (
        "AWS_SECRET_ACCESS_KEY",
        "B1N428_PRODUCTION_SENTINEL",
        "DATABASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HTTPS_PROXY",
        "RAILWAY_TOKEN",
    ):
        assert inherited_name not in os.environ

    sandbox_root = Path(os.environ["HARNESS_SANDBOX_ROOT"])
    assert Path(os.environ["HOME"]) == sandbox_root / "home"
    assert Path(os.environ["TMPDIR"]) == sandbox_root / "tmp"
    assert Path(os.environ["XDG_CACHE_HOME"]) == sandbox_root / "cache"
    assert Path(os.environ["UV_CACHE_DIR"]) == sandbox_root / "uv-cache"
    assert os.environ["BACKEND_URL"] == "http://127.0.0.1:9"
    assert os.environ["RPC_URL"] == "http://127.0.0.1:9"
    assert os.environ["HEDGE_MODE"] == "simulate"
    assert os.environ["TRADE_LOG_PATH"] == "/dev/null"
    assert not (Path(__file__).parents[1] / "data" / "trade_history.jsonl").exists()


def test_harness_cleans_sandbox_when_setup_fails():
    existing = set(Path("/tmp").glob("marketmaker-harness.*"))
    child_env = os.environ.copy()
    child_env["HARNESS_TEST_FORCE_SETUP_FAILURE"] = "1"

    result = subprocess.run(
        [str(HARNESS), "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=child_env,
    )

    assert result.returncode != 0
    assert "forced setup failure" in result.stderr
    assert set(Path("/tmp").glob("marketmaker-harness.*")) == existing
