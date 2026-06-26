#!/usr/bin/env python3
"""
dp-publish.py — Publish datapack ZIPs to Modrinth and/or CurseForge.

Reads publish config from .depends/manifest.json:

  {
    "id": "my-pack",
    "version": "1.0.0",
    "publish": {
      "modrinth": {
        "project_id":    "XXXXXXXX",
        "game_versions": ["1.21.1"],
        "loaders":       ["datapack"],
        "channel":       "release"       // release | beta | alpha (default: release)
      },
      "curseforge": {
        "project_id":      123456,
        "game_version_ids": [10550],     // from CurseForge game version API
        "release_type":    "release"     // release | beta | alpha (default: release)
      }
    }
  }

Environment variables:
  MODRINTH_TOKEN    — Modrinth PAT
  CURSEFORGE_TOKEN  — CurseForge API key
"""

import argparse
import json
import mimetypes
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _dp_common import (
    OUTPUT_DIR,
    load_manifest,
    log, warn, die,
)

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_json(url: str, headers: dict, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        die(f"HTTP {e.code} {e.reason}\n  URL: {url}\n  Body: {body[:500]}")
    except urllib.error.URLError as e:
        die(f"Network error: {e.reason}")

def _multipart_post(url: str, headers: dict, fields: dict, files: dict) -> dict:
    """
    Minimal multipart/form-data POST — no external deps.
    files: { field_name: (filename, bytes, content_type) }
    fields: { field_name: str_value }
    """
    boundary = "dp_depman_boundary_xK9zQ2"
    body_parts = []

    for name, value in fields.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        )

    for name, (filename, data, ct) in files.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {ct}\r\n\r\n'
        )

    body = b"".join(
        (part.encode() if isinstance(part, str) else part)
        for part in body_parts
    )
    for name, (filename, data, ct) in files.items():
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    headers = dict(headers)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode(errors="replace")
        die(f"HTTP {e.code} {e.reason}\n  URL: {url}\n  Body: {body_resp[:500]}")
    except urllib.error.URLError as e:
        die(f"Network error: {e.reason}")

# ─── ZIP picker ───────────────────────────────────────────────────────────────

