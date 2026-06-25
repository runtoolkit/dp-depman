#!/usr/bin/env python3
"""
dp-resolve.py — Datapack Dependency Resolver
Source: GitHub Releases (prod) or Git Submodule (dev)
Output: Separate ZIPs or single merged ZIP
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

# ─── Constants ────────────────────────────────────────────────────────────────

CONFIG_FILE   = "datapack_depends.json"
LOCK_FILE     = "datapack_depends.lock"
CACHE_DIR     = Path(".dp-cache")
OUTPUT_DIR    = Path("dist")
SUBMODULE_DIR = Path("deps")

# ─── Version comparison ───────────────────────────────────────────────────────

def parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("v")
    parts = re.split(r"[.\-]", v)
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            pass  # ignore pre-release suffixes
    return tuple(result)

def version_matches(version: str, constraint: str) -> bool:
    """
    Supported constraint formats:
      "1.2.3"    → exact match
      ">=1.2.0"  → minimum version
      "1.2.x"    → wildcard (major.minor fixed)
      "^1.2.0"   → major fixed, minor+patch free
      "~1.2.0"   → major+minor fixed, patch free
    """
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
        base = parse_version(constraint[:-2])
        return v[:2] == base[:2]

    if constraint.startswith("^"):
        base = parse_version(constraint[1:])
        return v[0] == base[0] and v >= base

    if constraint.startswith("~"):
        base = parse_version(constraint[1:])
        return v[:2] == base[:2] and v >= base

    raise ValueError(f"Unsupported constraint format: '{constraint}'")

# ─── Config loading ───────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        die(f"{CONFIG_FILE} not found: {path}")
    with open(path) as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            die(f"JSON parse error: {e}")

    for field in ("id", "version", "dependencies"):
        if field not in cfg:
            die(f"Missing required field: '{field}'")

    return cfg

# ─── Lock file ────────────────────────────────────────────────────────────────

def load_lock(root: Path) -> dict:
    lock_path = root / LOCK_FILE
    if lock_path.exists():
        with open(lock_path) as f:
            return json.load(f)
    return {"resolved": {}}

def save_lock(root: Path, lock: dict):
    lock_path = root / LOCK_FILE
    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)
    log(f"Lock file updated: {lock_path}")

# ─── GitHub Releases resolution ───────────────────────────────────────────────

def fetch_github_releases(owner: str, repo: str, token: str | None) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        die(f"GitHub API error ({owner}/{repo}): {e.code} {e.reason}")
    except urllib.error.URLError as e:
        die(f"Network error ({owner}/{repo}): {e.reason}")

def resolve_github(dep_id: str, dep_cfg: dict, token: str | None) -> dict:
    """
    dep_cfg example:
      {
        "source": "github",
        "repo": "runtoolkit/dataLib",
        "version": ">=26.2.0",
        "asset": "dataLib.zip"   ← optional; first .zip is used if omitted
      }
    """
    repo   = dep_cfg["repo"]
    owner, repo_name = repo.split("/", 1)
    constraint   = dep_cfg["version"]
    wanted_asset = dep_cfg.get("asset")

    log(f"Checking GitHub releases: {repo}")
    releases = fetch_github_releases(owner, repo_name, token)

    candidates = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        ver = tag.lstrip("v")
        try:
            if version_matches(ver, constraint):
                candidates.append((ver, rel))
        except ValueError as e:
            warn(f"  {tag}: {e}")

    if not candidates:
        die(f"[{dep_id}] No compatible release found (constraint: {constraint})")

    candidates.sort(key=lambda x: parse_version(x[0]), reverse=True)
    chosen_ver, chosen_rel = candidates[0]
    log(f"  → {repo} v{chosen_ver} selected")

    assets = chosen_rel.get("assets", [])
    if wanted_asset:
        asset = next((a for a in assets if a["name"] == wanted_asset), None)
        if not asset:
            die(f"[{dep_id}] Asset '{wanted_asset}' not found in v{chosen_ver}")
    else:
        asset = next((a for a in assets if a["name"].endswith(".zip")), None)
        if not asset:
            die(f"[{dep_id}] No ZIP asset found in v{chosen_ver}")

    return {
        "source": "github",
        "repo": repo,
        "version": chosen_ver,
        "asset_name": asset["name"],
        "download_url": asset["browser_download_url"],
        "sha256": None,
    }

def download_asset(url: str, dest: Path, token: str | None) -> str:
    """Download file, return SHA-256 hex digest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    log(f"  Downloading: {url}")
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        while chunk := resp.read(65536):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()

