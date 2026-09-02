# Hermes Profile-aware RAG MCP plugin

This plugin exposes the RAG MCP tools through Hermes' supported plugin API.
It is designed for a multiplex gateway where every employee has a Hermes
Profile and a different RAG PAT.

## Repository layout

- The repository root is mounted as Hermes' `profile_rag_mcp` backend plugin.
- `wechat_provisioner/` contains the pairing, Profile provisioning, unbind,
  and attachment-cleanup integration service.
- `hermes-wechat-provisioner.compose.yaml` is the production Compose override.
- Secrets, employee PATs, Profile state databases, chat history, and temporary
  attachments are runtime data and must never be committed to this repository.

For a fresh server installation, follow [DEPLOYMENT.md](DEPLOYMENT.md). The
plugin and Provisioner must be released together because their cleanup and
offboarding behavior share the same contract.

## Runtime model

- The active Profile PAT is read from `MCP_COMPANY_MCP_API_KEY` for every call.
- The PAT is never added to tool arguments, model context, logs, or shell history.
- The RAG server is called with one stateless JSON-RPC `tools/call` request;
  Hermes conversation state remains durable and Profile-local.
- No MCP session, keepalive task, or per-Profile connection is retained.
- Redirects are rejected so the Authorization header stays on the configured origin.
- New Profile directories and `gateway.profile_routes` changes are loaded on
  the next inbound message without restarting the shared Gateway.
- A route reload failure or a configured route that cannot resolve its target
  is rejected before Agent dispatch instead of falling back to the default
  Profile. Employee MCP tools likewise reject missing or default Profile scope.
- Employee Weixin Profiles receive only the approved company tool policy. The
  plugin does not expose terminal, generic file, Gateway, or Session-control
  tools to the model.

The RAG endpoint defaults to `https://zsk.freshpixxp.com/mcp`. Override it
globally with `PROFILE_RAG_MCP_URL`; set the request timeout with
`PROFILE_RAG_MCP_TIMEOUT_SECONDS` (default: 60 seconds).

For employee onboarding, the plugin also sets Hermes' pending pairing capacity
to 100. Override it with `PROFILE_RAG_MCP_MAX_PENDING_PAIRINGS` (3-500). This
changes only the number of one-hour pending codes; approval attempts and
per-user rate limits remain enforced by Hermes.

## Docker deployment

Mount this directory as a bundled backend so existing and future Profiles load
it automatically:

```yaml
services:
  gateway:
    volumes:
      - /path/to/hermes-data:/opt/data
      - /path/to/dedicated-hermes-cache:/opt/data/cache
      - /path/to/profile_rag_mcp:/opt/hermes/plugins/profile_rag_mcp:ro
    environment:
      PROFILE_RAG_MCP_URL: https://zsk.freshpixxp.com/mcp
```

`/path/to/dedicated-hermes-cache` should be a dedicated 12 GiB filesystem or
cloud data-disk mount, not merely another directory on the Session database
filesystem. The logical cache limit defaults to 10 GiB, starts LRU cleanup at
70%, and rejects only new attachments at 85%, leaving ordinary Weixin text
chat, root `state.db`, Profile `state.db`, and Profile `.env` writable.

The attachment lifecycle and quota defaults are configurable through:

```env
PROFILE_RAG_MCP_ATTACHMENT_MAX_BYTES=26214400
PROFILE_RAG_MCP_ATTACHMENT_BATCH_MAX_FILES=5
PROFILE_RAG_MCP_ATTACHMENT_SESSION_MAX_FILES=10
PROFILE_RAG_MCP_ATTACHMENT_SESSION_MAX_BYTES=209715200
PROFILE_RAG_MCP_ATTACHMENT_PROFILE_MAX_BYTES=314572800
PROFILE_RAG_MCP_ATTACHMENT_GLOBAL_MAX_BYTES=10737418240
PROFILE_RAG_MCP_ATTACHMENT_CLEANUP_PERCENT=70
PROFILE_RAG_MCP_ATTACHMENT_REJECT_PERCENT=85
PROFILE_RAG_MCP_ATTACHMENT_UNPROCESSED_TTL_SECONDS=1800
PROFILE_RAG_MCP_ATTACHMENT_ANALYZED_IDLE_TTL_SECONDS=3600
PROFILE_RAG_MCP_ATTACHMENT_HARD_TTL_SECONDS=14400
PROFILE_RAG_MCP_ATTACHMENT_CLEANUP_INTERVAL_SECONDS=600
PROFILE_RAG_MCP_ATTACHMENT_METRICS_FILE=/opt/data/state/profile-rag-mcp-attachment-cache.json
```

