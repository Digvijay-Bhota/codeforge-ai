"""CF-07 regression tests: Docker Compose network exposure and credentials.

docker-compose.yml used to publish PostgreSQL (with codeforge/codeforge) and
Redis (with no authentication) on every host interface.  Redis holds the job
queue, OAuth state and the repository-permission cache that authorization
relies on, so anyone who could reach it could grant themselves repository
access.  These tests pin the hardened development defaults: no published
database/Redis ports (loopback only, opt-in), required strong credentials
checked by compose-guard, and Redis authentication enforced by the server.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "docker-compose.yml"
DEV_PORTS = ROOT / "docker-compose.dev-ports.yml"
APP_SERVICES = ("api", "worker", "dispatcher")


class _StrictLoader(yaml.SafeLoader):
    """A YAML loader that rejects duplicate mapping keys (safe_load keeps the last one)."""


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
    duplicates = {k for k in keys if keys.count(k) > 1 and k != "<<"}
    if duplicates:
        raise AssertionError(f"duplicate YAML keys {sorted(map(str, duplicates))} at line {node.start_mark.line + 1}")
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load(path: Path) -> dict[str, Any]:
    data = yaml.load(path.read_text(), Loader=_StrictLoader)  # nosec B506 - SafeLoader subclass
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load(COMPOSE)


def services(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]


# ── Network exposure ───────────────────────────────────────────────────────────


def test_database_and_redis_publish_no_host_ports(compose: dict[str, Any]) -> None:
    for name in ("postgres", "redis"):
        assert "ports" not in services(compose)[name], name


@pytest.mark.parametrize("path", [COMPOSE, DEV_PORTS], ids=["base", "dev-ports"])
def test_every_published_port_is_bound_to_loopback(path: Path) -> None:
    for name, svc in load(path)["services"].items():
        for mapping in svc.get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), f"{path.name}: {name} publishes {mapping}"


@pytest.mark.parametrize("path", [COMPOSE, DEV_PORTS], ids=["base", "dev-ports"])
def test_compose_files_have_no_duplicate_keys(path: Path) -> None:
    """A duplicated key would silently override an earlier, hardened value."""
    load(path)
    with pytest.raises(AssertionError, match="duplicate YAML keys"):
        yaml.load("a: 1\na: 2\n", Loader=_StrictLoader)  # nosec B506


def test_dev_ports_override_only_adds_loopback_database_and_redis() -> None:
    override = load(DEV_PORTS)["services"]
    assert set(override) == {"postgres", "redis"}
    assert override["postgres"]["ports"] == ["127.0.0.1:5433:5432"]
    assert override["redis"]["ports"] == ["127.0.0.1:6379:6379"]


# ── Credentials ────────────────────────────────────────────────────────────────


def test_no_credentials_are_hard_coded() -> None:
    text = COMPOSE.read_text()
    assert "codeforge:codeforge" not in text
    # Every YAML key that sets a password takes it from the environment.
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        values = re.findall(rf"^\s*{key}:\s*(.*)$", text, flags=re.MULTILINE)
        assert values and set(values) == {f"${{{key}:-}}"}, (key, len(values))


def test_postgres_takes_its_password_from_the_environment(compose: dict[str, Any]) -> None:
    assert services(compose)["postgres"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-}"


def test_app_services_use_the_same_authenticated_urls(compose: dict[str, Any]) -> None:
    envs = [services(compose)[name]["environment"] for name in APP_SERVICES]
    for key in ("DATABASE_URL", "REDIS_URL"):
        assert len({env[key] for env in envs}) == 1, key
    env = envs[0]
    assert env["DATABASE_URL"] == "postgresql+asyncpg://codeforge:${POSTGRES_PASSWORD:-}@postgres:5432/codeforge"
    assert env["REDIS_URL"] == "redis://:${REDIS_PASSWORD:-}@redis:6379/0"


# ── Redis authentication ───────────────────────────────────────────────────────


def test_redis_server_enforces_its_password(compose: dict[str, Any]) -> None:
    redis = services(compose)["redis"]
    script = "\n".join(redis["command"])
    assert "requirepass" in script and '"$$REDIS_PASSWORD"' in script
    # Started through the image entrypoint so redis-server still drops root.
    assert "exec docker-entrypoint.sh redis-server /tmp/redis.conf" in script
    assert "umask 077" in script and "chown redis:redis /tmp/redis.conf" in script
    assert redis["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:-}"


def test_redis_health_check_authenticates(compose: dict[str, Any]) -> None:
    test = services(compose)["redis"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    # REDISCLI_AUTH keeps the password off the redis-cli command line; without
    # authentication, PING returns NOAUTH and the grep fails.
    assert test[1] == 'REDISCLI_AUTH="$$REDIS_PASSWORD" redis-cli ping | grep -q PONG'


def test_postgres_health_check_proves_the_configured_password(compose: dict[str, Any]) -> None:
    test = services(compose)["postgres"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    # Not pg_isready (no login) and not 127.0.0.1 (trusted without a password by
    # the image's pg_hba.conf): log in over the network address with the password.
    assert test[1] == (
        'PGPASSWORD="$$POSTGRES_PASSWORD" psql -h postgres -U codeforge -d codeforge '
        "-tAc 'select 1' >/dev/null"
    )


def test_no_service_can_start_unless_the_credential_check_succeeds(compose: dict[str, Any]) -> None:
    """Every service reaches compose-guard through completed/healthy dependencies."""
    svcs = services(compose)

    def gated(name: str, seen: frozenset[str] = frozenset()) -> bool:
        deps = svcs[name].get("depends_on", {})
        if deps.get("compose-guard") == {"condition": "service_completed_successfully"}:
            return True
        return any(
            cond.get("condition") in ("service_healthy", "service_completed_successfully")
            and dep not in seen and gated(dep, seen | {name})
            for dep, cond in deps.items()
        )

    for name in svcs:
        if name != "compose-guard":
            assert gated(name), name


def test_database_and_redis_wait_for_the_credential_check(compose: dict[str, Any]) -> None:
    for name in ("postgres", "redis"):
        assert services(compose)[name]["depends_on"] == {
            "compose-guard": {"condition": "service_completed_successfully"}
        }, name
    for name in APP_SERVICES:
        deps = services(compose)[name]["depends_on"]
        assert deps["postgres"] == {"condition": "service_healthy"}
        assert deps["redis"] == {"condition": "service_healthy"}


# ── compose-guard behaviour ────────────────────────────────────────────────────


def run_guard(compose: dict[str, Any], **env: str) -> subprocess.CompletedProcess[str]:
    """Run compose-guard's script as the container would (Compose turns $$ into $)."""
    guard = services(compose)["compose-guard"]
    assert guard["entrypoint"] == ["sh", "-c"]
    script = guard["command"][0].replace("$$", "$")
    sh = shutil.which("sh")
    assert sh is not None
    return subprocess.run(  # nosec B603
        [sh, "-c", script], env={"PATH": os.environ.get("PATH", "")} | env,
        capture_output=True, text=True, timeout=10, check=False,
    )


