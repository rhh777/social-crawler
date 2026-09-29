# Kubernetes deployment example

Deploys PostgreSQL and one application replica, with persistent volumes for data,
browser profiles and media.

Build the application image and make it available to the cluster as
`social-crawler:latest`, or override the image with Kustomize before applying
the manifests. Create the runtime secret separately:

```sh
kubectl create namespace social-crawler --dry-run=client -o yaml | kubectl apply -f -
kubectl -n social-crawler create secret generic social-crawler-secrets \
  --from-literal=postgres-password='<database password>'
kubectl apply -k deploy/k8s/base
```

The services use `ClusterIP`. Forward the console port for local access:

```sh
kubectl -n social-crawler port-forward service/social-crawler 8765:8765
```

Open <http://localhost:8765>. Account browser windows use KasmVNC through the
same console port.

## Before using this outside development

- Set a storage class appropriate for the cluster; the example's PVCs use the
  cluster default.
- Pin both application and PostgreSQL images by digest.
- Set `CRAWLER_IMAGE_DIGEST` to the deployed application digest.
- Add authenticated ingress or another access-control layer. The application
  does not provide a public multi-user authentication boundary.
- Keep one replica until account, profile, and scheduler leases have
  multi-node semantics.

Downloaded media is stored under `/app/artifacts` on a 20 GiB PVC. Media download
is disabled by default. Back up application data, profiles, artifacts, and the
PostgreSQL volume before replacing claims or changing storage classes.

## Custom images and overlays

Keep cluster-specific image registries, node selection, storage classes and
network settings in an untracked local overlay or a separate private repository.
The application and sidecar image names in this repository are local build names.

The directory has three layers:

- `base/` contains the portable application and PostgreSQL resources.
- `components/` contains optional capabilities such as the AdsPower sidecar.
- `overlays/` combines the base and components for a concrete deployment.

The public AdsPower example can be rendered with
`kubectl kustomize deploy/k8s/overlays/adspower`. Keep private environment
overlays out of the public source distribution.

When runtime dependency declarations and `uv.lock` are unchanged,
`deploy/docker/runtime.Dockerfile` can build a source-only layer from an existing runtime
image. It compares the TOML build-system/project sections and the complete
lockfile, then verifies that the base image's Chrome executable matches the
major version pinned by the application. Tool-only configuration changes do
not force a runtime rebuild, while dependency or browser-identity drift fails
the build.
Pass the base image explicitly in shared or production workflows:

```sh
docker buildx build -f deploy/docker/runtime.Dockerfile \
  --build-arg RUNTIME_IMAGE="$RUNTIME_IMAGE" \
  --label org.opencontainers.image.revision="$REVISION" \
  -t "$IMAGE_TAG" .
```
