import logging
from collections.abc import Iterator

import pytest

from flecs_mcp.config import Config, ConfigError, configure_logging, redact_url

URL = "http://localhost:27750"


def test_defaults() -> None:
    config = Config.from_env({"FLECS_REST_URL": URL})

    assert config == Config(
        rest_url=URL,
        timeout=5.0,
        verify_tls=True,
        allow_mutations=False,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8000,
    )


def test_custom_values() -> None:
    config = Config.from_env(
        {
            "FLECS_REST_URL": "https://game.example:9000/flecs/",
            "FLECS_REST_TIMEOUT": "2.5",
            "FLECS_REST_VERIFY_TLS": "false",
            "FLECS_REST_ALLOW_MUTATIONS": "yes",
            "MCP_LOG_LEVEL": "debug",
            "MCP_TRANSPORT": "HTTP",
            "MCP_HOST": "0.0.0.0",
            "MCP_PORT": "9100",
        }
    )

    assert config.rest_url == "https://game.example:9000/flecs"  # trailing / removed
    assert config.timeout == 2.5
    assert config.verify_tls is False
    assert config.allow_mutations is True
    assert config.log_level == "DEBUG"
    assert config.transport == "http"
    assert config.host == "0.0.0.0"
    assert config.port == 9100


def test_empty_values_are_treated_as_unset() -> None:
    config = Config.from_env(
        {"FLECS_REST_URL": URL, "FLECS_REST_TIMEOUT": "", "MCP_LOG_LEVEL": "  "}
    )

    assert config.timeout == 5.0
    assert config.log_level == "INFO"


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLECS_REST_URL", "http://flecs-app:27750")

    assert Config.from_env().rest_url == "http://flecs-app:27750"


def test_missing_url_explains_what_to_set() -> None:
    with pytest.raises(ConfigError, match=r"FLECS_REST_URL is not set.*27750"):
        Config.from_env({})


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("localhost:27750", "must start with http:// or https://"),
        ("ftp://localhost:27750", "must start with http:// or https://"),
        ("http://", "must include a host"),
        ("http://localhost:notaport", "invalid port"),
        ("http://localhost:99999", "invalid port"),
        ("http://localhost:27750/?x=1", "must not contain a query"),
    ],
)
def test_invalid_url(url: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config.from_env({"FLECS_REST_URL": url})


@pytest.mark.parametrize("timeout", ["abc", "0", "-1", "nan", "inf"])
def test_invalid_timeout(timeout: str) -> None:
    with pytest.raises(ConfigError, match="FLECS_REST_TIMEOUT"):
        Config.from_env({"FLECS_REST_URL": URL, "FLECS_REST_TIMEOUT": timeout})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("ON", True), ("False", False), ("no", False)],
)
def test_boolean_values(raw: str, expected: bool) -> None:
    config = Config.from_env({"FLECS_REST_URL": URL, "FLECS_REST_VERIFY_TLS": raw})

    assert config.verify_tls is expected


@pytest.mark.parametrize(
    "name", ["FLECS_REST_VERIFY_TLS", "FLECS_REST_ALLOW_MUTATIONS"]
)
def test_invalid_boolean(name: str) -> None:
    with pytest.raises(ConfigError, match=f"{name} must be a boolean"):
        Config.from_env({"FLECS_REST_URL": URL, name: "maybe"})


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MCP_LOG_LEVEL", "verbose", "must be one of DEBUG, INFO"),
        ("MCP_TRANSPORT", "sse", "must be 'stdio' or 'http'"),
        ("MCP_PORT", "http", "must be an integer"),
        ("MCP_PORT", "0", "between 1 and 65535"),
    ],
)
def test_invalid_server_settings(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config.from_env({"FLECS_REST_URL": URL, name: value})


def test_credentials_are_redacted_from_display_url_and_errors() -> None:
    config = Config.from_env({"FLECS_REST_URL": "http://admin:s3cret@proxy:8080"})

    assert config.display_url == "http://***@proxy:8080"
    assert redact_url(URL) == URL
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"FLECS_REST_URL": "ftp://admin:s3cret@proxy"})
    assert "s3cret" not in str(excinfo.value)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ["", "fastmcp", "httpx", "httpcore"]
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("level", ["DEBUG", "WARNING"])
def test_configure_logging(level: str) -> None:
    configure_logging(level)

    assert logging.getLogger().level == logging.getLevelName(level)
    assert logging.getLogger("fastmcp").level == logging.getLevelName(level)
    # httpx logs full request URLs at INFO; it must stay quiet at any level.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
