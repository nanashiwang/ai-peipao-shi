from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def compose_config() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_api_plain_http_port_binds_localhost_by_default():
    service = compose_config()["services"]["api"]

    assert "${API_BIND_HOST:-127.0.0.1}:${API_PORT:-8000}:8000" in service["ports"]
    assert "8000" in service["expose"]


def test_tls_proxy_publishes_encrypted_entrypoint():
    service = compose_config()["services"]["tls-proxy"]

    assert service["image"].startswith("caddy:")
    assert "${TLS_BIND_HOST:-0.0.0.0}:${TLS_HTTPS_PORT:-9443}:443" in service["ports"]
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in service["volumes"]
    assert "./config/tls:/etc/caddy/certs:ro" in service["volumes"]
    assert service["depends_on"]["api"]["condition"] == "service_healthy"


def test_compose_limits_json_log_growth_and_checks_api_health():
    config = compose_config()
    logging = config["x-json-logging"]

    assert logging["options"]["max-size"] == "${LOG_MAX_SIZE:-10m}"
    assert logging["options"]["max-file"] == "${LOG_MAX_FILE:-5}"
    for name in ("api", "postgres", "tls-proxy"):
        assert config["services"][name]["logging"] == logging

    healthcheck = config["services"]["api"]["healthcheck"]["test"]
    assert "/health" in healthcheck[1]
    assert "data.get('ok')" in healthcheck[1]


def test_caddyfile_terminates_tls_and_proxies_to_api():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert ":443" in caddyfile
    assert "tls /etc/caddy/certs/server.crt /etc/caddy/certs/server.key" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "Strict-Transport-Security" in caddyfile
