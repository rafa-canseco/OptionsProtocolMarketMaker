"""Regression tests for market-maker workspace context routing."""

import shutil
import subprocess
from pathlib import Path

import pytest


CONTEXT_SCRIPT = Path(__file__).parents[1] / "scripts" / "harness-context.sh"
PROTOCOL = Path(__file__).parents[1] / "AGENTS.md"
REQUIRED_WORKSPACE_PATHS = (
    "AGENTS.md",
    "docs/ACTIVE_CONTEXT.md",
    "docs/v2/V2_OPERATING_CONTEXT.md",
    "docs/architecture/REPO_MAP.md",
)


def _create_workspace(tmp_path: Path, repo_relative: str) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    repo = workspace / repo_relative
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CONTEXT_SCRIPT, scripts / CONTEXT_SCRIPT.name)
    (repo / "AGENTS.md").write_text("# Local protocol\n")

    for relative_path in REQUIRED_WORKSPACE_PATHS:
        context_path = workspace / relative_path
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text("context fixture\n")

    router = workspace / "harness" / "bin" / "context"
    router.parent.mkdir(parents=True)
    router.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Context for %s\\n' \"$1\"\n"
        "printf '  docs/ACTIVE_CONTEXT.md\\n'\n"
        "printf '  docs/v2/V2_OPERATING_CONTEXT.md\\n'\n"
        "printf '  docs/architecture/REPO_MAP.md\\n'\n"
        "printf '  marketMaker/AGENTS.md\\n'\n"
    )
    router.chmod(0o755)
    doctor = workspace / "harness" / "bin" / "doctor"
    doctor.write_text("#!/usr/bin/env bash\nprintf 'doctor fixture passed\\n'\n")
    doctor.chmod(0o755)
    return workspace, repo


def _run_context(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", f"scripts/{CONTEXT_SCRIPT.name}", "--check"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "repo_relative",
    ["marketMaker", ".worktrees/ticket", ".worktrees/nested/deep/ticket"],
)
def test_context_router_resolves_normal_and_worktree_layouts(tmp_path, repo_relative):
    workspace, repo = _create_workspace(tmp_path, repo_relative)

    result = _run_context(repo)

    assert result.returncode == 0
    assert f"workspace={workspace}" in result.stdout


def test_context_router_fails_when_workspace_route_is_missing(tmp_path):
    workspace, repo = _create_workspace(tmp_path, ".worktrees/ticket")
    missing = workspace / "docs" / "ACTIVE_CONTEXT.md"
    missing.unlink()

    result = _run_context(repo)

    assert result.returncode == 1
    assert str(missing) in result.stderr


def test_context_router_allows_explicit_standalone_fallback(tmp_path):
    repo = tmp_path / "standalone"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CONTEXT_SCRIPT, scripts / CONTEXT_SCRIPT.name)
    (repo / "AGENTS.md").write_text("# Local protocol\n")

    result = _run_context(repo)

    assert result.returncode == 0
    assert "standalone checkout" in result.stdout


def test_context_router_rejects_standalone_checkout_without_protocol(tmp_path):
    repo = tmp_path / "standalone"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CONTEXT_SCRIPT, scripts / CONTEXT_SCRIPT.name)

    result = _run_context(repo)

    assert result.returncode == 1
    assert str(repo / "AGENTS.md") in result.stderr


def test_documented_doctor_command_resolves_worktree_depth(tmp_path):
    workspace, repo = _create_workspace(tmp_path, ".worktrees/ticket")

    result = subprocess.run(
        [
            "bash",
            "-c",
            'workspace_root="$(./scripts/harness-context.sh --workspace-root)" '
            '&& "$workspace_root/harness/bin/doctor"',
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "doctor fixture passed\n"
    assert workspace.as_posix() not in result.stderr


def test_holdout_boundary_is_independent_of_relative_checkout_depth():
    protocol = PROTOCOL.read_text()

    assert "../options-scenarios" not in protocol
    assert "../harness" not in protocol
    assert "basename is `options-scenarios`" in protocol
