# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [2.0.0] - 2026-05-27

### Added
- Docker/GHCR multi-arch build workflow (`linux/amd64`, `linux/arm64`) — pre-built images
  at `ghcr.io/njhughes-01/biqu-panda-breath-mod`
- `cc2_connector.py` — Elegoo Centauri Carbon 2 MQTT bridge; publishes 7 HA sensors
  (nozzle/bed/chamber temps, nozzle/bed targets, print status, progress) via autodiscovery
- `panda_web.py` — browser-based control dashboard on port 8088; replaces the upstream
  PySide6 desktop GUI concept with a dependency-free HTTP+MQTT interface
- `docker-compose.cc2.yml` — Compose override for CC2 users; standard users run only
  `docker-compose.yml` and never need the CC2 backend
- `generate_config.py` — testable Python module for `panda_config.json` generation
- Test suite (`pytest`) covering config generation, CC2 message parsing, web endpoints,
  and CC2_IP conditional integration
- `SETUP.md` — full setup guide with both deployment paths, env var reference tables,
  TLS documentation, and troubleshooting
- Runtime TLS cert generation via Docker volume (`panda_certs`) — no manual cert setup

### Changed
- `docker-compose.yml` switched from local build to GHCR image pull
- `docker-entrypoint.sh` config generation refactored to call `generate_config.py`
- `CC2_IP` env var gates CC2 MQTT subscription and bed-temp source in `Panda.py`;
  standard deployments (no `CC2_IP`) behave identically to upstream

### Fixed
- German HA entity names and log strings translated to English
- paho-mqtt v2 callback API (`CallbackAPIVersion.VERSION2`, `ReasonCode.is_failure`)
- `panda_web` button state now reflects live MQTT state (mode/power/slicer buttons)
- HA REST API bed temp fetch skipped when CC2 MQTT data is available
