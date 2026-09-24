# Employee model selection and fallback

Status: enabled on the local Mac on 2026-09-21 at 15:17 (Asia/Shanghai). Default: official `deepseek-flash`; alternate: `gpt-6-astra`. Both endpoints passed real synthetic completion tests. Hermes was recreated to import the independent DeepSeek credential; Weixin and Telegram connected successfully.

`employee_models.json` is a non-secret, administrator-owned two-route registry. DeepSeek and GPT each have their own URL, model ID and credential environment variable. GPT reuses `HERMES_SHARED_MODEL_API_KEY`; DeepSeek requires `HERMES_DEEPSEEK_API_KEY`. Add the latter to the existing local `hermes-mineru-stack/secrets/hermes-shared-model.env` (never commit that file). Docker Compose must recreate the Hermes container to import a changed env file; a plain restart does not import new environment variables.

When enabled:

- `/model deepseek`, `/model gpt`, `/model` are handled directly for authorized Weixin/Telegram users. Other model/flag inputs are refused in managed mode.
- The choice is atomically saved in the routed employee Profile's config. Both channels and future sessions use it. No credential value is written by the switch. PAT, permissions and session files are preserved.
- Profiles without a preference inherit DeepSeek; the provisioning path preserves an existing preference and defaults new employees to DeepSeek.
- The managed shared-model overlay is deliberately followed by the administrator's two-route policy, so the previous system GPT pin cannot overwrite an employee's approved selection.
- Primary and fallback entries contain independent URL/key references. Hermes' native request retry/fallback path handles errors without restarting the entire agent/tool workflow. The request retry budget is 1; the sole fallback is the other provider. No indefinite ping-pong. Automatic failover does not save a different employee preference.
- The normal provider error reply is Chinese and contains no upstream exception or secrets. A policy/context/invalid-request refusal can still be handled by Hermes' native classification; not every non-provider error implies both providers were attempted.
- Running turns are not mutated by the command. New turns reload effective profile config. Native session-only model overrides are ignored in managed mode.

Activation and validation record:

1. Completed: authenticated official `/models` confirmed `deepseek-flash`; separate credential loaded from the environment file.
2. Completed: real completions on both endpoints; DeepSeek tool-call round trip and reasoning_effort=low succeeded.
3. Completed: flag enabled and container recreated; effective defaults verified for root, template and two employees. Both channel command handlers were tested in an isolated temporary Profile with actual runtime provider resolution. Client-side acceptance is now available to the user.
4. Dual-direction fallback activation and exhaustion tests passed using substitute clients. Production provider outages were not induced; these tests do not prove behavior under every real upstream failure.

Rollback: set `enabled` false and restart. If employee configs were changed while enabled, restore those model-related config fields from the activation backup as well; disabling the flag alone does not undo saved Profile choices.
