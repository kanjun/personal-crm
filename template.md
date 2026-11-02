---
title: "Personal CRM"
description: "Everyone you've interacted with, pulled from your email, calendar, contacts and messages into one searchable people list with full history"
thumbnail: "template.svg"
version: v1
format: v2
---

# Personal CRM

## What it is

A private, searchable people list assembled from email, calendar and contacts, with optional messages and notes. Each person has their full interaction history, editable dossier fields and links back to the underlying records. Duplicate review and outreach lists help you maintain the network over time.

## How it works

The `crm` Flask program serves port 8091. The `people-archive` library keeps raw source records in SQLite before deriving identities, search indexes and dossiers. Human-owned identity assertions, person edits and list decisions persist across rebuilds. The app exposes person history, pretty-printed source JSON, original-source links and duplicate review.

The enabled `crm-refresh` routine runs `system/scripts/crm_refresh.sh` every two hours from the checkout root. The settings-driven refresh walks historical Gmail in resumable slices, applies Gmail history/labels, pulls Calendar and Contacts, rebuilds people, runs the subscription Claude judge and rebuilds again. Each source has a time limit and a per-stage log. No separate Gmail job is needed. On an unconfigured installation it exits cleanly and asks for account settings.

The optional `crm_cleanup` supervised program ships with autostart=false. Its modules, reversible ledger and cleanup skill are included, but commands and overlays require cleanup_enabled and cleanup pages return 404 while disabled. Neither optional social routine runs by default. No archive, cleanup evidence, machine state, authentication or opted-in data ships.

The payload is about 70 MB before an adopter imports their own records. The first full Gmail walk on a large mailbox takes days; both the archive and rebuilds can need substantial disk space. Gateway availability is needed for new imports, while existing archived records remain readable offline.

## Recipe

The authoritative include paths, exclusions and published-version modification rules are in [template.toml](template.toml). The added root dependency configuration registers people-archive as a workspace member and the regenerated lockfile includes only the clean base plus CRM dependencies.

## Requirements

- requires_llm: Claude subscription CLI via claude -p (keyless). Optional GPT features use authenticated Codex; no fallback provider. Adopters using keyed Claude must adapt the model calls.

- requires_permission: google-gmail-api / google-gmail-read-all (Gmail message bodies, labels, history and profile).
- requires_permission: google-calendar-api / google-calendar-read-all (Calendar events and history).
- requires_permission: google-people-api / google-people-read-all (Google Contacts and other contacts).

These identifiers are verified against the installed connector permission schemas. Initiate read permissions for the adopter’s configured Google accounts.

- Configure your identities before importing data: Create data/.apps/crm/settings.json from settings.example.json, set accounts and self_addresses, and optionally owner_name, owner_context, colleague_domains and outreach_context.
- Initial archive is empty: Connect Gmail read, Calendar read and Contacts read, then run the enabled refresh. The first full Gmail walk on a large mailbox takes days at approximately two requests per second; rebuilds are disk-heavy.
- OPTIONAL Beeper credentials and route; off by default: Only when beeper_enabled=true, supply your own network route, beeper_host, beeper_port and beeper_token or BEEPER_ACCESS_TOKEN. No activation secret is required while disabled.
- OPTIONAL cleanup authentication; off by default: Only when cleanup_enabled=true, configure codex_command with authenticated Codex (tested command shape 0.154.0); review copied-archive results before enabling the application ledger and manually starting crm_cleanup. cleanup_protected_path is optional.
- OPTIONAL social credentials and permissions; off by default: Only when social_enabled=true, connect a custom Bright Data service with its API key for api.brightdata.com. GPT discovery requires Codex/OpenAI authentication (an OpenAI API key or supported subscription login). These secrets and permissions are optional and must not be requested on activation. Both social routines ship disabled; optional stages can run through refresh.
- OPTIONAL local notes and dossier paths: Leave dossier_folder and hand_kept_contacts_path empty to disable them. Configure notes_folders, notes_device and file-read permission only for notes imports; Apple Notes additionally requires apple_notes_container and an explicit selection JSON. Import Granola only from an explicitly supplied export.

## Optional features: off by default

All settings live in `data/.apps/crm/settings.json`, with an example at `system/apps/crm/settings.example.json`. `CRM_SETTINGS_FILE` selects another file; `CRM_<FIELD>` environment values override individual settings. Lists and booleans use JSON, and strings are literal. `CRM_DATA_DIR` and `CRM_PORT` support isolated verification. Secrets can be injected into the process environment without entering the settings file.

| Feature | Single switch | Setup only if enabled |
| --- | --- | --- |
| Beeper messages | `beeper_enabled=true` | Your own Desktop API network route, host, port and access token; no default host or proxy. |
| GPT identity cleanup | `cleanup_enabled=true` | Authenticated Codex via `codex_command`; inspect a copied archive before applying results. Optionally set `cleanup_protected_path`. The supervised worker stays off until explicitly launched. |
| Social profiles | `social_enabled=true` | Optional Bright Data API key and custom-service permission for enrichment; optional OpenAI/Codex authentication for GPT discovery. Refresh runs the enabled feature; standalone social routines remain disabled until separately chosen. |

These credentials and permissions are OPTIONAL, only for the selected feature. They are deliberately represented as optional adaptation entries rather than activation secrets: this manifest format has no optional activation field. Do not request them when adopting the core CRM. A disabled feature performs no network or model calls.

`dossier_folder` defaults empty, so no dossier is written or copied to another computer. `hand_kept_contacts_path` defaults empty. Colleague domains default empty. `timezone` defaults to the container zone; storage timestamps remain UTC. Owner labels default to “the user”, and self-identification uses only configured accounts and self_addresses.

## Environment

Nothing extra -- runs on the stock workspace environment.

The core uses the stock Python/uv, Claude subscription CLI and latchkey. Optional GPT features need a compatible authenticated Codex executable (tested command shape 0.154.0); provide its path in codex_command only if you enable them. No private package archive is needed and no runtime download occurs.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.


## Publication history

### v1 (2026-11-02) -- Portable personal CRM with configurable sources and optional features off by default

## Adaptation history

Each adopter appends its own dated changes here.
