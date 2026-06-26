#!/usr/bin/env python3
"""
dp-resolve.py — Datapack Dependency Resolver

.depends/ layout inside the output ZIP:
  my-pack-1.0.0.zip
  ├── pack.mcmeta
  ├── data/
  └── .depends/
      ├── manifest.json          ← this pack's identity
      └── dataLib-6.0.0.zip      ← dependency ZIPs embedded here

Sources: github | modrinth | curseforge | submodule
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _dp_common import (
    CACHE_DIR, DEPENDS_DIR, LOCK_FILE, OUTPUT_DIR, SUBMODULE_DIR,
    load_manifest, load_all_deps, load_lock, save_lock,
    log, warn, die,
)

# ─── Version comparison ───────────────────────────────────────────────────────

def parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("v")
    result = []
    for p in re.split(r"[.\-]", v):
        try:
            result.append(int(p))
        except ValueError:
            pass
    return tuple(result)

def version_matches(version: str, constraint: str) -> bool:
    version = version.lstrip("v")
    v = parse_version(version)

    if re.match(r"^\d+\.\d+(\.\d+)*$", constraint):
        return v == parse_version(constraint)
    if constraint.startswith(">="):
        return v >= parse_version(constraint[2:])
    if constraint.startswith("<="):
        return v <= parse_version(constraint[2:])
    if constraint.startswith(">") and not constraint.startswith(">="):
        return v > parse_version(constraint[1:])
    if constraint.startswith("<") and not constraint.startswith("<="):
        return v < parse_version(constraint[1:])
    if re.match(r"^\d+\.\d+\.x$", constraint):
        return parse_version(version)[:2] == parse_version(constraint[:-2])[:2]
    if constraint.startswith("^"):
        base = parse_version(constraint[1:])
        return v[0] == base[0] and v >= base
    if constraint.startswith("~"):
        base = parse_version(constraint[1:])
        return v[:2] == base[:2] and v >= base

    raise ValueError(f"Unsupported constraint: '{constraint}'")

# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code} {e.reason}: {url}")
    except urllib.error.URLError as e:
        die(f"Network error: {e.reason}: {url}")

def _download(url: str, dest: Path, headers: dict | None = None) -> str:
    """Download url → dest, return SHA-256 hex."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=headers or {})
    log(f"  Downloading: {url}")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            while chunk := resp.read(65536):
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        die(f"Download failed HTTP {e.code}: {url}")
    except urllib.error.URLError as e:
        die(f"Download failed: {e.reason}: {url}")
    return h.hexdigest()

# ─── GitHub resolution ────────────────────────────────────────────────────────

def resolve_github(dep_id: str, dep_cfg: dict, token: str | None) -> dict:
    repo             = dep_cfg["repo"]
    owner, repo_name = repo.split("/", 1)
    constraint       = dep_cfg["version"]
    wanted_asset     = dep_cfg.get("asset")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    log(f"[{dep_id}] GitHub: {repo} ({constraint})")
    releases = json.loads(_http_get(
        f"https://api.github.com/repos/{owner}/{repo_name}/releases", headers
    ))

    candidates = []
    for rel in releases:
        ver = rel.get("tag_name", "").lstrip("v")
        try:
            if version_matches(ver, constraint):
                candidates.append((ver, rel))
        except ValueError:
            pass

    if not candidates:
        die(f"[{dep_id}] No release satisfies '{constraint}'")

    candidates.sort(key=lambda x: parse_version(x[0]), reverse=True)
    chosen_ver, chosen_rel = candidates[0]
    log(f"  → v{chosen_ver} selected")

    assets = chosen_rel.get("assets", [])
    if wanted_asset:
        asset = next((a for a in assets if a["name"] == wanted_asset), None)
        if not asset:
            die(f"[{dep_id}] Asset '{wanted_asset}' not found in v{chosen_ver}")
    else:
        asset = next((a for a in assets if a["name"].endswith(".zip")), None)
        if not asset:
            die(f"[{dep_id}] No ZIP asset in v{chosen_ver}")

    return {
        "source":       "github",
        "repo":         repo,
        "version":      chosen_ver,
        "asset_name":   asset["name"],
        "download_url": asset["browser_download_url"],
        "sha256":       None,
        "_dl_headers":  {"Authorization": f"Bearer {token}"} if token else {},
    }

