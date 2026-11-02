# People archive

The durable raw archive behind Personal CRM. Google calls use the connected account's latchkey gateway; Gmail is paced to roughly two requests per second. The published two-hourly refresh is the sole scheduled Gmail writer. It first walks historical Gmail in resumable slices, then applies history and label changes. Calendar, Contacts, optional messages and notes retain source records before deriving people.

The people tables can be rebuilt. Human-owned identity assertions, person edits and outreach-list decisions cannot: the resolver preserves them and overlays machine results beneath them. Source originals remain accessible through the CRM, including pretty-printed JSON and source links.

Shared configuration is documented in `system/apps/crm/README.md` and `settings.example.json`. All three optional features (Beeper, cleanup, social discovery/enrichment) default off. No personal archive, cleanup progress or device state ships. Dossier writing is disabled without a configured folder.

For manual Gmail sync, use your own configured account: `uv run --no-sync people-archive pull-gmail --account you@example.com`. `pull-gmail` resumes the full walk; `sync-gmail --account you@example.com --time-limit 180` maintains current labels/history. No separate Gmail routine is required. The legacy read-only `gmail_store` API is also included for archive consumers.

Claude judges use the subscription CLI (`claude -p`); optional GPT operations use authenticated Codex without provider fallback. Raw records, provider responses, evidence IDs and human decisions stay in separate durable tables or ledgers.
