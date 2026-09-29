# Deployment assets

The repository root keeps the conventional default entry points:

- `Dockerfile` builds the amd64 application image with `docker build --platform linux/amd64 .`.
- `compose.yaml` runs the application and PostgreSQL with Docker Compose; the
  optional `adspower` profile adds the same-network AdsPower sidecar.

Supporting deployment assets are grouped here:

- `docker/` contains specialized image definitions and image-build resources.
- `k8s/base/` contains portable Kubernetes resources.
- `k8s/components/` contains optional Kubernetes capabilities.
- `k8s/overlays/` contains deployable Kubernetes variants and environments.

See [`k8s/README.md`](k8s/README.md) for deployment and validation commands.
