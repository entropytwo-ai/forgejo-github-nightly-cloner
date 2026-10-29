# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Backup verification and checksums.

A backup directory is valid when:

* ``manifest.json`` exists, is valid JSON and has the expected keys;
* every repository with status ``succeeded`` has one ``{type}.json`` per
  captured item type, each a JSON list whose length matches the manifest count;
* every data file's SHA-256 matches ``manifest["checksums"]`` and, if present,
  ``checksums.sha256``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
CHECKSUMS = "checksums.sha256"
REQUIRED_MANIFEST_KEYS = ("backup_date", "completed_at", "summary", "coverage", "repositories", "checksums")
VALID_REPO_STATUSES = {"succeeded", "failed", "inaccessible", "skipped"}


@dataclass
class VerifyResult:
    """Verification outcome."""

    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_files(backup_path: Path) -> List[Path]:
    """All per-repository JSON files (excludes the manifest)."""
    return sorted(p for p in backup_path.rglob("*.json") if p.name != MANIFEST or p.parent != backup_path)


def compute_checksums(backup_path: Union[str, Path]) -> Dict[str, str]:
    """Map of POSIX relative path -> SHA-256 for every data file."""
    root = Path(backup_path)
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in data_files(root)}


def write_checksums_file(backup_path: Union[str, Path], checksums: Dict[str, str]) -> Path:
    """Write ``checksums.sha256`` in ``sha256sum`` format (manifest included)."""
    root = Path(backup_path)
    entries = dict(checksums)
    if (root / MANIFEST).exists():
        entries[MANIFEST] = sha256_file(root / MANIFEST)
    target = root / CHECKSUMS
    target.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items())), encoding="utf-8")
    return target


def read_checksums_file(path: Path) -> Dict[str, str]:
    """Parse a ``sha256sum``-format file."""
    result: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            result[name.strip()] = digest.strip()
    return result


def verify_backup(backup_path: Union[str, Path], write_missing_checksums: bool = True) -> VerifyResult:
    """Verify a backup directory. See module docstring for the rules."""
    root = Path(backup_path)
    errors: List[str] = []
    warnings: List[str] = []

    if not root.is_dir():
        return VerifyResult(False, [f"Backup directory not found: {root}"])
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return VerifyResult(False, [f"{MANIFEST} missing in {root}"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return VerifyResult(False, [f"{MANIFEST} is not valid JSON: {exc}"])
    if not isinstance(manifest, dict):
        return VerifyResult(False, [f"{MANIFEST} must be a JSON object"])

    missing_keys = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
    if missing_keys:
        errors.append(f"{MANIFEST} missing keys: {', '.join(missing_keys)}")
    repositories = manifest.get("repositories") or {}
    if not isinstance(repositories, dict):
        errors.append("manifest 'repositories' must be an object")
        repositories = {}

    summary = manifest.get("summary") or {}
    if isinstance(summary, dict) and summary.get("total") is not None and summary.get("total") != len(repositories):
        errors.append(f"summary.total={summary.get('total')} but manifest lists {len(repositories)} repositories")

    for full_name, info in sorted(repositories.items()):
        status = (info or {}).get("status")
        if status not in VALID_REPO_STATUSES:
            errors.append(f"{full_name}: invalid status {status!r}")
            continue
        if status != "succeeded":
            if status in {"failed", "inaccessible"}:
                warnings.append(f"{full_name}: {status} ({(info or {}).get('error')})")
            continue
        for item_type, count in sorted((info.get("items") or {}).items()):
            if count is None:
                continue  # type was inaccessible (e.g. collaborators 403)
            file_path = root / full_name / f"{item_type}.json"
            if not file_path.is_file():
                errors.append(f"{full_name}: missing {item_type}.json")
                continue
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                errors.append(f"{full_name}/{item_type}.json: invalid JSON ({exc})")
                continue
            if not isinstance(data, list):
                errors.append(f"{full_name}/{item_type}.json: expected a JSON list")
            elif len(data) != count:
                errors.append(f"{full_name}/{item_type}.json: has {len(data)} items, manifest says {count}")

    expected = manifest.get("checksums") or {}
    actual = compute_checksums(root)
    for rel, digest in sorted(expected.items()):
        if rel not in actual:
            errors.append(f"checksum target missing: {rel}")
        elif actual[rel] != digest:
            errors.append(f"checksum mismatch: {rel}")
    for rel in sorted(set(actual) - set(expected)):
        warnings.append(f"file not listed in manifest checksums: {rel}")

    checksum_file = root / CHECKSUMS
    if checksum_file.is_file():
        recorded = read_checksums_file(checksum_file)
        for rel, digest in sorted(recorded.items()):
            target = root / rel
            if not target.is_file():
                errors.append(f"{CHECKSUMS}: missing file {rel}")
            elif sha256_file(target) != digest:
                errors.append(f"{CHECKSUMS}: mismatch for {rel}")
    elif not errors and write_missing_checksums:
        write_checksums_file(root, actual)
        warnings.append(f"{CHECKSUMS} was missing and has been written")

    ok = not errors
    level = logging.INFO if ok else logging.ERROR
    log.log(level, "Verification of %s: %s (%d errors, %d warnings)", root, "OK" if ok else "FAILED", len(errors), len(warnings))
    for err in errors:
        log.error("  %s", err)
    return VerifyResult(ok, errors, warnings)
