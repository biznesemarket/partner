"""Тесты поставки: разрешение release asset и манифест dist.

Сценарии:
- D-01  latest/точный тег выбирают только опубликованный релиз
- D-02  wheel выбирается из активов, sdist игнорируется
- D-03  resolve берет sha256 из digest и не требует скачивания
- D-04  недостающий digest вычисляется по скачанному файлу
- D-05  манифест сверяет sha256 файлов с релизом и падает при расхождении
- D-06  repo-pack workflow не откатывается на публичный PyPI (fail closed)
- D-07  публичный release не требует PAT: browser URL и sha256 достаточно
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import partner_dist as dist  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
WHEEL = "partner_tracker-1.2.0-py3-none-any.whl"
SHA = "a" * 64


def _release(tag: str = "v1.2.0", *, digest: str | None = f"sha256:{SHA}") -> dict:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-11-14T00:00:00Z",
        "assets": [
            {
                "id": 11,
                "name": WHEEL,
                "size": 123,
                "digest": digest,
                "url": "https://api.github.com/repos/acme/partner/releases/assets/11",
                "browser_download_url": (
                    "https://github.com/acme/partner/releases/download/"
                    f"{tag}/{WHEEL}"
                ),
            },
            {
                "id": 12,
                "name": "partner_tracker-1.2.0.tar.gz",
                "size": 456,
                "digest": "sha256:" + "b" * 64,
                "url": "https://api.github.com/repos/acme/partner/releases/assets/12",
                "browser_download_url": "https://github.com/acme/partner/releases/download/x/x.tar.gz",
            },
        ],
    }


# --- D-01 ----------------------------------------------------------------

def test_pick_release_latest_and_exact_tag():
    releases = [
        {"tag_name": "v1.3.0", "draft": True, "published_at": "2026-11-15T00:00:00Z", "assets": []},
        {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-11-14T00:00:00Z",
            "assets": [],
        },
        {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-11-13T00:00:00Z",
            "assets": [],
        },
        _release("v1.2.0"),
    ]
    assert dist.pick_release(releases, "latest")["tag_name"] == "v1.2.0"
    assert dist.pick_release(releases, "1.1.0")["tag_name"] == "v1.1.0"
    assert dist.pick_release(releases, "v1.2.0")["tag_name"] == "v1.2.0"
    with pytest.raises(dist.DistError):
        dist.pick_release(releases, "9.9.9")


# --- D-02 ----------------------------------------------------------------

def test_pick_wheel_ignores_sdist():
    wheel = dist.pick_wheel(_release())
    assert wheel["name"] == WHEEL
    with pytest.raises(dist.DistError):
        dist.pick_wheel({"tag_name": "v1", "assets": []})


# --- D-03 ----------------------------------------------------------------

def test_resolve_uses_asset_digest():
    calls: list[str] = []

    def transport(url: str, token: str | None):
        calls.append(url)
        return _release()

    result = dist.resolve_release("acme/partner", "1.2.0", "secret", transport=transport)
    assert calls == ["https://api.github.com/repos/acme/partner/releases/tags/v1.2.0"]
    assert result["version"] == "1.2.0"
    assert result["sha256"] == SHA
    assert result["api_url"].endswith("/assets/11")
    assert result["browser_url"].endswith(WHEEL)


def test_resolve_latest_lists_releases():
    calls: list[str] = []

    def transport(url: str, token: str | None):
        calls.append(url)
        return [_release()]

    result = dist.resolve_release("acme/partner", "latest", None, transport=transport)
    assert calls == ["https://api.github.com/repos/acme/partner/releases"]
    assert result["tag"] == "v1.2.0"


# --- D-04 ----------------------------------------------------------------

def test_resolve_downloads_when_digest_missing(tmp_path):
    body = b"wheel-bytes"
    sha = hashlib.sha256(body).hexdigest()

    def transport(url: str, token: str | None):
        return _release(digest=None)

    def download(url: str, token: str | None, dest: Path):
        dest.write_bytes(body)
        return dest

    result = dist.resolve_release(
        "acme/partner", "1.2.0", None, transport=transport, download=download
    )
    assert result["sha256"] == sha


# --- D-05 ----------------------------------------------------------------

def test_manifest_matches_release_assets(tmp_path):
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"wheel")
    sdist = tmp_path / "partner_tracker-1.2.0.tar.gz"
    sdist.write_bytes(b"sdist")
    wheel_sha = hashlib.sha256(b"wheel").hexdigest()
    sdist_sha = hashlib.sha256(b"sdist").hexdigest()
    release = _release(digest=f"sha256:{wheel_sha}")
    release["assets"][1]["digest"] = f"sha256:{sdist_sha}"

    manifest = dist.build_manifest(
        tmp_path, release, "acme/partner", generated_at="2026-11-14T00:00:00+00:00"
    )
    names = [asset["name"] for asset in manifest["assets"]]
    assert names == [WHEEL, "partner_tracker-1.2.0.tar.gz"]
    assert manifest["tag"] == "v1.2.0"
    assert manifest["version"] == "1.2.0"
    assert manifest["generated_at"] == "2026-11-14T00:00:00+00:00"
    assert all(asset["api_url"] for asset in manifest["assets"])


def test_manifest_rejects_missing_or_tampered_asset(tmp_path):
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"wheel")
    release = _release()
    release["assets"] = []
    with pytest.raises(dist.DistError):
        dist.build_manifest(tmp_path, release, "acme/partner")

    release = _release(digest="sha256:" + "0" * 64)
    with pytest.raises(dist.DistError):
        dist.build_manifest(tmp_path, release, "acme/partner")


def test_download_wheel_verifies_sha(tmp_path):
    resolution = {"api_url": "https://api.example/asset", "wheel": WHEEL, "sha256": SHA}

    def bad(url: str, token: str | None, dest: Path):
        dest.write_bytes(b"bad")
        return dest

    with pytest.raises(dist.DistError):
        dist.download_wheel(resolution, tmp_path, None, download=bad)
    assert not (tmp_path / WHEEL).exists()

    body = b"good"
    resolution["sha256"] = hashlib.sha256(body).hexdigest()

    def good(url: str, token: str | None, dest: Path):
        dest.write_bytes(body)
        return dest

    path = dist.download_wheel(resolution, tmp_path, None, download=good)
    assert path.read_bytes() == body


# --- D-07 ----------------------------------------------------------------

def test_public_release_commands_use_browser_url_without_token():
    resolution = dist.resolve_release(
        "acme/partner", "1.2.0", None, transport=lambda url, token: _release()
    )
    commands = dist.gh_secret_commands(resolution, "acme/site")
    assert any("PARTNER_TRACKER_WHEEL" in command for command in commands)
    assert all("api.github.com" not in command for command in commands)
    assert any(resolution["browser_url"] in command for command in commands)
    assert not any("PARTNER_TRACKER_TOKEN" in command for command in commands)


def test_private_release_commands_use_api_url_and_token():
    resolution = dist.resolve_release(
        "acme/partner", "1.2.0", "secret", transport=lambda url, token: _release()
    )
    commands = dist.gh_secret_commands(resolution, "acme/site", authenticated=True)
    assert any(resolution["api_url"] in command for command in commands)
    assert any("PARTNER_TRACKER_TOKEN" in command for command in commands)
    rendered = dist.render_resolution(
        resolution, "acme/site", authenticated=True
    )
    assert "PARTNER_TRACKER_TOKEN" in rendered
    assert "токен не требуется" not in rendered


def test_render_resolution_does_not_require_pat_for_public_release():
    resolution = dist.resolve_release(
        "acme/partner", "1.2.0", None, transport=lambda url, token: _release()
    )
    rendered = dist.render_resolution(resolution, "acme/site")
    assert resolution["browser_url"] in rendered
    assert "PARTNER_TRACKER_TOKEN" not in rendered
    assert "PARTNER_TRACKER_SHA256" in rendered


def test_download_wheel_uses_api_asset_for_public_and_private(tmp_path):
    resolution = {
        "api_url": "https://api.example/asset",
        "browser_url": "https://github.example/releases/download/v1/w.whl",
        "wheel": WHEEL,
        "sha256": hashlib.sha256(b"good").hexdigest(),
    }
    urls: list[str] = []

    def good(url: str, token: str | None, dest: Path):
        urls.append(url)
        dest.write_bytes(b"good")
        return dest

    dist.download_wheel(resolution, tmp_path, None, download=good)
    assert urls == [resolution["api_url"]]

    urls.clear()
    dist.download_wheel(resolution, tmp_path, "secret", download=good)
    assert urls == [resolution["api_url"]]


# --- D-06 ----------------------------------------------------------------

def test_repo_pack_workflow_fails_closed():
    workflow = yaml.safe_load(
        (REPO / "repo-pack" / "workflows" / "partner-policy-check.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["policy-lint"]["steps"]
    install = next(
        step for step in steps if "Установить partner-tracker" in step.get("name", "")
    )
    assert "PARTNER_TRACKER_TOKEN" in install["env"]
    assert "PARTNER_TRACKER_SHA256" in install["env"]
    run = install["run"]
    assert "application/octet-stream" in run
    assert "sha256sum -c" in run
    assert "pip install partner-tracker" not in run
    assert "::error::" in run
