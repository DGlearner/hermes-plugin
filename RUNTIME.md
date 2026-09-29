# Hermes Profile-aware RAG MCP plugin

This plugin exposes the RAG MCP tools through Hermes' supported plugin API.
It is designed for a multiplex gateway where every employee has a Hermes
Profile and a different RAG PAT.

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
PROFILE_RAG_MCP_ATTACHMENT_BATCH_MAX_FILES=20
PROFILE_RAG_MCP_ATTACHMENT_SESSION_MAX_FILES=200
PROFILE_RAG_MCP_ATTACHMENT_SESSION_MAX_BYTES=1073741824
PROFILE_RAG_MCP_ATTACHMENT_PROFILE_MAX_BYTES=1073741824
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
  cleaner. Local temporary analysis creates no OSS objects.
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

### Scheduled task-summary delivery

Profile scripts can call `work_todos.run_reminder(home, username, peer,
reminder_key=...)`. Use a stable key for a scheduled occurrence, for example
`work-todos:2026-09-18:09:00`, so repeated execution cannot send the same
reminder again. The employee PAT is used to query and verify `get_current_user`
before `list_my_tasks`; each attempt fetches current tasks rather than retaining
an old report. Replies use that employee's independent Weixin transport or an
exactly matched route through the shared bot.

The reminder's profile scope masks inherited non-global credentials before
loading the selected Profile's own secrets. This also applies in fresh cron
subprocesses, which do not inherit the Gateway's in-memory multiplex flag.
Without this, inherited default-bot environment variables can falsely enable
an employee's independent transport and select the wrong context-token directory.
A missing employee PAT must never fall back to the default Profile's PAT.

Daily reminders support both the scheduled run and first interaction of the day.
Enable an employee explicitly with
`ReminderQueue(root).subscribe_daily(profile, peer, expected_username)` and use
`run_reminder(home, expected_username, peer, daily=True)` in the daily cron script.
The inbound hook only enqueues work; the background worker queries MCP and sends
the separate reminder without replacing the normal conversation response.
Employees without a subscription are not opted in by receiving a message.

Both triggers share one SQLite row with a unique `(employee_id, local_day)` index,
where `local_day` is the Asia/Shanghai calendar date. This is a delivery ledger,
not a scan of session history or tool calls. A successful morning reminder makes
the first-chat trigger a no-op; a successful pre-09:00 interaction reminder makes
the later cron run a no-op. Repeated inbound messages and concurrent cron runs
cannot create another occurrence. Manual conversational task queries are separate
and are not blocked by this daily automation's record.

Daily occurrences wait until local midnight, allowing an afternoon first message
to trigger a fresh query. The previous day's reminder is not replayed. Existing
same-day cron records are adopted, preserving sent/uncertain states and allowing
expired four-hour records to resume on interaction within the same day. Profile
session rotation does not delete these rows. Sending, sent, and uncertain rows
are never re-armed, including on a rebind. Generic one-off reminders retain their
four-hour expiry. The existing attempt cap and identity checks apply to both.

Creating a one-off test does not enable daily delivery. Register a recurring
`0 9 * * *` job in the employee Profile, with the deployment timezone set to
`Asia/Shanghai`, an unlimited repeat count, `--no-agent`, and `--deliver local`.
The script sends through this module itself; a second Weixin delivery target
would duplicate delivery. Return a nonzero script exit status unless the queue
state is `sent` (or the script was explicitly invoked in query-only mode).

Delivery state is stored separately from conversation Sessions in
`<Hermes root>/notifications/work-todos.sqlite3`. It contains binding identity, recipient,
schedule key, delivery status, attempt count, and sanitized error metadata;
it contains no PAT, bot token, context token, or task contents. Daily Session
rotation and Gateway restarts do not clear the queue.

- Explicit frequency limits and connection failures before sending retry after
  30, 120, and 300 seconds, with at most four delivery rounds in total.
- An explicit context rejection gets at most one recovery request per round:
  reread the same account and peer's latest token, using it if it changed;
  otherwise try without the rejected token. Recheck binding and transport before
  recovery, retain the original cached token, and reuse the reminder's client ID.
  A successful response or an ambiguous timeout never triggers this fallback.