# ─── Modrinth resolution ──────────────────────────────────────────────────────

def resolve_modrinth(dep_id: str, dep_cfg: dict) -> dict:
    """
    dep_cfg — three usage modes:

    1. Pinned version_id (no API call):
       { "source": "modrinth", "project": "ZS3lIxKu",
         "version_id": "dtMspp4A", "version": "dtMspp4A",
         "asset": "data_api.zip",
         "download_url": "https://cdn.modrinth.com/..." }

    2. Pinned version_id, resolve asset via API:
       { "source": "modrinth", "project": "ZS3lIxKu",
         "version_id": "dtMspp4A", "version": "dtMspp4A" }

    3. Constraint-based (resolves latest matching):
       { "source": "modrinth", "project": "datalib", "version": ">=6.0.0" }
    """
    project    = dep_cfg["project"]
    version_id = dep_cfg.get("version_id")
    dl_url     = dep_cfg.get("download_url")
    asset_name = dep_cfg.get("asset", f"{dep_id}.zip")
    headers    = {"User-Agent": "dp-depman/1.0 (github.com/runtoolkit)"}

    # Mode 1: direct URL provided — skip all API calls
    if version_id and dl_url:
        log(f"[{dep_id}] Modrinth: pinned {version_id} (direct URL)")
        return {
            "source":       "modrinth",
            "project":      project,
            "version":      version_id,
            "asset_name":   asset_name,
            "download_url": dl_url,
            "sha256":       dep_cfg.get("sha256"),
            "_dl_headers":  headers,
        }

    # Mode 2: pinned version_id, fetch asset URL from API
    if version_id:
        log(f"[{dep_id}] Modrinth: fetching version {version_id}")
        v = json.loads(_http_get(
            f"https://api.modrinth.com/v2/version/{version_id}", headers
        ))
        files = v.get("files", [])
        zf = next((f for f in files if f.get("filename", "").endswith(".zip")), None)
        if not zf:
            die(f"[{dep_id}] No ZIP in Modrinth version {version_id}")
        log(f"  → {zf['filename']}")
        return {
            "source":       "modrinth",
            "project":      project,
            "version":      version_id,
            "asset_name":   zf["filename"],
            "download_url": zf["url"],
            "sha256":       zf.get("hashes", {}).get("sha256"),
            "_dl_headers":  headers,
        }

    # Mode 3: constraint-based — resolve latest matching version
    constraint = dep_cfg.get("version", "*")
    log(f"[{dep_id}] Modrinth: {project} ({constraint})")
    versions = json.loads(_http_get(
        f"https://api.modrinth.com/v2/project/{project}/version", headers
    ))

    candidates = []
    for v in versions:
        ver = v.get("version_number", "").lstrip("v")
        try:
            if version_matches(ver, constraint):
                candidates.append((ver, v))
        except ValueError:
            pass

    if not candidates:
        die(f"[{dep_id}] No Modrinth version satisfies '{constraint}'")

    candidates.sort(key=lambda x: parse_version(x[0]), reverse=True)
    chosen_ver, chosen_v = candidates[0]
    log(f"  → v{chosen_ver} selected")

    files = chosen_v.get("files", [])
    zf = next((f for f in files if f.get("filename", "").endswith(".zip")), None)
    if not zf:
        die(f"[{dep_id}] No ZIP in Modrinth version {chosen_ver}")

    return {
        "source":       "modrinth",
        "project":      project,
        "version":      chosen_ver,
        "asset_name":   zf["filename"],
        "download_url": zf["url"],
        "sha256":       zf.get("hashes", {}).get("sha256"),
        "_dl_headers":  headers,
    }

# ─── CurseForge resolution ────────────────────────────────────────────────────

