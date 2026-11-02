import json
from types import SimpleNamespace

import pytest

from people_archive import refresh
from people_archive.beeper_pull import BeeperClient
from people_archive.settings import load_settings


def test_defaults_need_no_optional_source_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    settings = load_settings()
    assert settings.accounts == settings.self_addresses == settings.colleague_domains == []
    assert not settings.beeper_enabled and not settings.cleanup_enabled and not settings.social_enabled
    assert not settings.beeper_host and not settings.dossier_folder and not settings.hand_kept_contacts_path


def test_file_settings_and_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"accounts": ["me@example.com"], "beeper_enabled": True, "beeper_host": "device.example"}))
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(path))
    monkeypatch.setenv("CRM_BEEPER_ENABLED", "false")
    assert load_settings().accounts == ["me@example.com"]
    assert load_settings().beeper_host == "device.example"
    assert not load_settings().beeper_enabled


def test_refresh_without_accounts_never_launches_a_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: pytest.fail("An unconfigured refresh must not contact a source"))
    refresh.main()
    assert "add your Google accounts" in capsys.readouterr().out


def test_google_refresh_runs_with_all_optional_features_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRM_ACCOUNTS", '["me@example.com"]')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        # Exercise a failed optional Google source without blocking the resolver.
        return SimpleNamespace(returncode=1 if "pull-calendar" in command else 0)
    monkeypatch.setattr(refresh.subprocess, "run", run)
    refresh.main()
    operations = [command[6] for command in commands]
    assert operations == ["pull-gmail", "sync-gmail", "pull-contacts", "pull-calendar", "resolve", "judge", "resolve"]
    assert all("me@example.com" in command for command in commands if "judge" not in command)
    assert "pull-beeper" not in operations and "enrich-social" not in operations and "write-vault" not in operations
    assert "pull-calendar-1\t" in (tmp_path / "logs/step-status.tsv").read_text()


def test_beeper_disabled_before_any_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    with pytest.raises(RuntimeError, match="Beeper is off"):
        BeeperClient.from_environment()


def test_beeper_enabled_uses_configured_route_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CRM_BEEPER_ENABLED", "true")
    monkeypatch.setenv("CRM_BEEPER_HOST", "example.test")
    monkeypatch.setenv("CRM_BEEPER_PORT", "24444")
    monkeypatch.setenv("CRM_BEEPER_TOKEN", "invented-test-token")
    client = BeeperClient.from_environment()
    assert client.base_url == "http://example.test:24444"
    assert client.proxy_url == ""
    client._client.close()
