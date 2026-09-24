---
name: profile-inspection
description: Inspect the current Profile's task and review queues on its configured schedule.
---

# Profile inspection

The runtime invokes this Skill through the native Profile inspection runner.
The runner first queries the current Profile's permitted MCP tools and compares
durable fingerprints. If there is no new item or state change, finish silently
without an LLM turn or outbound message.

When the runner supplies changed items, produce a concise notification in the
current Profile's language. Mention the item type, title or identifier, and the
new state. Do not request or reveal PATs, API keys, channel credentials, or
internal filesystem paths. Send through the current Profile's active WeChat or
Telegram channel only.