- Rejected context or `prepare failed` waits for a new accepted message on the
  same employee's bound bot and peer. `prepare failed` is logged as a rejection,
  not automatically classified as a frequency limit or confirmed token expiry.
- A new interaction wakes the worker; before catch-up delivery, the worker
  rechecks the exact binding generation and fetches current tasks again.
- The worker also compares a non-reversible fingerprint of the exact bot/peer's
  persisted context against the rejected attempt. A changed context recovers a
  missed inbound hook across a restart. Unchanged, missing, or another peer's
  context never triggers this retry. Legacy records establish a baseline once
  without sending. No token is stored in the queue, and the existing expiry,
  binding checks, attempt limit, and uncertain-delivery protection still apply.
- Unbind, rebind, or a credential/employee mismatch cancels the old reminder.
- A lost send acknowledgement or interrupted send is marked
  `delivery_uncertain` and never retried automatically, avoiding duplicates.
- Unsent one-off reminders expire after four hours; daily reminders expire at
  Asia/Shanghai midnight. Terminal records are retained for
  seven days. The maintenance worker checks every 15 seconds and wakes earlier
  when an accepted user message updates the Weixin context.

The `delivery_attempts` table records every HTTP attempt's mode, whether context
was present, time since the last locally observed inbound message (unknown until
observed), text length, sanitized rejection, and presence of a message receipt.
No credential or text body is stored. Attempts are retained for seven days.

