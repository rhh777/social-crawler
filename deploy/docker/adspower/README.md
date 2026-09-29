# AdsPower runtime image

Keep your key in the project `.env.deploy` as `ADS_API_KEY`. For Kubernetes, use
`uv run python scripts/sync_adspower_secret.py --context YOUR_CONTEXT` to update the
existing Secret. See [configuration](../../../docs/guides/configuration.md).

This image runs the AdsPower CLI runtime as a non-root sidecar. Supply
`ADS_API_KEY` from a runtime secret and persist `/home/crawler/.adspowerCli`
when browser kernels and profile state must survive Pod replacement.

Build the crawler image first and pass it as `RUNTIME_IMAGE`. The sidecar
reuses its browser libraries. Pin the image by digest for deployment.

Set `ADS_KERNEL_VERSION` to pre-download the profile's Chrome kernel. The
entrypoint creates `/tmp/adspower-ready` only after warmup; the supplied
Kubernetes startup/readiness probes require that marker. The entrypoint exits
if the Local API later fails `ADS_HEALTH_FAILURES` consecutive checks (default
18), allowing Compose or Kubernetes to restart the sidecar without killing it
during a short CPU-heavy browser startup.

Run the sidecar in the same Pod as the application. They share loopback
networking for the Local API and CDP connections; neither needs a Service port.
Docker Compose provides the equivalent opt-in service with
`docker compose --env-file .env.deploy --profile adspower up --build --wait`.

The profile presents macOS, so `fonts/99-macos-families.conf` renames the
installed Liberation and Noto CJK/emoji fonts to macOS family names (Helvetica,
Times, Menlo, PingFang, Songti, Hiragino, Apple Color Emoji) at fontconfig scan
time and hides Linux-only families. Only name-based detection is covered:
glyph widths still differ from Apple's fonts, and Chrome's built-in metric
compatibility table still reports `Liberation Sans`/`Liberation Serif`/
`Liberation Mono` as present because Arial, Times and Courier are served by them.