# ─── Submodule resolution ─────────────────────────────────────────────────────

def resolve_submodule(dep_id: str, dep_cfg: dict, root: Path) -> dict:
    """
    dep_cfg example:
      {
        "source": "submodule",
        "path": "deps/dataLib",
        "url": "https://github.com/runtoolkit/dataLib.git",
        "version": ">=26.2.0"
      }
    """
    rel_path = dep_cfg.get("path", str(SUBMODULE_DIR / dep_id))
    abs_path = root / rel_path

    if not abs_path.exists():
        die(
            f"[{dep_id}] Submodule directory not found: {rel_path}\n"
            f"  Run: git submodule add <url> {rel_path}"
        )

    # Try to detect version from datapack_depends.json first, then pack.mcmeta
    detected_ver = "unknown"
    dep_config_path = abs_path / CONFIG_FILE
    if dep_config_path.exists():
        try:
            with open(dep_config_path) as f:
                dep_meta = json.load(f)
            detected_ver = dep_meta.get("version", detected_ver)
        except Exception:
            pass

    if detected_ver == "unknown":
        mcmeta_path = abs_path / "pack.mcmeta"
        if mcmeta_path.exists():
            try:
                with open(mcmeta_path) as f:
                    meta = json.load(f)
                detected_ver = meta.get("pack", {}).get("version", "unknown")
            except Exception:
                pass

    constraint = dep_cfg.get("version", "*")
    if constraint != "*" and detected_ver != "unknown":
        try:
            if not version_matches(detected_ver, constraint):
                die(
                    f"[{dep_id}] Version mismatch: "
                    f"submodule v{detected_ver} does not satisfy '{constraint}'\n"
                    f"  Update submodule: git submodule update --remote {rel_path}"
                )
        except ValueError as e:
            warn(f"[{dep_id}] Constraint check skipped: {e}")

    log(f"  → {dep_id} submodule v{detected_ver} ({rel_path})")

    return {
        "source": "submodule",
        "path": rel_path,
        "version": detected_ver,
    }

# ─── Dependency resolution ────────────────────────────────────────────────────

def resolve_all(cfg: dict, root: Path, mode: str, token: str | None) -> dict:
    """
    mode: "prod" → GitHub releases
          "dev"  → submodule
          "auto" → determined by each dep's source field
    """
    lock = load_lock(root)
    deps = cfg.get("dependencies", {})
    if not deps:
        log("No dependencies declared.")
        return {}

    results = {}
    for dep_id, dep_cfg in deps.items():
        if isinstance(dep_cfg, str):
            dep_cfg = {"source": "auto", "version": dep_cfg}

        source = dep_cfg.get("source", "auto")
        effective_mode = mode
        if effective_mode == "auto":
            effective_mode = source if source in ("github", "submodule") else "prod"

        if effective_mode == "prod" or source == "github":
            if "repo" not in dep_cfg:
                die(f"[{dep_id}] Missing 'repo' field (required for prod mode)")
            info = resolve_github(dep_id, dep_cfg, token)
        else:
            info = resolve_submodule(dep_id, dep_cfg, root)

        results[dep_id] = info

    return results

# ─── ZIP operations ───────────────────────────────────────────────────────────

