<p align="center">
  <img alt="Personal CRM" src="template.svg" width="480">
</p>

# Personal CRM

<p align="center">
  <a href="https://studio.imbue.com/open?git_url=https://github.com/kanjun/personal-crm"><img alt="Open in Imbue Studio" height="64" src="https://img.shields.io/badge/Open%20in%20Imbue%20Studio-D8D1C0?style=for-the-badge"></a>
</p>

Didn't work? Create a Studio workspace and paste this to your agent:
` /use-template https://github.com/kanjun/personal-crm`

## Why you care

Find the person you remember from a meeting, recover the conversation that introduced you, and see the history without searching several separate apps. Your corrections stay yours when the archive refreshes.

## How to use it

1. Connect your Google account with read access to Gmail, Calendar and Contacts. Your agent helps configure your own addresses in `data/.apps/crm/settings.json`.
2. Visit CRM to search people or anything in the archive. Select a person for their email and meeting history, notes and original-source links.
3. Correct names, roles or notes directly on a person’s page. Review possible duplicates and decide which people belong in an outreach list.
4. The archive refreshes every two hours. Large first Gmail imports take days at roughly two requests per second; leave enough disk space for the archive and rebuilds.

The template contains no personal archive. You can read what has already been imported when a source is offline.

## Ideas for making it yours

- Add a follow-up view ordered by how long it has been since you spoke.
- Tailor outreach suggestions to a project or community you care about.
- Import another meeting transcript format alongside Granola exports.
- Add your own relationship labels and dossier sections.

## Optional, off by default

One settings file controls the optional features. Copy `system/apps/crm/settings.example.json` to `data/.apps/crm/settings.json`. Every field supports a `CRM_<FIELD>` environment override; lists and booleans use JSON, strings are literal. `CRM_SETTINGS_FILE` selects another settings file.

| Feature | Turn it on | What you supply only then |
| --- | --- | --- |
| Beeper messages | `beeper_enabled=true` | Your Desktop API’s host, port, token and network route. There is no assumed host. |
| Identity cleanup | `cleanup_enabled=true` | An authenticated Codex executable through `codex_command`. Review copied-archive results before applying. The supervised worker stays off until you launch it; `cleanup_protected_path` is optional. |
| Social profile discovery and enrichment | `social_enabled=true` | Bright Data API key and permission for enrichment; OpenAI/Codex authentication for GPT discovery. These are optional credentials, never core activation requirements. Both standalone social routines ship disabled; the enabled feature also runs through refresh. |

The CRM and ordinary refresh work without all three. Configure a writable `dossier_folder` only if you want Markdown dossier export. Hand-kept contacts and local notes paths are optional and empty by default. Self addresses, colleague domains and owner context come from your settings; no publisher accounts or devices are included. Display times follow the container zone unless you set `timezone`.

## What this is

This repository is a published **Imbue Studio template**: a clean, bootable
snapshot of what an agent built, ready to adapt into your own. It is NOT the
generic workspace template -- it is this specific project.

[`template.md`](template.md) is the full manifest -- what it is, how it
works, what it needs to run, and what to adapt -- with the
machine-readable half (recipe, requirements, and the environment it needs
installed) in [`template.toml`](template.toml).
