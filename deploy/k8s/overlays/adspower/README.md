# AdsPower Kubernetes overlay

This overlay adds the AdsPower runtime as a sidecar in the crawler Pod. The two
containers share loopback networking, `/dev/shm`, and the X11 socket; neither the
Local API nor dynamic CDP ports are exposed through a Service.

Build `deploy/docker/adspower/Dockerfile` as `adspower-runtime:latest` and make both
this image and `social-crawler:latest` available to your cluster. Set your registry and image digests in a Kustomize overlay before deploying.

Set `ADS_API_KEY` in the project root `.env.deploy`, then synchronize it to the Secret
without putting the key in shell history (the namespace must already exist):

```sh
uv run python scripts/sync_adspower_secret.py --context YOUR_CONTEXT
```

This updates `adspower-api/api-key` only. Restart the Pod after changing the key.
See [the configuration guide](../../../../docs/guides/configuration.md) for all
settings and precedence. Kubernetes does not automatically read local environment files.

Render and validate before deployment:

```sh
kubectl kustomize deploy/k8s/overlays/adspower
kubectl --context YOUR_CONTEXT apply -k deploy/k8s/overlays/adspower
```

After the sidecar is ready, set an account's `browser_provider` to `adspower`
and configure its `adspower_profile_id`. Leaving the provider as `chromium`
keeps the existing browser path even while the sidecar is installed.

Account windows opened from the console and headed collection runs each use a
dedicated KasmVNC display. A running collection can therefore be watched from
the console without opening the profile a second time, and concurrent profiles
do not share a desktop. The shared Xvfb display `:99` remains the process-level
fallback. These displays use software rendering; the client enables SwiftShader
for headful profiles and requests a 1440x900 window. Profile screen and renderer
settings are managed in AdsPower. See [account and browser design](../../../../docs/design/accounts-and-browsers.md).

Before starting a profile the client refuses one that AdsPower reports as open
on another device (`environment_in_use`, which stops the run) and closes a
local copy left over from an interrupted worker. Close the profile in the
desktop AdsPower client before running collection in the cluster.