def get_dep_zip(dep_id: str, info: dict, root: Path, token: str | None) -> Path:
    """Return the dependency ZIP path (from cache or downloaded)."""
    if info["source"] == "submodule":
        src = root / info["path"]
        cache_zip = CACHE_DIR / f"{dep_id}-{info['version']}-submodule.zip"
        cache_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(cache_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in src.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(src))
        return cache_zip
    else:
        cache_zip = CACHE_DIR / f"{dep_id}-{info['version']}-{info['asset_name']}"
        cache_zip.parent.mkdir(parents=True, exist_ok=True)
        if not cache_zip.exists():
            sha = download_asset(info["download_url"], cache_zip, token)
            info["sha256"] = sha
            log(f"  SHA-256: {sha}")
        else:
            log(f"  Using cached: {cache_zip.name}")
        return cache_zip

def build_separate_zips(cfg: dict, resolved: dict, root: Path, token: str | None):
    """Produce one ZIP per dependency plus the main pack ZIP."""
    out = root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    # Bug 3 fix: use clean dep_id-version.zip name, not the cache filename
    for dep_id, info in resolved.items():
        dep_zip = get_dep_zip(dep_id, info, root, token)
        dest = out / f"{dep_id}-{info['version']}.zip"
        shutil.copy2(dep_zip, dest)
        log(f"  Copied: {dest.name}")

    # Bug 1+2 fix: zip from pack_root so pack.mcmeta is at ZIP root,
    # and only include datapack files (pack.mcmeta + data/)
    pack_root = _get_pack_root(cfg, root)
    pack_zip_name = f"{cfg['id']}-{cfg['version']}.zip"
    pack_zip_path = out / pack_zip_name
    with zipfile.ZipFile(pack_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in pack_root.rglob("*"):
            if file.is_file() and not _should_exclude(file, pack_root):
                zf.write(file, file.relative_to(pack_root))
    log(f"Main pack: {pack_zip_path.name}")

def build_merged_zip(cfg: dict, resolved: dict, root: Path, token: str | None):
    """Merge all packs into a single ZIP (namespace collision risk — warned)."""
    out = root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    merged_name = f"{cfg['id']}-{cfg['version']}-merged.zip"
    merged_path = out / merged_name

    seen_files: dict[str, str] = {}
    conflicts = []

    with zipfile.ZipFile(merged_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Dependencies first
        for dep_id, info in resolved.items():
            dep_zip_path = get_dep_zip(dep_id, info, root, token)
            with zipfile.ZipFile(dep_zip_path) as dep_zip:
                for name in dep_zip.namelist():
                    if name in seen_files:
                        conflicts.append(
                            f"  CONFLICT: '{name}' ({seen_files[name]} ↔ {dep_id})"
                        )
                    else:
                        seen_files[name] = dep_id
                    zf.writestr(name, dep_zip.read(name))

        # Main pack overrides dependencies — zip from pack_root (Bug 1+2 fix)
        pack_root = _get_pack_root(cfg, root)
        for file in pack_root.rglob("*"):
            if file.is_file() and not _should_exclude(file, pack_root):
                arc_name = str(file.relative_to(pack_root))
                seen_files[arc_name] = f"{cfg['id']} (override)"
                zf.write(file, arc_name)

    if conflicts:
        warn("Namespace conflicts detected — main pack overrides:")
        for c in conflicts:
            warn(c)

    log(f"Merged ZIP: {merged_path.name}")

def _get_pack_root(cfg: dict, root: Path) -> Path:
    """
    Return the directory that contains pack.mcmeta.
    Checks config 'pack_root' field first, then searches for pack.mcmeta,
    then falls back to root itself.
    """
    # Explicit config override
    if "pack_root" in cfg:
        return root / cfg["pack_root"]

    # Search for pack.mcmeta one level deep
    for candidate in [root] + list(root.iterdir()):
        if candidate.is_dir() and (candidate / "pack.mcmeta").exists():
            if candidate != root:
                log(f"Pack root detected: {candidate.relative_to(root)}/")
            return candidate

    return root

def _should_exclude(file: Path, root: Path) -> bool:
    rel   = file.relative_to(root)
    parts = rel.parts
    # Only include pack.mcmeta and data/ — everything else is repo metadata
    allowed_roots = {"data", "pack.mcmeta"}
    if parts[0] not in allowed_roots:
        return True
    excluded = {".git", ".github", ".dp-cache", "dist", "__pycache__"}
    return parts[0] in excluded or parts[0].startswith(".")

# ─── Submodule init helper ────────────────────────────────────────────────────

def cmd_init_submodules(cfg: dict, root: Path):
    """Add submodule dependencies via git submodule add."""
    deps = cfg.get("dependencies", {})
    for dep_id, dep_cfg in deps.items():
        if isinstance(dep_cfg, str):
            continue
        if dep_cfg.get("source") != "submodule":
            continue
        url = dep_cfg.get("url")
        if not url:
            warn(f"[{dep_id}] Missing 'url' field — cannot add submodule")
            continue
        path = dep_cfg.get("path", str(SUBMODULE_DIR / dep_id))
        abs_path = root / path
        if abs_path.exists():
            log(f"[{dep_id}] Already present: {path}")
            continue
        log(f"[{dep_id}] git submodule add {url} {path}")
        result = subprocess.run(
            ["git", "submodule", "add", url, path],
            cwd=root, capture_output=True, text=True
        )
        if result.returncode != 0:
            die(f"git submodule add failed:\n{result.stderr}")

# ─── Utilities ────────────────────────────────────────────────────────────────

def log(msg: str):  print(f"[dp-resolve] {msg}")
def warn(msg: str): print(f"[dp-resolve] WARNING: {msg}", file=sys.stderr)
def die(msg: str):  print(f"[dp-resolve] ERROR: {msg}", file=sys.stderr); sys.exit(1)

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Datapack dependency manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  resolve   Resolve dependencies and write lock file
  build     Produce ZIP output(s)
  init      Add submodule dependencies via git submodule add
  check     Verify version constraints only (no ZIP output)

Examples:
  dp-resolve resolve --mode prod
  dp-resolve build --output separate
  dp-resolve build --output merged
  dp-resolve build --mode dev --output both
        """,
    )
    parser.add_argument("command", choices=["resolve", "build", "init", "check"])
    parser.add_argument(
        "--mode", choices=["prod", "dev", "auto"], default="auto",
        help="prod=GitHub releases  dev=submodule  auto=per-dep source field"
    )
    parser.add_argument(
        "--output", choices=["separate", "merged", "both"], default="separate",
        help="ZIP output mode (build command only)"
    )
    parser.add_argument(
        "--config", default=CONFIG_FILE,
        help=f"Config file path (default: {CONFIG_FILE})"
    )
    parser.add_argument(
        "--root", default=".",
        help="Project root directory (default: .)"
    )
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (default: $GITHUB_TOKEN env var)"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    cfg  = load_config(root / args.config)

    log(f"Project: {cfg['id']} v{cfg['version']}")

    if args.command == "init":
        cmd_init_submodules(cfg, root)
        return

    resolved = resolve_all(cfg, root, args.mode, args.token)

    if args.command == "check":
        if resolved:
            log("Version check passed:")
            for dep_id, info in resolved.items():
                log(f"  {dep_id}: {info['version']} ({info['source']})")
        else:
            log("No dependencies declared.")
        return

    if args.command in ("resolve", "build"):
        lock = {"resolved": resolved}
        save_lock(root, lock)

    if args.command == "build":
        log(f"Output mode: {args.output}")
        if args.output in ("separate", "both"):
            build_separate_zips(cfg, resolved, root, args.token)
        if args.output in ("merged", "both"):
            build_merged_zip(cfg, resolved, root, args.token)

    log("Done.")

if __name__ == "__main__":
    main()
