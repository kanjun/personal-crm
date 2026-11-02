---
name: crm-cleanup
description: Run optional GPT identity cleanup using private CRM evidence and public professional websites.
---

# CRM cleanup

Optional and off by default. Before using private evidence or public research, confirm the adopter wants cleanup and understands its scope. There is no inherited permission from the publisher.

1. Read `data/.apps/crm/settings.json`. Continue only if `cleanup_enabled` is true. Configure `codex_command` for an authenticated Codex installation (tested command shape: 0.154.0); there is no provider fallback. Keep `crm_cleanup` autostart false.
2. Preserve a consistent SQLite backup. Run first against a copied archive without `--apply`:
   `uv run --no-sync python -m people_archive.cleanup run --archive PATH/archive.sqlite --ledger PATH/cleanup.sqlite --progress PATH/cleanup-progress.json --hours 1 --workers 1`.
3. Inspect extraction, duplicate judgments and public-source proposals separately. All exact private source IDs and quotes are validated; public facts remain proposals until independently verified. Bounded representative evidence does not read every message.
4. After the adopter approves applying reviewed results, enable `apply_enabled=true` in the copied cleanup ledger and exercise a deterministic resolve there. Confirm that person edits, split assertions, saved URLs and dossier notes survive. An optional `cleanup_protected_path` setting names a JSON list of protected dossier pages (`id` per item). It has no default.
5. Only then apply to the real archive. The supervised `crm_cleanup` command resumes its checkpoints, uses two workers, and pauses on provider failures. On constrained hosts use one worker. To launch it manually use `supervisorctl start crm_cleanup`; enabling the setting alone never starts the supervised program. Do not switch providers on errors.
6. Report actual extraction, judgment and research coverage, uncertainty, and remaining counts. Provider-reported tokens do not establish dollar cost.

Undo: `uv run --no-sync python -m people_archive.cleanup revoke --ledger data/.apps/crm/cleanup.sqlite`, optionally `--decision ID`, then `uv run --no-sync people-archive resolve`. Raw archive and human assertions remain unchanged. The adjacent cleanup ledger retains absorbed-ID aliases for saved links.
