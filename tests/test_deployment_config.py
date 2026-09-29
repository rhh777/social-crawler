from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployments_use_account_desktops_without_a_shared_vnc_port():
    for name in ("compose.yaml", "deploy/k8s/base/app.yaml"):
        config = (ROOT / name).read_text()
        assert "Xvfb :99" in config  # Background browser display remains available.
        assert "novnc" not in config
        assert "x11vnc" not in config
        assert "6080" not in config
        assert "CRAWLER_LOGIN_VIEWER_URL" not in config
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "deploy/docker/kasmvnc/install.sh" in dockerfile
    assert "x11vnc" not in dockerfile


def test_adspower_overlay_is_opt_in_and_keeps_api_private():
    overlay = ROOT / "deploy/k8s/overlays/adspower"
    component = ROOT / "deploy/k8s/components/adspower"
    kustomization = (overlay / "kustomization.yaml").read_text()
    patch = (component / "app.patch.yaml").read_text()

    assert "../../base" in kustomization
    assert "../../components/adspower" in kustomization
    assert "newName: adspower-runtime" in kustomization
    assert "newTag: latest" in kustomization
    assert "name: adspower" in patch
    assert "name: adspower-api" in patch
    assert "secretKeyRef:" in patch
    assert "check-status" not in patch  # Node CLI is too slow under browser load
    assert "adspower-healthy" in patch
    assert "test -f /tmp/adspower-ready" in patch
    assert "timeoutSeconds: 3" not in patch
    assert "containerPort:" not in patch


def test_compose_pins_amd64_and_offers_an_opt_in_adspower_sidecar():
    compose = (ROOT / "compose.yaml").read_text()

    assert compose.count("platform: linux/amd64") == 2
    assert 'profiles: ["adspower"]' in compose
    assert "dockerfile: deploy/docker/adspower/Dockerfile" in compose
    assert "crawler-runtime: service:app" in compose
    assert 'network_mode: "service:app"' in compose
    assert "adspower-cache:/home/crawler/.adspowerCli" in compose
    assert compose.count("x11:/tmp/.X11-unix") == 2
    assert "containerPort: 50325" not in compose


def test_adspower_runtime_does_not_bake_credentials_into_the_image():
    runtime = ROOT / "deploy/docker/adspower"
    dockerfile = (runtime / "Dockerfile").read_text()
    entrypoint = (runtime / "entrypoint.sh").read_text()
    healthcheck = (runtime / "healthcheck.sh").read_text()

    assert "ADS_API_KEY" not in dockerfile
    assert "ADS_API_KEY must be supplied through a runtime secret" in entrypoint
    assert 'ads start -k "$ADS_API_KEY"' in entrypoint
    assert 'failure_limit="${ADS_HEALTH_FAILURES:-18}"' in entrypoint
    assert "python <<'PY'" in entrypoint
    assert '"/api/v2/browser-profile/download-kernel"' in entrypoint
    assert "node <<" not in entrypoint
    assert "exec python - <<'PY'" in healthcheck
    assert "urllib.request.urlopen" in healthcheck


def test_adspower_runtime_presents_macos_font_families():
    runtime = ROOT / "deploy/docker/adspower"
    dockerfile = (runtime / "Dockerfile").read_text()
    fonts = (runtime / "fonts/99-macos-families.conf").read_text()

    assert "fonts/99-macos-families.conf /etc/fonts/conf.d/" in dockerfile
    assert "fc-cache -f" in dockerfile
    for family in ("PingFang SC", "Helvetica", "Menlo", "Apple Color Emoji", "Songti SC"):
        assert f"<string>{family}</string>" in fonts
    for linux_fonts in ("truetype/wqy", "truetype/dejavu", "opentype/unifont"):
        assert linux_fonts in fonts
