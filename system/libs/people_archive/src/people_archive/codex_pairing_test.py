import pytest

from people_archive import codex_pairing
from people_archive.cleanup_models import ProviderUnavailable


def test_missing_optional_codex_has_actionable_error(monkeypatch):
    monkeypatch.setenv("CRM_CODEX_COMMAND", "/missing/crm-codex")
    with pytest.raises(ProviderUnavailable, match="configured Codex"):
        codex_pairing.private_codex_command()


def test_optional_codex_uses_configured_executable(monkeypatch):
    monkeypatch.setenv("CRM_CODEX_COMMAND", "/usr/bin/true")
    assert codex_pairing.private_codex_command() == "/usr/bin/true"