The process-global maintenance worker runs once at Gateway startup and then
every 10–30 minutes. It records global, Profile, and Session byte usage,
deletion counters, quota rejections, failures, active processing leases, and
cache-disk utilization in the configured metrics JSON file and normal logs.

Temporary-file lifecycle is independent from durable chat history:

- Adapter-captured files are durably registered before the model turn, so a
  Gateway restart cannot strand an untracked file. Pending, failed, rejected,
  or interrupted-processing files expire after 30 minutes.
- Successfully analyzed files expire 60 minutes after last access and every
  temporary source has an absolute four-hour lifetime. The retained active
  attachment keeps only its file name, parser metadata, and bounded excerpt
  after physical deletion; it contains no cache path or reusable grant.
- `/new`, daily Session rotation, Weixin unbind, and employee offboarding mark
  the affected Session or Profile for cleanup without deleting `state.db`.
- A successful knowledge upload deletes only the Hermes local cache after OSS
  confirmation. Formal knowledge objects are never candidates for this
  cleaner. Temporary analysis objects use the RAG service's separate staged
  OSS finalizer.
- Analysis and upload tools hold process-global reference-counted leases in a
  `finally`-safe context. Cleanup skips leased files and retries a pending
  deletion after the final lease is released.
- Physical deletion accepts only registered regular files under the configured
  cache roots whose path, inode, size, and modification time still match. It
  refuses path traversal, symlinks, identity drift, and cross-Profile deletion.

Each Profile keeps only its employee PAT:

```env
MCP_COMPANY_MCP_API_KEY=ragmcp_...
```

The model credential is deliberately shared by all Profiles through the
Gateway process environment:

```env
HERMES_SHARED_MODEL_API_KEY=...
```

Provider URL, model name, and reasoning settings remain in Profile YAML. The
provisioner copies them from one known-working source Profile and rewrites any
inline provider key to `api_key_env: HERMES_SHARED_MODEL_API_KEY`. Never put the
shared model key in a Profile YAML or Profile `.env`.

Remove the old `company-mcp` entry from each Profile's `mcp_servers` config.
Keeping both registrations creates duplicate tool-name collisions.

## Conversation history

Hermes stores each Profile's sessions and FTS5 history in that Profile's
`state.db`. Configure predictable session boundaries while keeping old sessions
searchable:

```yaml
session_reset:
  mode: daily
  idle_minutes: 1440
  at_hour: 4
  notify: false

agent:
  restart_drain_timeout: 60
  restart_after_turn_timeout: 60
```

The active day's messages remain in the current context. At the first message
after the daily boundary Hermes creates a new Session, preserves the previous
Session and its FTS index, and makes it available through `session_search`.
The Gateway keeps the durable `session_key -> session_id` routing index in its
root `state.db`, while transcript reads and writes are scoped to the resolved
employee Profile `state.db`. The plugin preserves that split and scopes only
transcript I/O after Hermes has established the task-local Profile. Session
rotation keeps routing in the root database while ending the matching old row
in that employee's Profile database. A missing, invalid, or mismatched employee
Profile is rejected instead of reading or updating another database.

The current Session does not depend on `session_search`: Hermes injects its
bounded recent transcript on every model turn. If an upstream history load is
unexpectedly empty, the plugin performs one read-only, bounded recovery from
that same Profile and Session. `session_search` remains for older Sessions,
including those retained after the daily 04:00 rotation. Short Chinese
references such as “刚才”“这个”“继续” are resolved from the live Session and
active attachment rather than FTS5 keyword matching.

The restart settings let active turns finish before the container exits;
combine them with a Docker `stop_grace_period` longer than the drain timeout.

## Employee tools and Weixin attachments

Employee Weixin Profiles enable only `web`, `vision`, `session_search`,
`clarify`, and `profile_rag_mcp`. The RAG toolset consumes the MCP server's
released catalog plus two local attachment tools. The plugin does not maintain
a second Tool allowlist: publication and role visibility belong to the original
MCP service, while every `tools/call` remains subject to the same server-side
policy and domain authorization.

