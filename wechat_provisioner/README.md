# Hermes WeChat Provisioner

This private service turns an employee's WeChat pairing code into a durable
Hermes Profile binding. It runs as the Hermes container's main process with a
Docker `unless-stopped` restart policy, while the normal default gateway
remains under its own S6 service.

The service:

- validates a pairing code in the default Profile;
- creates `employee-<employee_uuid_hex>` from `employee-template`;
- stores the employee RAG PAT only in that Profile's `.env`;
- grants the WeChat user in both the routed employee Profile and the default
  ingress Profile required by Hermes busy-session authorization;
- writes one `gateway.profile_routes` rule; the Profile RAG plugin hot-loads it
  on the next inbound message without restarting the shared gateway;
- keeps a secret-free idempotency journal under `/opt/data/provisioner`;
- removes the route, grant, and PAT on unbind while retaining chat history;
- writes a durable Profile attachment-cleanup request on unbind so the Gateway
  physically removes that employee's temporary attachment files without
  deleting the Profile `state.db`;
- accepts repeated unbind calls safely, so employee offboarding cleanup can retry after network or process failures;
- converges new and existing employee Profiles to one shared model
  configuration, restricted Weixin tools, and a durable Session policy without
  replacing any Profile `state.db`.

It must listen only on loopback or a Tailscale address and requires a shared
Bearer token. Pairing codes and PAT values are never written to its state file
or application logs.

## Employee template

After applying the Compose override, create a template from one correctly
configured test Profile. The helper clones only Profile configuration and then
removes the employee PAT and all WeChat account credentials. Any inline model
key is replaced with the shared environment reference.

```bash
docker exec -u 1000 hermes-weixin \
  python -m wechat_provisioner.cli create-template \
  --clone-from test-department-manager
```

`/readyz` rejects a template that still contains an employee PAT or WeChat
credential.

When the source model URL, model name, reasoning settings, tool policy, or
Session policy changes, converge the template and all managed employee
Profiles. Extra test Profiles can be named explicitly and the option may be
repeated:

```bash
docker exec -u 1000 hermes-weixin \
  python -m wechat_provisioner.cli sync-employee-profiles \
  --model-source test-department-manager \
  --include-profile test-employee \
  --include-profile test-department-manager \
  --include-profile test-company-leader
```

This command updates only managed YAML and marker files. It does not recreate
Profiles, delete PAT files, or modify Session databases.

## Runtime

Generate a 32-byte random shared token, save only the raw value in a mode-600
host file, and apply `hermes-wechat-provisioner.compose.yaml` together with the
normal Hermes compose file. Adjust the two host mount paths and the Tailscale
listen address for the target host.

Store the common model credential in the host file referenced by the Compose
override, never in Git:

```bash
install -m 600 /dev/null /home/kozy/project/hermes-shared-model.env
```

The file contains one assignment:

```env
HERMES_SHARED_MODEL_API_KEY=replace-with-the-shared-provider-key
```

Recreate the Gateway container after adding or changing this file because
Docker environment variables are read only at container creation. Creating a
new employee Profile afterward does not require a Gateway restart; the plugin
loads the new route and Profile auth store on that employee's next inbound
message.

The RAG application uses the same secret through:

```env
RAG_MCP_HERMES_PROVISIONER_URL=http://100.104.30.119:8788
RAG_MCP_HERMES_PROVISIONER_TOKEN=<same secret>
```

Disabling an employee or using the administration page's offboarding action
revokes every employee PAT immediately and queues this Provisioner unbind. The
RAG application keeps retrying a `revoking` binding with exponential backoff;
it never re-enables the employee when this service is unavailable. Successful
cleanup removes routing and credentials but deliberately retains the employee
Profile and its `state.db` chat history.

## Profile policy

Every managed employee Profile uses the same provider URL/model settings and
the shared model-key environment reference, while retaining its own
`MCP_COMPANY_MCP_API_KEY`. Weixin exposes only `web`, `vision`,
`session_search`, `clarify`, and `profile_rag_mcp`; command approvals are denied
by default. Sessions rotate on the first message after 04:00 Asia/Shanghai once
per day, and old Sessions remain in the Profile `state.db` for
`session_search`.
