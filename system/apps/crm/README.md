# Personal CRM

A searchable people list, person history, source records, duplicate review, editable dossiers and outreach lists. The Flask app runs on port 8091 and uses `data/.apps/crm/archive.sqlite`. Raw source records and human-owned identity assertions, list decisions and person edits persist independently of rebuilt people tables.

Copy `settings.example.json` to `data/.apps/crm/settings.json`, then set `accounts` and `self_addresses`. The shared settings loader supports `CRM_SETTINGS_FILE`, `CRM_DATA_DIR`, and `CRM_<FIELD>` environment overrides. Boolean and list overrides are JSON; strings are literal. `colleague_domains` defaults to an empty list. Display `timezone` defaults to the container zone; raw timestamps remain UTC.

`system/routines/crm-refresh.toml` runs `system/scripts/crm_refresh.sh` every two hours. Refresh walks Gmail incrementally, synchronizes labels/history, pulls Calendar and Contacts, resolves people and runs the Claude judge. Run it once with `uv run --no-sync bash system/scripts/crm_refresh.sh`. Large first Gmail walks take days at about two requests per second; rebuilds use substantial disk space. The archive is not shipped.

Optional, off by default:

- Beeper: set `beeper_enabled=true`, `beeper_host`, `beeper_port`, and `beeper_token` (or `BEEPER_ACCESS_TOKEN` in the process environment). Supply a network route to your own Beeper Desktop API; no host or proxy is assumed. `beeper_scheme` and `beeper_proxy_url` support your route.
- Cleanup: set `cleanup_enabled=true` to reveal cleanup views and permit cleanup commands. Configure `codex_command` with authenticated Codex and optionally `cleanup_protected_path`. Follow the `crm-cleanup` instructions before manually launching the supervised worker; its autostart stays false. No publisher approval or ledger is inherited.
- Social discovery/enrichment: set `social_enabled=true`. Refresh then runs discovery from existing mail and profile enrichment; both standalone social routines ship disabled. Configure Bright Data through latchkey only if using enrichment, and OpenAI/Codex authentication only if using GPT discovery. These are optional credentials. The CRM and ordinary refresh need neither.

Dossier export is off until `dossier_folder` names a writable directory; no remote copy is attempted. `hand_kept_contacts_path` optionally names a tab-separated contacts file. Notes import needs explicit `notes_folders` entries (`label`, `path_under_users`) and an optional `notes_device`; Apple Notes needs `apple_notes_container` and `apple_notes_selection` (a JSON selection with device, folder, since, exclude_ids). File permissions are requested only when a notes source is configured. Granola imports accept an explicit input file through `people-archive import-granola --help`.

Set `owner_name`, `owner_context` and `outreach_context` to personalize prompts and outreach. `CRM_PORT` supports isolated verification.
