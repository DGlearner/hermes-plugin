# Deployment

This repository is a source-mounted Hermes integration. It does not contain
employee credentials, chat history, pairing state, or the Hermes base image.

## Required persistent data

Keep these paths outside the Git checkout and back them up separately:

- `/home/kozy/project/hermes-data`
- `/home/kozy/project/hermes-cache`
- `/home/kozy/project/hermes-shared-model.env`
- `/home/kozy/project/hermes-wechat-provisioner.token`

The cache path should use a dedicated filesystem or data-disk mount. Deleting
or replacing the repository must never delete the persistent paths above.

## First installation

Clone the repository at the fixed production path:

```bash
git clone <REMOTE_GIT_URL> /home/kozy/project/hermes-profile-rag-mcp
```

The Hermes base Compose file must mount the repository root as the plugin:

```yaml
services:
  gateway:
    volumes:
      - /home/kozy/project/hermes-profile-rag-mcp:/opt/hermes/plugins/profile_rag_mcp:ro
```

Validate and start the Gateway with both Compose files:

```bash
docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  config --quiet

docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  up -d --force-recreate gateway
```

## Updating an existing server

```bash
cd /home/kozy/project/hermes-profile-rag-mcp
git fetch origin
git pull --ff-only origin main

docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  up -d --force-recreate gateway
```

Verify the deployment:

```bash
docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' hermes-weixin
docker exec hermes-weixin grep -m1 '^version:' /opt/hermes/plugins/profile_rag_mcp/plugin.yaml
```

Expected plugin version for this release: `0.7.0`.