The first production catalog deliberately excludes requirement and task Tools.
An employee PAT receives 19 identity, directory, knowledge, transfer, and daily
report Tools. Department managers and company leaders receive the same set plus
three knowledge-review Tools. `knowledge_admin` receives the full 25-Tool
released catalog including archive, restore, and purge governance. Requirement
and task code remains registered for development, but only `system_admin` can
discover or call it until a later production release.

The local attachment tools accept only document, source-code, or image paths captured from Hermes'
Weixin adapter for the current Profile and Session. They reject arbitrary
paths, symlinks, modified files, expired grants, unsupported extensions, and
files larger than 25 MiB. Supported formats are DOCX/DOC, PDF, PPTX/PPT,
XLSX/XLS, CSV, HTML/XHTML, Markdown, TXT, JSON, XML, YAML, common source-code
files, JPEG, and PNG.

Media-only Weixin messages join the adapter's existing per-Session text batch
before Agent dispatch. A file or image therefore waits for the short configured
Weixin batch window so a nearby text instruction and additional attachments are
handled as one turn. Messages from different employees never share a batch.

Use the same five-second quiet window for normal text and long/media batches so
the result does not depend on whether the employee sends text or an attachment
first:

```yaml
gateway:
  platforms:
    weixin:
      extra:
        text_batch_delay_seconds: 5
        text_batch_split_delay_seconds: 5
```

- `analyze_wechat_attachment` extracts bounded text from an authorized PDF,
  DOCX, PPTX, XLSX, or other supported document so Hermes can summarize or
  analyze it without enabling terminal or generic filesystem tools. Up to
  80,000 characters are returned; larger documents include representative
  sections from across the file plus an explicit truncation warning. The most
  recently analyzed attachment is recorded per Profile and Session with a
  bounded excerpt, so follow-ups such as “继续分析刚才那份 PPT” resolve without
  re-uploading or searching the knowledge base. Only an explicit request to
  query company knowledge, existing documents, or related knowledge enables
  `search_knowledge`; the plugin rejects accidental calls deterministically.
- `upload_wechat_knowledge_attachment` uploads one authorized attachment to the
  normal ingestion workflow. Personal knowledge needs no category. Shared
  company knowledge requires a category and follows supervisor review. JPEG
  and PNG uploads require `image_analysis` produced after Hermes vision inspects
  the exact current attachment. The server records that analysis as untrusted
  client-generated evidence, validates the actual image independently, and also
  runs local OCR when available.
- `prepare_task_submission_file_download` receives only a `submission_no` and
  `document_id` already visible in task feedback, then returns a short-lived URL
  for the exact document version pinned by that submission. The model must not
  substitute a file name, store the URL, or expose employee credentials.

The OSS PUT is streamed from the cached file. The Profile PAT is sent only to
the RAG MCP origin and is never forwarded to OSS. A generic Hermes file tool is
therefore not required for either workflow.

Temporary analysis uses the existing staged-upload extraction path. The RAG
service deletes the temporary OSS object after extraction in a `finally`
cleanup path. An analyzed attachment is not indexed or persisted as knowledge
unless the employee separately invokes the knowledge-ingestion workflow.

Legacy DOC/PPT extraction runs only the fixed `antiword` and `catppt` commands
without a shell. Images are bounded by file-size and pixel-count limits. The
service never executes uploaded source code, Office macros, or embedded objects.

## Schema updates

Run this after changing RAG MCP tools:

```bash
PYTHONPATH=src .venv/bin/python scripts/export_hermes_profile_rag_tools.py
```

At Gateway startup the plugin fetches `/api/mcp/tool-catalog` from the configured
RAG service and registers exactly that server-published catalog. The generated
`tool_catalog_cache.json` is only a last-known-good availability cache when the
catalog endpoint is temporarily unreachable; it is not a second plugin-owned
allowlist, and every call remains subject to server-side PAT authorization.

## Identity smoke test

Run inside the Hermes container after enabling the plugin:

```bash
python /opt/hermes/plugins/profile_rag_mcp/smoke_test.py \
  /opt/data/profiles/test-employee \
  /opt/data/profiles/test-department-manager \
  /opt/data/profiles/test-company-leader
```

The command returns only the Profile name, tool count, and readable employee
identity. It does not print PAT values.