def resolve_curseforge(dep_id: str, dep_cfg: dict, cf_token: str | None) -> dict:
    """
    dep_cfg:
      {
        "source":     "curseforge",
        "project_id": 123456,
        "version":    ">=6.0.0"
      }

    Requires CURSEFORGE_TOKEN env var (CurseForge API key).
    """
    if not cf_token:
        die(
            f"[{dep_id}] CurseForge resolution requires CURSEFORGE_TOKEN env var.\n"
            f"  Get an API key at: https://console.curseforge.com/"
        )

    project_id = dep_cfg["project_id"]
    constraint = dep_cfg["version"]

    headers = {
        "Accept":    "application/json",
        "x-api-key": cf_token,
    }

    log(f"[{dep_id}] CurseForge: project {project_id} ({constraint})")

    # Fetch all files for the project
    data = json.loads(_http_get(
        f"https://api.curseforge.com/v1/mods/{project_id}/files?pageSize=50",
        headers,
    ))
    files = data.get("data", [])

    candidates = []
    for f in files:
        display = f.get("displayName", "")
        # CurseForge doesn't have a clean version field — parse from displayName
        match = re.search(r"(\d+\.\d+[\.\d]*)", display)
        if not match:
            continue
        ver = match.group(1)
        try:
            if version_matches(ver, constraint):
                candidates.append((ver, f))
        except ValueError:
            pass

    if not candidates:
        die(f"[{dep_id}] No CurseForge file satisfies '{constraint}'")

    candidates.sort(key=lambda x: parse_version(x[0]), reverse=True)
    chosen_ver, chosen_f = candidates[0]
    log(f"  → v{chosen_ver} selected (file id: {chosen_f['id']})")

    return {
        "source":       "curseforge",
        "project_id":   project_id,
        "file_id":      chosen_f["id"],
        "version":      chosen_ver,
        "asset_name":   chosen_f.get("fileName", f"{dep_id}-{chosen_ver}.zip"),
        "download_url": chosen_f.get("downloadUrl"),
        "sha256":       None,
        "_dl_headers":  headers,
    }

# ─── Submodule resolution ─────────────────────────────────────────────────────

def resolve_submodule(dep_id: str, dep_cfg: dict, root: Path) -> dict:
    rel_path = dep_cfg.get("path", str(SUBMODULE_DIR / dep_id))
    abs_path = root / rel_path

    if not abs_path.exists():
        die(
            f"[{dep_id}] Submodule not found: {rel_path}\n"
            f"  Run: git submodule add <url> {rel_path}"
        )

    detected_ver = "unknown"
    for candidate in [
        abs_path / DEPENDS_DIR / "manifest.json",
        abs_path / "datapack_depends.json",
        abs_path / "pack.mcmeta",
    ]:
        if not candidate.exists():
            continue
        try:
            with open(candidate) as f:
                data = json.load(f)
            ver = data.get("version") or data.get("pack", {}).get("version")
            if ver:
                detected_ver = str(ver)
                break
        except Exception:
            continue

    constraint = dep_cfg.get("version", "*")
    if constraint != "*" and detected_ver != "unknown":
        try:
            if not version_matches(detected_ver, constraint):
                die(
                    f"[{dep_id}] Submodule v{detected_ver} does not satisfy '{constraint}'\n"
                    f"  Update: git submodule update --remote {rel_path}"
                )
        except ValueError as e:
            warn(f"[{dep_id}] Constraint check skipped: {e}")

    log(f"  → {dep_id} submodule v{detected_ver} ({rel_path})")
    return {"source": "submodule", "path": rel_path, "version": detected_ver}

# ─── Direct URL resolution ───────────────────────────────────────────────────

def resolve_url(dep_id: str, dep_cfg: dict) -> dict:
    """
    dep_cfg:
      {
        "source":     "url",
        "url":        "https://cdn.modrinth.com/...",
        "version":    "1.0.0",
        "asset_name": "data_api.zip"   ← optional, inferred from URL if absent
      }
    """
    url     = dep_cfg.get("url")
    version = dep_cfg.get("version", "unknown")

    if not url:
        die(f"[{dep_id}] Missing 'url' for source=url")

    # Infer asset_name from URL path (strip query string)
    asset_name = dep_cfg.get("asset_name") or url.split("?")[0].split("/")[-1]
    if not asset_name.endswith(".zip"):
        asset_name = f"{dep_id}-{version}.zip"

    log(f"[{dep_id}] URL: {url[:80]}{'...' if len(url) > 80 else ''}")
    return {
        "source":       "url",
        "version":      version,
        "asset_name":   asset_name,
        "download_url": url,
        "sha256":       dep_cfg.get("sha256"),
        "_dl_headers":  {},
    }

