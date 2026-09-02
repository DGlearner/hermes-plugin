# Deployment

This repository is a source-mounted Hermes integration. It does not contain
employee credentials, chat history, pairing state, or the Hermes base image.
The repository publishes the pinned base image to GHCR, while plugin releases
remain independent Git tags such as `v0.7.0`.

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
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-base-image.compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  config --quiet

docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-base-image.compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  pull gateway

docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-base-image.compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  up -d --force-recreate --no-build gateway
```

## Updating an existing server

```bash
cd /home/kozy/project/hermes-profile-rag-mcp
git fetch origin
git pull --ff-only origin main

docker compose \
  -f /home/kozy/project/hermes-agent/hermes-23-compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-base-image.compose.yaml \
  -f /home/kozy/project/hermes-profile-rag-mcp/hermes-wechat-provisioner.compose.yaml \
  up -d --force-recreate --no-build gateway
```

Verify the deployment:

```bash
docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' hermes-weixin
docker exec hermes-weixin grep -m1 '^version:' /opt/hermes/plugins/profile_rag_mcp/plugin.yaml
```

Expected plugin version for this release: `0.7.0`.

## Hermes base-image releases

The immutable runtime image is built from the exact upstream source recorded
in `docker/hermes-base.env`. Updating the plugin does not rebuild this image.

To publish a new base image, change all three pinned values in that file and
push the reviewed commit to `main`. The workflow publishes three equivalent
tags:

- the upstream Hermes version, for example `0.20.5`;
- the first 12 characters of the upstream commit;
- `stable`, which is convenient for inspection but should not be used by the
  production Compose file.

Production must use the explicit Hermes version tag. If the GHCR package is
private, authenticate the server once with a GitHub token that has only
package-read permission before running `docker compose pull`.