The recovery request cannot renew a server-side permission to push to a dormant
peer. Tencent's documented `getConfig` returns a typing ticket, not a new
`context_token`; a process restart and daily Session rotation do not refresh it.
There is no fixed 24-hour expiry enforced by this plugin because the exact
server-side window is not documented in the protocol. See
[Tencent's protocol](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)
and [the matching Hermes field report](https://github.com/NousResearch/hermes-agent/pull/100815).

Script-triggered delivery logs are written to
`<Hermes root>/notifications/work-todos-delivery.log`; background retries also use the
normal Gateway log. These rules improve recovery but do not bypass any Weixin
restriction on proactive messages or guarantee receipt by the client.

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

Hermes owns semantic interpretation and selection of `search_knowledge`. The
plugin only verifies that the request has current real-user context, is not an
explicit rejection of knowledge retrieval, and is not solely a request to
resume analysis of the current temporary attachment. A successful or explicit
knowledge search records a marker, without query text, in the current Profile's
current Session. Follow-ups such as “再看看类似方案” may inherit that scope.
Explicit negation, switching back to the current attachment, `/new`, and
automatic Session rotation clear it; it is never shared across Profiles or
Sessions. Plugin-generated errors include `source: "profile_rag_mcp"`.

The restart settings let active turns finish before the container exits;
combine them with a Docker `stop_grace_period` longer than the drain timeout.

## Employee tools and Weixin attachments

Version 0.11.0 also accepts cached MP4/MOV/WEBM/MKV attachments for knowledge upload.
Use `upload_wechat_knowledge_attachment` with a user-provided `video_summary` and optional
`video_title` (defaults to the filename). No duration or timeline is required. The server indexes
the description and labels it summary-only; it does not parse video. Never invent visual/audio
details. `analyze_wechat_attachment` rejects videos explicitly. The existing 25 MiB attachment
limit, Profile/Session grants, processing leases and cleanup remain in force. Upgrade the MCP
server first, then reload this plugin by restarting Hermes. No base-image upgrade is needed.

Employee Weixin Profiles enable only `web`, `vision`, `session_search`,
`clarify`, and `profile_rag_mcp`. The RAG toolset consumes the MCP server's
released catalog plus three local attachment tools. The plugin does not maintain
a second Tool allowlist: publication and role visibility belong to the original
MCP service, while every `tools/call` remains subject to the same server-side
policy and domain authorization.

At registration time the plugin replaces only the two knowledge-search tool
descriptions with the Profile/Session continuity policy above. The MCP server
still owns tool publication, input schemas, PAT authorization, role visibility,
and domain access control.

The first production catalog deliberately excludes requirement and task Tools.
An employee PAT receives 19 identity, directory, knowledge, transfer, and daily
report Tools. Department managers and company leaders receive the same set plus
three knowledge-review Tools. `knowledge_admin` receives the full 25-Tool
released catalog including archive, restore, and purge governance. Requirement
and task code remains registered for development, but only `system_admin` can
discover or call it until a later production release.

The local attachment tools accept only document, source-code, image, or supported video paths captured from Hermes'
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
  re-uploading or searching the knowledge base. Hermes selects
  `search_knowledge` from the user's semantics and same-Session conversation;
  the plugin rejects missing user context, explicit retrieval negation, and
  attachment-only continuation calls deterministically.
- `upload_wechat_knowledge_attachment` uploads one authorized attachment to the
  normal ingestion workflow. Personal knowledge needs no category. Shared
  company knowledge requires a category and follows supervisor review. JPEG
  and PNG uploads require `image_analysis` produced after Hermes vision inspects
  the exact current attachment. The server records that analysis as untrusted
  client-generated evidence, validates the actual image independently, and also
  runs local OCR when available.
- `download_wechat_knowledge_file` takes only a `document_id` returned by search
  or listing. It calls `prepare_knowledge_download` using the active employee's
  PAT on every request, fetches the authorized original bytes without forwarding
  credentials, and returns a Gateway attachment directive. The model must include
  `delivery_directive` verbatim in its final reply, outside code fences. The
  `[[as_document]]` marker requests original-file delivery; this is not a text
  export or a summary. Status `ready_for_delivery` means the download succeeded,
  not that Weixin has acknowledged delivery.
  Signed URLs are never returned by this local tool or written to its cache.
  GET requests allow only HTTPS on the MCP origin or exact hostnames in
  `PROFILE_RAG_MCP_DOWNLOAD_HOSTS` (comma-separated, no wildcard); redirects and
  ambient proxies are disabled. Configure the actual public OSS bucket hostname
  in the deployment environment when direct downloads are enabled.
  Downloads use private `knowledge-downloads/<session-hash>/request-*/` folders
  inside each Profile, preserve safe original file names, and record a SHA-256
  digest in the result. A request accepts at most the existing attachment size
  limit (25 MiB by default) and streams for at most 60 seconds. Separate outbound
  cache quotas use the existing session count/byte, Profile byte, and global byte
  limits; quotas reserve one maximum-sized file before each transfer. Files expire
  after one hour and are removed by the existing maintenance worker (normally
  every ten minutes) or before the next download. Failed transfers remove partial
  files. Outbound files never become authorized incoming attachment grants.
- `prepare_task_submission_file_download` receives only a `submission_no` and
  `document_id` already visible in task feedback, then returns a short-lived URL
  for the exact document version pinned by that submission. The model must not
  substitute a file name, store the URL, or expose employee credentials.

The OSS PUT is streamed from the cached file. The Profile PAT is sent only to
the RAG MCP origin and is never forwarded to OSS. A generic Hermes file tool is
therefore not required for either workflow.

Temporary analysis (plugin 0.10.0) reads authorized plain-text attachments locally.
PDF/DOCX/PPTX/XLSX are streamed to the local MinerU adapter's authenticated
`POST /tasks/from-file` endpoint, sharing its existing backend pool with ingestion.
Parsing stays `auto`; no OSS object or ECS extraction job is created, and failures
never fall back to cloud staging. Legacy DOC/PPT/XLS must first be converted.
Extracted text remains untrusted data; files, macros and source code are never executed.

Configure Hermes with `PROFILE_RAG_MCP_LOCAL_MINERU_URL=http://mineru-adapter:8000`,
`PROFILE_RAG_MCP_LOCAL_MINERU_TOKEN_FILE=/run/secrets/adapter.token` and optionally
`PROFILE_RAG_MCP_LOCAL_ANALYSIS_TIMEOUT_SECONDS=600`. Mount the adapter token into
Hermes separately from Profile PATs. The local parser receives only its adapter token.
Model inference still uses the configured model provider and receives extracted text.

Incoming attachments retain their existing Profile/Session authorization and cache
expiry (pending/failed 30 minutes idle, analyzed 60 minutes idle, absolute 4 hours).
Active parsing holds a cleanup lease. Adapter upload spools are removed after native
submission, including failures. Native MinerU task results have their own retention
(the current Mac service uses 2 hours). Knowledge ingestion remains an explicit,
separate OSS/RAG operation.

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

### 2026-09-21: Telegram / Weixin attachment delivery

- Register verified document paths before awaited inbound-text preparation, with a durable pending key scoped to Profile, platform, sender and resolved Session ID. The pre-LLM hook recovers only that Session's pending receipts and supplies its authorized attachment manifest.
- Telegram private chats now use the same attachment authorization/analysis path as Weixin. Repeated receipt of the same cached path is deduplicated.
- During an attachment turn, follow-ups use the existing Gateway queue policy instead of interrupting the active model request. Core authorization and control handling remain in place; ordinary text-only conversations retain their configured policy.
- A late Telegram album item cancels only a pending debounce timer, never an album dispatch already in progress.
- Receipt/handoff logs report Profile, Session, accepted/rejected counts without attachment contents or credentials.
- Regression tests: `tests/test_attachment_delivery.py`, `tests/test_attachment_quota_runtime.py`; real Telegram adapter tested without network sends. Full plugin suite passed before reload.

### 2026-09-21: Weixin encrypted document validation

`weixin_media.py` installs a document download override from the mounted plugin. It accepts `aes_key` / `aeskey` in the media or file item, normalizes raw hex/base64 representations, and rejects conflicting or invalid keys. Encrypted references without a key fail closed. Plaintext length (`file_item.len`), supported text decoding and PDF/modern Office signatures are checked before caching. Diagnostics contain reason codes, key field location, byte count and suffix only; no key values, CDN URLs or document content.

Old malformed text caches now return `attachment_content_invalid`; this does not repair historical ciphertext without its original key. A fresh upload is needed. Tests use actual Hermes AES functions and Weixin download code with mocked CDN responses; live client upload remains the final protocol verification.

### Weixin receipt diagnostics (2026-09-21)

Adapter-entry and exit diagnostics now correlate message-ID/sender/filename hashes with pre-download duplicate/policy gates and attempted/saved file counts. Key *field names* are recorded; raw keys, context tokens, CDN query parameters, filenames and document bodies are not. This is instrumentation, not a change to message admission or deduplication. Earlier logs begin after media collection, so absence of an old `inbound` line cannot prove that the Weixin API never delivered that message.

### Telegram private-topic attachment authorization (2026-09-21)

Telegram private bot chats can carry a `thread_id`. The attachment ingress guard
previously treated any thread as unsupported and silently skipped registration,
leaving downloaded files visible in the conversation but returning
`attachment_missing` from analysis and upload tools. Private topics now register
against their resolved Session ID. Group chats remain excluded; a topic without a
resolved Session fails closed with a diagnostic rather than entering the shared
sender-only pending lane. Existing unregistered files are not automatically
trusted by scanning caches or extracting paths from conversation text.

Regression coverage replays a private-topic envelope through the Gateway capture
hook, clears in-memory receipts, runs the pre-LLM durable handoff, and exercises
analysis with/without a path and upload authorization up to the network boundary.
It also checks different Session/Profile isolation and group rejection. Restoring
the former thread guard in an isolated copy reproduces the failure.
## Profile inspection

`profile_inspection.py` contains the native, Profile-scoped preflight used by
the general inspection Skill. It persists its SQLite state in each Profile's
`.hermes-profile-inspection.sqlite3`, so `last_checked_at`, observed and
notified fingerprints, and retry state survive a Gateway restart without
sharing one Profile's PAT or notification ledger with another Profile.

The optional Profile configuration is:

```yaml
profile_inspection:
  enabled: true
  timezone: Asia/Shanghai
  work_hours: {start: "09:00", end: "18:00"}
  interval_minutes: 30 # 5 (pilot), 30 or 60
  quiet_hours: {enabled: false, start: "22:00", end: "08:00"}
  sources: {tasks: true, knowledge_reviews: true}
  notifications: {max_items: 10}
```

The scheduler integration must call `InspectionRunner.run()` with the current
Profile's MCP query, model-summary, and current-channel send adapters. The
runner performs the MCP query and fingerprint comparison before invoking the
model; unchanged checks therefore return without a model turn or outbound
message. Quiet-time changes remain pending and are delivered after quiet time.
The module also exposes `force=True` for a one-shot manual inspection.


### crz daily-report pilot (2026-09-28)

The operational pilot uses `daily_inspection.DailyInspectionRunner`, invoked
by a native Hermes `no_agent` cron script. It is opt-in through the selected
Profile's `inspection.json`; no plugin-register hook or background worker is
installed. The scheduler ticks every minute; the runner queries every 5, 30 or
60 minutes in 09:00–18:00 Asia/Shanghai. Unchanged checks emit the native
`wakeAgent: false` gate and never construct an agent. When changes exist,
Hermes `run_job` renders the shared Skill with the Profile's model, then the
runner delivers once to the explicitly bound destination. It verifies Profile
identity before querying and checks the route again before delivery.

This pilot queries today's `list_daily_reports`, using report type, subject,
and report date as the stable key and version/content fingerprint for changes.
The API currently lacks pagination: snapshots reaching its 100-record limit
fail closed rather than silently missing reports. It is not a history/event
feed; changes between polls and reports outside today's date are not replayed.

Durable state is stored in `inspection/state.sqlite3`, separate from older
prototype task inspection state. A process lock prevents concurrent manual and
scheduled runs. Pending batches are refreshed from authorized query results;
quiet time defers delivery. Query failures back off, model failures stop after
four attempts, and delivery with an unknown outcome is held as `uncertain`
for operator review rather than blindly retried. Restarting cron preserves the
ledger. Only `sent` batches advance the notified fingerprint. Status metadata
without report bodies is available in `inspection/last_result.json`.

Only crz is enrolled for the initial pilot, using its bound Telegram private
chat. Other Profiles, templates and the existing daily task reminder are not
modified. To disable, set `enabled: false` in `inspection.json` and pause the
named cron job. Do not delete the ledger when pausing or restarting.

Pilot interval update: crz uses `interval_minutes: 5` for the daily-report
acceptance test. The default remains 30 minutes; only this Profile configuration
was changed. Cron reads the configuration on each tick without a restart.

crz testing now enables `test_notify_each_check: true` in `inspection.json`.
Each actual query produces a fixed-text Telegram test receipt with execution
 time, visible report count and change count; unchanged queries do not invoke
 the model. Scheduler ticks that are not due produce no receipt. Query failures
 produce a failure receipt. Quiet hours still suppress receipts. Receipt attempts
 are persisted before sending and never retried on an uncertain outcome; they
 do not modify business notification fingerprints. Disable this flag to restore
 normal unchanged-check silence.

crz now uses `notification_targets` for both its bound Telegram and Weixin
routes. Both change notifications and test receipts are delivered to each.
`notification_target` remains the backward-compatible single-target fallback.
`inspection/deliveries.sqlite3` records each batch and destination separately;
one channel failure does not prevent the other from being attempted. Successful
or uncertain attempts are not blindly replayed. The sender revalidates Profile
routing for each destination and Weixin also verifies the active binding.

Test receipts now include the full current authorized report body with report
 type, subject, date and author, including on unchanged checks. Bodies are taken
 from the current query rather than historical notification batches. They are
 removed from the returned status metadata before `last_result.json` is written.
Long messages are split into numbered 1500-character parts without truncation;
per-destination delivery records also deduplicate each part independently.
The current crz pilot source remains daily reports, not task todos.

Latest crz acceptance-test setting: `interval_minutes: 2` (previously 5).
The default remains 30. Each report entry uses a numbered title, a blank line,
flush-left date/author, a blank line, the body label and another blank line before
the content. All content lines have leading whitespace removed for display;
paragraph breaks are retained. Both Weixin and Telegram receive this layout.

The crz unified inspection pilot enables `include_tasks: true`: daily reports
remain restricted to today's Profile-local date, while `list_my_tasks` is paged
without a date filter and `get_task` supplies full instructions and submission
requirements. Open, in-progress and pending-review tasks share the durable read
receipt ledger; completed, cancelled or no-longer-visible tasks stop reminding.
An incomplete task snapshot fails closed. The legacy 09:00 work-todos job and
first-interaction subscription are paused for crz to avoid duplicate delivery.