# ─── Resolution dispatcher ────────────────────────────────────────────────────

def resolve_all(deps: dict, root: Path, mode: str, tokens: dict) -> dict:
    """
    tokens: {
      "github":     "ghp_...",
      "curseforge": "...",
    }
    """
    if not deps:
        log("No dependencies declared.")
        return {}

    results = {}
    for dep_id, dep_cfg in deps.items():
        if isinstance(dep_cfg, str):
            dep_cfg = {"source": "auto", "version": dep_cfg}

        source = dep_cfg.get("source", "auto")

        # mode override
        if mode == "dev":
            effective = "submodule"
        elif mode == "prod":
            effective = source if source in ("github", "modrinth", "curseforge", "url") else "github"
        else:
            effective = source  # auto

        if effective == "github":
            if "repo" not in dep_cfg:
                die(f"[{dep_id}] Missing 'repo' for source=github")
            info = resolve_github(dep_id, dep_cfg, tokens.get("github"))

        elif effective == "modrinth":
            if "project" not in dep_cfg:
                die(f"[{dep_id}] Missing 'project' for source=modrinth")
            info = resolve_modrinth(dep_id, dep_cfg)

        elif effective == "curseforge":
            if "project_id" not in dep_cfg:
                die(f"[{dep_id}] Missing 'project_id' for source=curseforge")
            info = resolve_curseforge(dep_id, dep_cfg, tokens.get("curseforge"))

        elif effective == "submodule":
            info = resolve_submodule(dep_id, dep_cfg, root)

        elif effective == "url":
            info = resolve_url(dep_id, dep_cfg)

        else:
            die(f"[{dep_id}] Unknown source: '{effective}'")

        results[dep_id] = info

    return results

# ─── Cache + download ─────────────────────────────────────────────────────────

def get_dep_zip(dep_id: str, info: dict, root: Path) -> Path:
    """Return path to the dependency ZIP (cached or freshly built/downloaded)."""

    if info["source"] == "submodule":
        src       = root / info["path"]
        cache_zip = CACHE_DIR / f"{dep_id}-{info['version']}-submodule.zip"
        cache_zip.parent.mkdir(parents=True, exist_ok=True)
        # Always rebuild submodule ZIPs (source may have changed)
        with zipfile.ZipFile(cache_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in src.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(src))
        return cache_zip

    asset_name = info.get("asset_name", f"{dep_id}-{info['version']}.zip")
    cache_zip  = CACHE_DIR / f"{dep_id}-{info['version']}-{asset_name}"
    cache_zip.parent.mkdir(parents=True, exist_ok=True)

    if cache_zip.exists():
        # Verify SHA-256 if we have it
        expected = info.get("sha256")
        if expected:
            actual = hashlib.sha256(cache_zip.read_bytes()).hexdigest()
            if actual != expected:
                warn(f"  Cache SHA-256 mismatch for {dep_id}, re-downloading")
                cache_zip.unlink()
            else:
                log(f"  Using cached: {cache_zip.name}")
                return cache_zip
        else:
            log(f"  Using cached: {cache_zip.name}")
            return cache_zip

    dl_url = info.get("download_url")
    if not dl_url:
        die(f"[{dep_id}] No download URL available")

    headers = info.get("_dl_headers", {})
    sha     = _download(dl_url, cache_zip, headers)

    if info.get("sha256") and sha != info["sha256"]:
        cache_zip.unlink()
        die(f"[{dep_id}] SHA-256 mismatch after download — aborting")

    info["sha256"] = sha
    log(f"  SHA-256: {sha}")
    return cache_zip

# ─── Pack root detection ──────────────────────────────────────────────────────