def find_pack_zip(root: Path, manifest: dict) -> Path:
    """Find the main pack ZIP in dist/."""
    out     = root / OUTPUT_DIR
    pack_id = manifest["id"]
    version = manifest["version"]

    # Exact name match first
    exact = out / f"{pack_id}-{version}.zip"
    if exact.exists():
        return exact

    # Fallback: any zip starting with pack_id that doesn't end in -merged.zip
    candidates = [
        f for f in out.glob(f"{pack_id}-*.zip")
        if not f.name.endswith("-merged.zip")
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        die(
            f"Multiple ZIPs found for '{pack_id}' in dist/ — specify one:\n"
            + "\n".join(f"  {c.name}" for c in candidates)
        )

    die(f"No ZIP found for '{pack_id}' in {out}/\n  Run: dp-resolve.py build")

# ─── Modrinth publish ─────────────────────────────────────────────────────────

def publish_modrinth(root: Path, manifest: dict, zip_path: Path, token: str, dry_run: bool):
    cfg = manifest.get("publish", {}).get("modrinth")
    if not cfg:
        die("No 'publish.modrinth' block in manifest.json")

    project_id    = cfg["project_id"]
    game_versions = cfg.get("game_versions", [])
    loaders       = cfg.get("loaders", ["datapack"])
    channel       = cfg.get("channel", "release")
    version       = manifest["version"]
    pack_id       = manifest["id"]

    if not game_versions:
        die("publish.modrinth.game_versions is empty")

    version_data = {
        "name":           f"{pack_id} {version}",
        "version_number": version,
        "changelog":      "",
        "dependencies":   [],
        "game_versions":  game_versions,
        "version_type":   channel,
        "loaders":        loaders,
        "featured":       False,
        "project_id":     project_id,
        "file_parts":     [zip_path.name],
    }

    log(f"Modrinth: project={project_id} version={version} channel={channel}")
    if dry_run:
        log("  [dry-run] Would POST to https://api.modrinth.com/v2/version")
        log(f"  [dry-run] file: {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")
        return

    headers = {
        "Authorization": token,
        "User-Agent":    "dp-depman/1.0",
    }

    result = _multipart_post(
        "https://api.modrinth.com/v2/version",
        headers,
        fields={"data": json.dumps(version_data)},
        files={
            zip_path.name: (
                zip_path.name,
                zip_path.read_bytes(),
                "application/zip",
            )
        },
    )

    version_id  = result.get("id", "?")
    version_url = f"https://modrinth.com/project/{project_id}/version/{version_id}"
    log(f"  Published: {version_url}")

# ─── CurseForge publish ───────────────────────────────────────────────────────

def publish_curseforge(root: Path, manifest: dict, zip_path: Path, token: str, dry_run: bool):
    cfg = manifest.get("publish", {}).get("curseforge")
    if not cfg:
        die("No 'publish.curseforge' block in manifest.json")

    project_id       = cfg["project_id"]
    game_version_ids = cfg.get("game_version_ids", [])
    release_type     = cfg.get("release_type", "release")
    version          = manifest["version"]
    pack_id          = manifest["id"]

    if not game_version_ids:
        die(
            "publish.curseforge.game_version_ids is empty.\n"
            "  Get version IDs from: https://api.curseforge.com/v1/games/432/versions"
        )

    metadata = {
        "changelog":        f"{pack_id} {version}",
        "changelogType":    "text",
        "displayName":      f"{pack_id} {version}",
        "gameVersions":     game_version_ids,
        "releaseType":      release_type,
    }

    log(f"CurseForge: project={project_id} version={version} type={release_type}")
    if dry_run:
        log(f"  [dry-run] Would POST to https://minecraft.curseforge.com/api/projects/{project_id}/upload-file")
        log(f"  [dry-run] file: {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")
        return

    headers = {"x-api-key": token}

    result = _multipart_post(
        f"https://minecraft.curseforge.com/api/projects/{project_id}/upload-file",
        headers,
        fields={"metadata": json.dumps(metadata)},
        files={
            "file": (
                zip_path.name,
                zip_path.read_bytes(),
                "application/zip",
            )
        },
    )

    file_id  = result.get("id", "?")
    file_url = f"https://www.curseforge.com/minecraft/mc-mods/{project_id}/files/{file_id}"
    log(f"  Published: {file_url}")

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Publish datapack to Modrinth and/or CurseForge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Targets:
  modrinth      Requires MODRINTH_TOKEN env var
  curseforge    Requires CURSEFORGE_TOKEN env var
  all           Both platforms

Examples:
  dp-publish.py modrinth
  dp-publish.py curseforge
  dp-publish.py all
  dp-publish.py all --dry-run
        """,
    )
    parser.add_argument("target", choices=["modrinth", "curseforge", "all"])
    parser.add_argument("--root",    default=".")
    parser.add_argument("--zip",     help="Override ZIP path (default: auto-detect from dist/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be published without uploading")
    args = parser.parse_args()

    root     = Path(args.root).resolve()
    manifest = load_manifest(root)
    zip_path = Path(args.zip) if args.zip else find_pack_zip(root, manifest)

    log(f"Project: {manifest['id']} v{manifest['version']}")
    log(f"ZIP:     {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")

    mr_token = os.environ.get("MODRINTH_TOKEN")
    cf_token = os.environ.get("CURSEFORGE_TOKEN")

    if args.target in ("modrinth", "all"):
        if not mr_token and not args.dry_run:
            die("MODRINTH_TOKEN env var not set")
        publish_modrinth(root, manifest, zip_path, mr_token or "", args.dry_run)

    if args.target in ("curseforge", "all"):
        if not cf_token and not args.dry_run:
            die("CURSEFORGE_TOKEN env var not set")
        publish_curseforge(root, manifest, zip_path, cf_token or "", args.dry_run)

    log("Done.")

if __name__ == "__main__":
    main()
