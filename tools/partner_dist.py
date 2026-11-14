#!/usr/bin/env python3
"""Поставка partner-tracker: wheel из GitHub Release и манифест релиза.

resolve — найти собранный wheel в GitHub Release и вывести публичный URL,
sha256 и команды настройки клиентского репозитория.
manifest — собрать `dist-manifest.json` из файлов dist/ и метаданных
опубликованного релиза (используется release-workflow).

Релиз публичный: токен не требуется. Для приватного зеркала или обхода
лимитов GitHub API можно передать `--token`, `GITHUB_TOKEN` или `GH_TOKEN`
(fine-grained PAT, Contents: Read-only). Токен не печатается. При
аутентифицированном resolve команды клиента используют API URL и
`PARTNER_TRACKER_TOKEN`; для публичного — browser URL без токена.

Запуск:
  python tools/partner_dist.py resolve --version latest --target-repo acme/site
  python tools/partner_dist.py resolve --version 1.3.0 --download dist-release
  python tools/partner_dist.py manifest --dist dist --tag v1.3.0 --out dist-manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

DEFAULT_REPO = "biznesemarket/partner"
WHEEL_SUFFIX = "-py3-none-any.whl"
JSON_ACCEPT = "application/vnd.github+json"
BINARY_ACCEPT = "application/octet-stream"


class DistError(Exception):
    """Ошибка поставки: нет релиза/артефакта, нет доступа, битый хеш."""


def _token(explicit: str | None = None) -> str | None:
    return explicit or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or None


def _headers(token: str | None, accept: str = JSON_ACCEPT) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "partner-dist/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_json(url: str, token: str | None) -> object:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        raise DistError(
            f"GitHub API {err.code}: {detail}\n"
            "Для приватного репозитория задайте GH_TOKEN (fine-grained PAT, Contents: Read)."
        ) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise DistError(f"GitHub API недоступен: {err}") from err


def download_asset(api_url: str, token: str | None, dest: pathlib.Path) -> pathlib.Path:
    """Скачать release asset: API URL приватного repo или browser URL публичного."""
    request = urllib.request.Request(
        api_url, headers=_headers(token, accept=BINARY_ACCEPT)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        raise DistError(f"Release asset {err.code}: {detail}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise DistError(f"Release asset недоступен: {err}") from err
    dest.write_bytes(body)
    return dest


def _api(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"


def fetch_releases(
    repo: str, token: str | None, *, transport: Callable[[str, str | None], object] = http_json
) -> list[dict]:
    data = transport(f"{_api(repo)}/releases", token)
    if not isinstance(data, list):
        raise DistError("GitHub API вернул не список релизов")
    return data


def fetch_release(
    repo: str,
    tag: str,
    token: str | None,
    *,
    transport: Callable[[str, str | None], object] = http_json,
) -> dict:
    data = transport(f"{_api(repo)}/releases/tags/{tag}", token)
    if not isinstance(data, dict):
        raise DistError(f"GitHub API не вернул релиз {tag}")
    return data


def pick_release(releases: list[dict], version: str) -> dict:
    """Выбрать опубликованный релиз: latest или точный тег vX.Y.Z/X.Y.Z."""
    published = [
        item for item in releases if not item.get("draft") and not item.get("prerelease")
    ]
    if not published:
        raise DistError("нет опубликованных релизов")
    if version in ("", "latest"):
        # GitHub отдает новые первыми, но не полагаемся на порядок API.
        return sorted(
            published,
            key=lambda item: str(item.get("published_at") or item.get("created_at") or ""),
            reverse=True,
        )[0]
    tag = version if version.startswith("v") else f"v{version}"
    for item in published:
        if item.get("tag_name") == tag:
            return item
    for item in published:
        if item.get("tag_name") == version:
            return item
    raise DistError(f"релиз {tag} не найден")


def pick_wheel(release: dict) -> dict:
    assets = release.get("assets") or []
    wheels = [
        asset
        for asset in assets
        if str(asset.get("name", "")).startswith("partner_tracker-")
        and str(asset.get("name", "")).endswith(WHEEL_SUFFIX)
    ]
    if not wheels:
        tag = release.get("tag_name", "?")
        raise DistError(f"в релизе {tag} нет wheel partner_tracker-*{WHEEL_SUFFIX}")
    return sorted(wheels, key=lambda asset: str(asset.get("name")))[-1]


def asset_sha256(asset: dict) -> str | None:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    return None


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_from_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def resolve_release(
    repo: str,
    version: str,
    token: str | None,
    *,
    transport: Callable[[str, str | None], object] = http_json,
    download: Callable[[str, str | None, pathlib.Path], pathlib.Path] = download_asset,
) -> dict:
    if version in ("", "latest"):
        release = pick_release(fetch_releases(repo, token, transport=transport), version)
    else:
        tag = version if version.startswith("v") else f"v{version}"
        release = fetch_release(repo, tag, token, transport=transport)
    wheel = pick_wheel(release)
    sha = asset_sha256(wheel)
    if not sha:
        with tempfile.TemporaryDirectory() as tmp:
            path = download(str(wheel["url"]), token, pathlib.Path(tmp) / str(wheel["name"]))
            sha = sha256_file(path)
    return {
        "repo": repo,
        "tag": str(release.get("tag_name")),
        "version": version_from_tag(str(release.get("tag_name"))),
        "wheel": str(wheel.get("name")),
        "size": wheel.get("size"),
        "asset_id": wheel.get("id"),
        "api_url": wheel.get("url"),
        "browser_url": wheel.get("browser_download_url"),
        "sha256": sha,
    }


def delivery_url(resolution: dict) -> str:
    """Публичный URL для клиента: browser URL релиза, иначе API URL."""
    return str(resolution.get("browser_url") or resolution["api_url"])


def gh_secret_commands(
    resolution: dict, target_repo: str, *, authenticated: bool = False
) -> list[str]:
    """Команды настройки клиентского репозитория.

    Публичный release: browser URL + sha256, токен не нужен.
    Приватное зеркало/API: api_url + sha256 + PARTNER_TRACKER_TOKEN.
    """
    wheel_url = (
        str(resolution["api_url"]) if authenticated else delivery_url(resolution)
    )
    commands = [
        f'gh secret set PARTNER_TRACKER_WHEEL --repo {target_repo} '
        f'--body "{wheel_url}"',
        f'gh secret set PARTNER_TRACKER_SHA256 --repo {target_repo} '
        f'--body "{resolution["sha256"]}"',
    ]
    if authenticated:
        commands.append(
            f'gh secret set PARTNER_TRACKER_TOKEN --repo {target_repo}  '
            f'# fine-grained PAT: Contents: Read-only на '
            f'{resolution.get("repo", "<vendor-repo>")}'
        )
    return commands


def download_wheel(
    resolution: dict,
    dest_dir: pathlib.Path,
    token: str | None,
    *,
    download: Callable[[str, str | None, pathlib.Path], pathlib.Path] = download_asset,
) -> pathlib.Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # API asset отдает байты и для публичного репозитория; browser URL
    # остается для команд клиента и ручного скачивания.
    path = download(str(resolution["api_url"]), token, dest_dir / str(resolution["wheel"]))
    actual = sha256_file(path)
    if actual != resolution["sha256"]:
        path.unlink(missing_ok=True)
        raise DistError(
            f"sha256 не совпал: ожидался {resolution['sha256']}, получен {actual}"
        )
    return path


def build_manifest(
    dist_dir: pathlib.Path,
    release: dict,
    repo: str,
    *,
    generated_at: str | None = None,
) -> dict:
    """Манифест dist/: имена, размеры, sha256 и ссылки на release assets."""
    if not dist_dir.is_dir():
        raise DistError(f"каталог dist не найден: {dist_dir}")
    assets = {str(asset.get("name")): asset for asset in release.get("assets") or []}
    entries: list[dict] = []
    for path in sorted(dist_dir.iterdir()):
        if not path.is_file() or path.name == "dist-manifest.json":
            continue
        if path.name not in assets:
            raise DistError(f"файл {path.name} не приложен к релизу {release.get('tag_name')}")
        asset = assets[path.name]
        sha = sha256_file(path)
        expected = asset_sha256(asset)
        if expected and expected != sha:
            raise DistError(
                f"sha256 {path.name} не совпадает с релизом: {sha} != {expected}"
            )
        entries.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": sha,
                "api_url": asset.get("url"),
                "browser_url": asset.get("browser_download_url"),
            }
        )
    if not entries:
        raise DistError("в dist нет файлов для манифеста")
    tag = str(release.get("tag_name"))
    return {
        "schema": 1,
        "repo": repo,
        "tag": tag,
        "version": version_from_tag(tag),
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assets": entries,
    }


def render_resolution(
    resolution: dict, target_repo: str, *, authenticated: bool = False
) -> str:
    lines = [
        f"Поставка partner-tracker {resolution['version']} ({resolution['tag']})",
        f"  wheel:  {resolution['wheel']}",
        f"  url:    {delivery_url(resolution)}",
        f"  api:    {resolution['api_url']}",
        f"  sha256: {resolution['sha256']}",
        "",
        f"Секреты клиентского репозитория {target_repo}:",
        *[
            f"  {command}"
            for command in gh_secret_commands(
                resolution, target_repo, authenticated=authenticated
            )
        ],
        (
            "Приватный источник: токен нужен, задайте его отдельно."
            if authenticated
            else "Публичный релиз: токен не требуется."
        ),
    ]
    return "\n".join(lines)


def _write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _configure_stdio() -> None:
    """Не падать на Windows-консоли с cp1251 при выводе русских сообщений."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser("resolve", help="найти wheel и sha256 в релизе")
    resolve_parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo вендора")
    resolve_parser.add_argument("--version", default="latest", help="latest | 1.3.0 | v1.3.0")
    resolve_parser.add_argument(
        "--token",
        default=None,
        help="GitHub token для приватного зеркала (или GITHUB_TOKEN/GH_TOKEN)",
    )
    resolve_parser.add_argument("--target-repo", default="<client-repo>")
    resolve_parser.add_argument("--download", type=pathlib.Path, default=None, help="каталог для wheel")
    resolve_parser.add_argument("--out", type=pathlib.Path, default=None, help="записать JSON")
    resolve_parser.add_argument("--json", action="store_true", help="вывести JSON")
    resolve_parser.set_defaults(handler="resolve")

    manifest_parser = sub.add_parser("manifest", help="собрать dist-manifest.json")
    manifest_parser.add_argument("--dist", type=pathlib.Path, required=True)
    manifest_parser.add_argument("--tag", required=True)
    manifest_parser.add_argument("--repo", default=DEFAULT_REPO)
    manifest_parser.add_argument("--token", default=None)
    manifest_parser.add_argument("--out", type=pathlib.Path, required=True)
    manifest_parser.set_defaults(handler="manifest")

    args = parser.parse_args(argv)
    token = _token(args.token)
    try:
        if args.handler == "resolve":
            resolution = resolve_release(args.repo, args.version, token)
            resolution["authenticated"] = bool(token)
            if args.download:
                path = download_wheel(resolution, args.download, token)
                resolution["downloaded"] = str(path)
            if args.out:
                _write_json(args.out, resolution)
            if args.json:
                print(json.dumps(resolution, ensure_ascii=False, indent=2))
            else:
                print(
                    render_resolution(
                        resolution, args.target_repo, authenticated=bool(token)
                    )
                )
            return 0

        release = fetch_release(args.repo, args.tag, token)
        manifest = build_manifest(args.dist, release, args.repo)
        _write_json(args.out, manifest)
        print(
            f"манифест: {args.out} ({len(manifest['assets'])} файлов, "
            f"{manifest['tag']})"
        )
        return 0
    except DistError as err:
        print(f"ошибка поставки: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