def _get_pack_root(manifest: dict, root: Path) -> Path:
    if "pack_root" in manifest:
        return root / manifest["pack_root"]
    for candidate in [root] + [p for p in root.iterdir() if p.is_dir()]:
        if (candidate / "pack.mcmeta").exists():
            if candidate != root:
                log(f"Pack root: {candidate.relative_to(root)}/")
            return candidate
    return root

def _pack_files(pack_root: Path):
    """Yield (file, arc_name) for files that belong in the output ZIP."""
    for file in pack_root.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(pack_root)
        # Only pack.mcmeta and data/ — everything else is repo metadata
        if rel.parts[0] in {"data", "pack.mcmeta"}:
            yield file, str(rel)

# ─── Build ────────────────────────────────────────────────────────────────────

def _build_merged_zip(manifest: dict, resolved: dict, root: Path, pack_root: Path, out: Path) -> Path:
    """
    Produce a single self-contained ZIP:

      <id>-<version>-merged.zip
      ├── pack.mcmeta
      ├── data/
      └── .depends/
          ├── manifest.json
          └── <dep_id>-<version>.zip   (one per dependency)
    """
    zip_name = f"{manifest['id']}-{manifest['version']}-merged.zip"
    zip_path = out / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Pack files (pack.mcmeta + data/)
        for file, arc in _pack_files(pack_root):
            zf.write(file, arc)

        # 2. .depends/manifest.json — this pack's own identity
        dep_manifest = {
            "id":      manifest["id"],
            "version": manifest["version"],
        }
        if manifest.get("description"):
            dep_manifest["description"] = manifest["description"]
        zf.writestr(
            f"{DEPENDS_DIR}/manifest.json",
            json.dumps(dep_manifest, indent=2) + "\n",
        )

        # 3. .depends/<dep_id>-<version>.zip — one per dependency
        for dep_id, info in resolved.items():
            dep_zip_path = get_dep_zip(dep_id, info, root)
            arc_name     = f"{DEPENDS_DIR}/{dep_id}-{info['version']}.zip"
            zf.write(dep_zip_path, arc_name)
            log(f"  Embedded: {arc_name}")

    log(f"Output: {zip_path.name}")
    return zip_path