STRONG = secrets.token_urlsafe(32)


@pytest.mark.parametrize(
    "postgres,redis",
    [
        ("", ""),
        ("", STRONG),
        (STRONG, ""),
        ("short-but-safe", STRONG),
        ("codeforge", STRONG),
        ("change-me-to-something-long", STRONG),
        ("password-password-password", STRONG),
        ("example-value-1234567890", STRONG),
        (STRONG, "secret-value-123456789"),
        ("has@reserved:chars/in-it?x", STRONG),
        ("has a space in the middle!", STRONG),
        # Found in review: all of these passed the original 16-character guard.
        ("a" * 40, STRONG),
        ("abcabcabcabcabcabcabcabcabcabcabcabc", STRONG),
        ("CodeForge-local-dev-database-key-2024", STRONG),
        ("PASSWORD-PASSWORD-PASSWORD-PASSWORD-1", STRONG),
        ("Change-Me-Before-Use-Please-Now-12345", STRONG),
        ("Zk3pQ9wL2mXv7RtY-ExAmPlE-8sNcF4hJ6dG", STRONG),
        (secrets.token_urlsafe(16), STRONG),
        ("qwertyuiopasdfghjklzxcvbnm1234", STRONG),
    ],
    ids=["both-missing", "postgres-missing", "redis-missing", "too-short", "old-default",
         "change-me", "password", "example", "secret", "reserved-chars", "space",
         "repeated-char", "repeated-pattern", "codeforge-mixed-case", "PASSWORD-upper",
         "Change-Me-capitalized", "embedded-placeholder", "token_urlsafe-16", "31-char-keyboard"],  # fmt: skip
)
def test_guard_refuses_missing_weak_or_unsafe_credentials(
    compose: dict[str, Any], postgres: str, redis: str
) -> None:
    env = {k: v for k, v in (("POSTGRES_PASSWORD", postgres), ("REDIS_PASSWORD", redis)) if v}
    result = run_guard(compose, **env)
    assert result.returncode != 0
    assert "compose-guard:" in result.stderr
    for value in (postgres, redis):
        if value:
            assert value not in result.stderr + result.stdout, "guard echoed a credential"


