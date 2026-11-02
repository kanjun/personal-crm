"""Portable CRM settings, shared by the web app, commands, and refresh routine."""

import json
import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class FeatureDisabledError(RuntimeError):
    """A command was invoked for a disabled optional feature."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_name: str = "the user"
    owner_context: str = ""
    accounts: list[str] = Field(default_factory=list)
    self_addresses: list[str] = Field(default_factory=list)
    colleague_domains: list[str] = Field(default_factory=list)
    colleague_categories: list[str] = Field(default_factory=list)
    timezone: str = ""
    dossier_folder: str = ""
    hand_kept_contacts_path: str = ""
    notes_folders: list[dict[str, str]] = Field(default_factory=list)
    notes_device: str = ""
    apple_notes_container: str = ""
    apple_notes_selection: str = ""
    beeper_enabled: bool = False
    beeper_host: str = ""
    beeper_port: int = Field(default=23373, ge=1, le=65535)
    beeper_scheme: str = "http"
    beeper_token: str = ""
    beeper_proxy_url: str = ""
    cleanup_enabled: bool = False
    cleanup_protected_path: str = ""
    codex_command: str = "codex"
    social_enabled: bool = False
    outreach_context: str = ""


def load_settings() -> Settings:
    """Read fresh on each call; CRM_<FIELD> overrides the JSON settings file.

    Lists and booleans use JSON in environment overrides. Secrets may be provided
    by the environment instead of placing them in the settings file.
    """
    path = Path(os.environ.get("CRM_SETTINGS_FILE", str(Path(os.environ.get("CRM_DATA_DIR", "data/.apps/crm")) / "settings.json")))
    values = json.loads(path.read_text()) if path.is_file() else {}
    for name, field in Settings.model_fields.items():
        value = os.environ.get(f"CRM_{name.upper()}")
        if value is not None:
            values[name] = value if field.annotation is str else json.loads(value)
    if not values.get("beeper_token") and os.environ.get("BEEPER_ACCESS_TOKEN"):
        values["beeper_token"] = os.environ["BEEPER_ACCESS_TOKEN"]
    return Settings.model_validate(values)


def display_timezone() -> tzinfo:
    value = load_settings().timezone
    return ZoneInfo(value) if value else datetime.now().astimezone().tzinfo or timezone.utc


def require_feature(name: str) -> None:
    if not getattr(load_settings(), f"{name}_enabled"):
        raise FeatureDisabledError(f"{name.capitalize()} is off. Set {name}_enabled in CRM settings to enable it.")