def _build_separate_zips(manifest: dict, resolved: dict, root: Path, pack_root: Path, out: Path) -> list[Path]:
    """
    Produce one ZIP per pack (main pack + each dependency), meant to be
    dropped side-by-side into world/datapacks/. No embedding/merging.

      <id>-<version>.zip            (main pack only — pack.mcmeta + data/)
      <dep_id>-<version>.zip        (one per dependency, copied as-is)
    """
    paths = []

    main_zip_name = f"{manifest['id']}-{manifest['version']}.zip"
    main_zip_path = out / main_zip_name
    with zipfile.ZipFile(main_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file, arc in _pack_files(pack_root):
            zf.write(file, arc)
    log(f"Output: {main_zip_path.name}")
    paths.append(main_zip_path)

    for dep_id, info in resolved.items():
        dep_zip_path = get_dep_zip(dep_id, info, root)
        dest_name    = f"{dep_id}-{info['version']}.zip"
        dest_path    = out / dest_name
        shutil.copyfile(dep_zip_path, dest_path)
        log(f"Output: {dest_path.name}")
        paths.append(dest_path)

    return paths


def build_pack_zip(manifest: dict, resolved: dict, root: Path, output_mode: str = "merged") -> list[Path]:
    """
    Produce build output(s) in dist/ according to output_mode:

      merged    — single self-contained ZIP with deps embedded under .depends/
      separate  — one ZIP per pack (main + each dep), no embedding
      both      — produce both of the above

    Returns the list of produced ZIP paths.
    """
    out = root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    pack_root = _get_pack_root(manifest, root)

    produced: list[Path] = []
    if output_mode in ("merged", "both"):
        produced.append(_build_merged_zip(manifest, resolved, root, pack_root, out))
    if output_mode in ("separate", "both"):
        produced.extend(_build_separate_zips(manifest, resolved, root, pack_root, out))

    return produced

# ─── Submodule init ───────────────────────────────────────────────────────────

def cmd_init_submodules(deps: dict, root: Path):
    for dep_id, dep_cfg in deps.items():
        if isinstance(dep_cfg, str) or dep_cfg.get("source") != "submodule":
            continue
        url      = dep_cfg.get("url")
        if not url:
            warn(f"[{dep_id}] Missing 'url' — cannot add submodule")
            continue
        path     = dep_cfg.get("path", str(SUBMODULE_DIR / dep_id))
        abs_path = root / path
        if abs_path.exists():
            log(f"[{dep_id}] Already present: {path}")
            continue
        log(f"[{dep_id}] git submodule add {url} {path}")
        result = subprocess.run(
            ["git", "submodule", "add", url, path],
            cwd=root, capture_output=True, text=True,
        )
        if result.returncode != 0:
            die(f"git submodule add failed:\n{result.stderr}")

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Datapack dependency resolver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  resolve   Resolve dependencies and write lock file
  build     Resolve + produce output ZIP in dist/
  init      Add submodule deps via git submodule add
  check     Verify constraints against lock file (no network)

Sources:
  github      repo: owner/repo, version: constraint, asset: filename (optional)
  modrinth    project: slug-or-id, version: constraint
  curseforge  project_id: 123456, version: constraint
  submodule   path: deps/lib, url: https://..., version: constraint

Examples:
  dp-resolve.py build --mode prod
  dp-resolve.py build --mode dev
  dp-resolve.py build --mode prod --output both
  dp-resolve.py resolve --mode prod
  dp-resolve.py check
        """,
    )
    parser.add_argument("command", choices=["resolve", "build", "init", "check"])
    parser.add_argument("--mode",   choices=["prod", "dev", "auto"], default="auto")
    parser.add_argument("--output", choices=["separate", "merged", "both"], default=None,
                        help="build: ZIP output mode (default: manifest's build.output, "
                             "else 'merged')")
    parser.add_argument("--root",  default=".")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub token (default: $GITHUB_TOKEN)")
    args = parser.parse_args()

    root     = Path(args.root).resolve()
    manifest = load_manifest(root)
    deps     = load_all_deps(root)

    log(f"Project: {manifest['id']} v{manifest['version']}")

    tokens = {
        "github":     args.token or os.environ.get("GITHUB_TOKEN"),
        "curseforge": os.environ.get("CURSEFORGE_TOKEN"),
    }

    if args.command == "init":
        cmd_init_submodules(deps, root)
        return

    if args.command == "check":
        lock          = load_lock(root)
        resolved_lock = lock.get("resolved", {})
        ok = True
        for dep_id, dep_cfg in deps.items():
            if isinstance(dep_cfg, str):
                dep_cfg = {"version": dep_cfg}
            constraint = dep_cfg.get("version", "*")
            entry      = resolved_lock.get(dep_id)
            if entry:
                locked_ver = entry.get("version", "unknown")
                try:
                    matches = version_matches(locked_ver, constraint)
                except ValueError as e:
                    warn(f"  {dep_id}: bad constraint '{constraint}': {e}")
                    ok = False
                    continue
                status = "OK  " if matches else "FAIL"
                if not matches:
                    ok = False
                log(f"  {status} {dep_id}: locked v{locked_ver} vs '{constraint}'")
            else:
                log(f"  ??   {dep_id}: not in lock file (run 'resolve' first)")
        if not deps:
            log("No dependencies declared.")
        elif ok:
            log("All constraints satisfied.")
        else:
            die("One or more constraints failed.")
        return

    resolved = resolve_all(deps, root, args.mode, tokens)

    if args.command in ("resolve", "build"):
        # Strip internal _dl_headers before saving to lock
        clean = {
            k: {ck: cv for ck, cv in v.items() if not ck.startswith("_")}
            for k, v in resolved.items()
        }
        save_lock(root, {"resolved": clean})
        log(f"Lock file updated.")

    if args.command == "build":
        output_mode = args.output or manifest.get("output") or "merged"
        build_pack_zip(manifest, resolved, root, output_mode)

    log("Done.")

if __name__ == "__main__":
    main()