@pytest.mark.parametrize("generate", [lambda: secrets.token_urlsafe(32), lambda: secrets.token_hex(16)],
                         ids=["token_urlsafe-32", "token_hex-16"])  # fmt: skip
def test_guard_accepts_generated_credentials(compose: dict[str, Any], generate: Any) -> None:
    result = run_guard(compose, POSTGRES_PASSWORD=generate(), REDIS_PASSWORD=STRONG)
    assert (result.returncode, result.stderr) == (0, "")


# ── Environment template and documentation ─────────────────────────────────────


def env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_env_example_ships_no_usable_compose_credentials(compose: dict[str, Any]) -> None:
    """Copying .env.example unchanged must not start the stack with a known password."""
    example = env_example()
    assert example["POSTGRES_PASSWORD"] == "" and example["REDIS_PASSWORD"] == ""
    assert run_guard(compose, POSTGRES_PASSWORD=example["POSTGRES_PASSWORD"],
                     REDIS_PASSWORD=example["REDIS_PASSWORD"]).returncode != 0  # fmt: skip
    assert "codeforge:codeforge" not in (ROOT / ".env.example").read_text()
    assert example["REDIS_URL"].startswith("redis://:<")  # password placeholder, not unauthenticated


def test_env_files_are_gitignored() -> None:
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


def test_documentation_states_requirements_and_limits() -> None:
    deployment = (ROOT / "docs" / "deployment.md").read_text()
    readme = (ROOT / "README.md").read_text()
    for needle in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "local development only",
                   "not a production deployment", "docker-compose.dev-ports.yml", "compose-guard",
                   "shorter than 32 characters", "cannot prove a value is random"):  # fmt: skip
        assert needle in deployment, needle
    for needle in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "127.0.0.1", "local development only"):
        assert needle in readme, needle
    header = COMPOSE.read_text().splitlines()[0]
    assert "LOCAL DEVELOPMENT ONLY" in header


# ── Compose itself (when the CLI is available, as in CI) ──────────────────────


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("with_credentials", [False, True])
def test_compose_file_is_valid(tmp_path: Path, with_credentials: bool) -> None:
    """Valid with or without credentials: `docker compose build` must work in CI."""
    env_file = tmp_path / "compose.env"
    env_file.write_text(
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\nREDIS_PASSWORD={STRONG}\n"
        if with_credentials else ""
    )
    for files in (["-f", str(COMPOSE)], ["-f", str(COMPOSE), "-f", str(DEV_PORTS)]):
        result = subprocess.run(  # nosec B603 B607
            ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_file),
             *files, "config", "--quiet"],
            capture_output=True, text=True, timeout=60, check=False,
            env={k: v for k, v in os.environ.items() if k not in ("POSTGRES_PASSWORD", "REDIS_PASSWORD")},
        )  # fmt: skip
        if result.returncode != 0 and "is not a docker command" in result.stderr:
            pytest.skip("docker compose plugin not available")
        assert result.returncode == 0, result.stderr
