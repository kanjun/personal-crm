"""Locate the optional Codex executable without downloading at runtime."""
import shutil
from pathlib import Path

from people_archive.cleanup_models import ProviderUnavailable
from people_archive.settings import load_settings


def private_codex_command() -> str:
    """Resolve the executable chosen for optional cleanup and social discovery."""
    resolved = shutil.which(load_settings().codex_command)
    if not resolved or not Path(resolved).is_file():
        raise ProviderUnavailable("Optional CRM GPT features require a configured Codex executable and authentication")
    return resolved
