"""One portable refresh, with bounded stages and no assumed external devices."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import click

from people_archive.settings import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.accounts:
        click.echo("CRM refresh skipped: add your Google accounts to CRM settings.")
        return
    logs = Path(os.environ.get("CRM_DATA_DIR", "data/.apps/crm")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    def run(name: str, seconds: int, *args: str, required: bool = False) -> None:
        with (logs / f"{name}.log").open("a") as output:
            try:
                result = subprocess.run(["timeout", "--kill-after=60", str(seconds), sys.executable, "-m", "people_archive.cli", *args], stdout=output, stderr=subprocess.STDOUT, timeout=seconds + 65)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
            with (logs / "step-status.tsv").open("a") as status:
                status.write(f"{name}\t{int(time.time())}\t{code}\n")
            if code:
                click.echo(f"CRM {name} failed ({code}); see {logs / (name + '.log')}")
                if required:
                    raise SystemExit(code)

    accounts = [part for account in settings.accounts for part in ("--account", account)]
    # This refresh is the sole shipped Gmail writer; a large initial walk is resumable.
    for index, account in enumerate(settings.accounts):
        run(f"pull-gmail-{index + 1}", 900, "pull-gmail", "--account", account)
    run("sync-gmail", 600, "sync-gmail", *accounts, "--time-limit", "180")
    for index, account in enumerate(settings.accounts):
        run(f"pull-contacts-{index + 1}", 600, "pull-contacts", "--account", account)
        run(f"pull-calendar-{index + 1}", 600, "pull-calendar", "--account", account)
    if settings.beeper_enabled:
        run("pull-beeper", 1800, "pull-beeper")
    if settings.notes_folders:
        run("pull-notes", 1200, "pull-notes")
    if settings.apple_notes_selection and settings.apple_notes_container:
        run("pull-apple-notes", 600, "pull-apple-notes", "--selection", settings.apple_notes_selection)
    run("resolve", 1800, "resolve", *accounts, required=True)
    run("judge", 2700, "judge", "--limit", "1500")
    run("resolve", 1800, "resolve", *accounts, required=True)
    if settings.dossier_folder:
        Path(settings.dossier_folder).mkdir(parents=True, exist_ok=True)
        run("write-vault", 900, "write-vault", "--vault", settings.dossier_folder, "--max-tier", "2", required=True)
    if settings.social_enabled:
        run("discover-social-private", 1800, "discover-social-private", "--limit", "1000")
        run("enrich-social", 1800, "enrich-social", "--counts-only", "--limit", "18", "--workers", "1")
    click.echo(json.dumps({"refresh": "finished", "accounts": len(settings.accounts)}))


if __name__ == "__main__":
    main()
