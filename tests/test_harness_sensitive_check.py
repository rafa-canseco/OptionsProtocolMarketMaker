"""Regression tests for the repository-local sensitive-data checker."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[1]
CHECKER = REPO_ROOT / "scripts" / "harness-sensitive-check.sh"
SCANNER = REPO_ROOT / "scripts" / "harness-sensitive-scan.py"


def _initialize_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CHECKER, scripts / CHECKER.name)
    shutil.copy2(SCANNER, scripts / SCANNER.name)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "safe.txt").write_text("safe fixture\n")
    subprocess.run(["git", "add", "safe.txt", "scripts"], cwd=repo, check=True)
    return repo


def _run_checker(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", f"scripts/{CHECKER.name}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _credential_assignment(name: str, value: str) -> str:
    return f'{name}="{value}"\n'


def _assert_path_only_failure(
    result: subprocess.CompletedProcess[str], path: str, value: str
) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert path in output
    assert value not in output


def test_sensitive_checker_accepts_clean_tracked_content(tmp_path):
    repo = _initialize_repo(tmp_path)

    result = _run_checker(repo)

    assert result.returncode == 0


@pytest.mark.parametrize(
    "forbidden_path",
    [
        ".env.staging",
        ".mcp.json",
        "data/history.jsonl",
        "wallet-keypair.json",
    ],
)
def test_sensitive_checker_rejects_forbidden_index_paths(tmp_path, forbidden_path):
    repo = _initialize_repo(tmp_path)
    fixture = repo / forbidden_path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("synthetic fixture\n")
    subprocess.run(["git", "add", "-f", forbidden_path], cwd=repo, check=True)

    result = _run_checker(repo)

    assert result.returncode == 1
    assert forbidden_path in result.stderr


def test_sensitive_checker_reads_staged_content_not_safe_working_copy(tmp_path):
    repo = _initialize_repo(tmp_path)
    credential_name = "API" + "_KEY"
    credential_value = "stage_" + "x" * 32
    fixture = repo / "staged.txt"
    fixture.write_text(_credential_assignment(credential_name, credential_value))
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)
    fixture.write_text("safe working copy\n")

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, credential_value)
    assert "index content" in result.stderr


def test_sensitive_checker_reads_unstaged_tracked_content_separately(tmp_path):
    repo = _initialize_repo(tmp_path)
    credential_name = "AUTH" + "_TOKEN"
    credential_value = "working_" + "y" * 32
    fixture = repo / "working.txt"
    fixture.write_text("safe index copy\n")
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)
    fixture.write_text(_credential_assignment(credential_name, credential_value))

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, credential_value)
    assert "working-tree content" in result.stderr


def _credential_cases() -> list[tuple[str, str, Callable[[], str]]]:
    return [
        ("api-key", "API" + "_KEY", lambda: "api_" + "a" * 32),
        ("service-key", "SERVICE" + "_KEY", lambda: "svc_" + "b" * 32),
        (
            "aws-secret-access-key",
            "AWS" + "_SECRET_ACCESS_KEY",
            lambda: "aws_" + "l" * 32,
        ),
        (
            "provider-access-key",
            "CLOUD" + "_PROVIDER_ACCESS_KEY",
            lambda: "provider_" + "m" * 32,
        ),
        ("supabase-key", "SUPABASE" + "_KEY", lambda: "sb_" + "c" * 32),
        (
            "supabase-service-role",
            "SUPABASE" + "_SERVICE_ROLE_KEY",
            lambda: "role_" + "d" * 32,
        ),
        ("auth-token", "AUTH" + "_TOKEN", lambda: "auth_" + "e" * 32),
        ("access-token", "ACCESS" + "_TOKEN", lambda: "access_" + "f" * 32),
        (
            "refresh-token",
            "REFRESH" + "_TOKEN",
            lambda: "refresh_" + "g" * 32,
        ),
        ("bearer-token", "BEARER" + "_TOKEN", lambda: "bearer_" + "h" * 32),
        (
            "railway-token",
            "RAILWAY" + "_TOKEN",
            lambda: "railway_" + "n" * 32,
        ),
        (
            "github-token",
            "GITHUB" + "_TOKEN",
            lambda: "github_" + "o" * 32,
        ),
        (
            "generic-provider-token",
            "DEPLOY" + "_PROVIDER_TOKEN",
            lambda: "deploy_" + "p" * 32,
        ),
        (
            "client-secret",
            "CLIENT" + "_SECRET",
            lambda: "client_" + "i" * 32,
        ),
        (
            "webhook-secret",
            "WEBHOOK" + "_SECRET",
            lambda: "webhook_" + "j" * 32,
        ),
        (
            "password",
            "DATABASE" + "_PASSWORD",
            lambda: "password_" + "k" * 16,
        ),
        (
            "mnemonic",
            "WALLET" + "_MNEMONIC",
            lambda: " ".join(f"word{chr(97 + index)}" for index in range(12)),
        ),
    ]


@pytest.mark.parametrize(
    ("case_name", "credential_name", "value_factory"),
    _credential_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_sensitive_checker_rejects_common_credential_forms(
    tmp_path,
    case_name: str,
    credential_name: str,
    value_factory: Callable[[], str],
):
    repo = _initialize_repo(tmp_path)
    credential_value = value_factory()
    fixture = repo / f"{case_name}.txt"
    fixture.write_text(_credential_assignment(credential_name, credential_value))
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, credential_value)


def test_sensitive_checker_scans_env_example_content(tmp_path):
    repo = _initialize_repo(tmp_path)
    credential_name = "CLIENT" + "_SECRET"
    credential_value = "example_file_" + "q" * 32
    fixture = repo / ".env.example"
    fixture.write_text(_credential_assignment(credential_name, credential_value))
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, credential_value)


def test_sensitive_checker_rejects_credentialized_url(tmp_path):
    repo = _initialize_repo(tmp_path)
    url_userinfo_value = "urlpass_" + "r" * 24
    url = "https://local-user:" + url_userinfo_value + "@database.invalid/app"
    fixture = repo / "database.txt"
    fixture.write_text("DATABASE" + "_URL=" + url + "\n")
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, url_userinfo_value)


def test_sensitive_checker_rejects_private_key_blocks(tmp_path):
    repo = _initialize_repo(tmp_path)
    header = "-----BEGIN " + "PRIVATE" + " KEY-----"
    footer = "-----END " + "PRIVATE" + " KEY-----"
    block_value = "pem_" + "z" * 48
    fixture = repo / "identity.txt"
    fixture.write_text(f"{header}\n{block_value}\n{footer}\n")
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)

    result = _run_checker(repo)

    _assert_path_only_failure(result, fixture.name, block_value)


def test_sensitive_checker_accepts_documented_placeholders(tmp_path):
    repo = _initialize_repo(tmp_path)
    fixture = repo / ".env.example"
    fixture.write_text(
        "\n".join(
            [
                _credential_assignment("PRIVATE" + "_KEY", "0x...").strip(),
                _credential_assignment("API" + "_KEY", "test-api-key").strip(),
                _credential_assignment("SUPABASE" + "_KEY", "<SUPABASE_KEY>").strip(),
                _credential_assignment("AUTH" + "_TOKEN", "changeme").strip(),
                _credential_assignment(
                    "AWS" + "_SECRET_ACCESS_KEY", "<AWS_SECRET_ACCESS_KEY>"
                ).strip(),
                _credential_assignment(
                    "RAILWAY" + "_TOKEN", "test-railway-token"
                ).strip(),
            ]
        )
        + "\n"
    )
    subprocess.run(["git", "add", fixture.name], cwd=repo, check=True)

    result = _run_checker(repo)

    assert result.returncode == 0, result.stderr
