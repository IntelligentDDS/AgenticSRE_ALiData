# AgenticSRE Release 20260531

This package was created from an exported Docker image and the current source snapshot.

## Image

- Image reference: `agenticsre:latest`
- Image tar: `agenticsre-image.tar.gz`
- Image source: `/tmp/agenticsre-image-20260531.tar.gz`
- Image metadata: `IMAGE_META.txt` when available
- Source snapshot: `feature/config-center-healing@faab05b`

## Contents

- `agenticsre-image.tar.gz`: prebuilt Docker image
- `agenticsre-source.tar.gz`: source snapshot from the current workspace
- `docker-compose.yaml`: offline runtime Compose definition (no build section)
- `deploy_docker.sh`: one-command offline deployment helper
- Runtime source directories are included at package top level for bind mounts
- `configs/`: runtime configuration templates
- `doc/`, `report/`, `测试/`, `tests/`: local project documentation and test materials
- `.env.example`: environment variable template
- `eval/`: fault scenarios and evaluation utilities required by Web APIs
- `SHA256SUMS`: checksum file for package verification
- `MANIFEST.txt`: file list inside this release package

## Deploy

```bash
tar xzf agenticsre-release-20260531-oneclick.tar.gz
cd agenticsre-release-20260531-oneclick
./deploy_docker.sh
```

The release deploy script creates `.env` from `.env.example` when needed,
loads `agenticsre-image.tar.gz`, verifies the bundled runtime, and starts Compose with
`--no-build`. It should not download apt/pip dependencies during deployment.
Use `./deploy_docker.sh --build` only when an online rebuild is intentional.

Dashboard: http://localhost:8080
Health: http://localhost:8080/api/health
