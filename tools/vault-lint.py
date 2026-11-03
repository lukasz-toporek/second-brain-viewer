#!/usr/bin/env python3
"""Vault lint: checks frontmatter and wikilinks in the vault.

    python3 vault-lint.py [--vault PATH]

The schema comes from the optional "lint" key of the vault's viewer config
(.viewer.json / viewer.json / vault-viewer.json, searched in the vault and up to
4 parent folders). Every sub-key is optional; the defaults below are used otherwise:

    "lint": {
      "valid_types":     ["home", "wiedza", ...],
      "required_fields": {"projekt": ["status"], ...},
      "excluded_files":  ["README.md"],
      "excluded_dirs":   [".obsidian", "Szablony"]
    }
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VAULT = Path.cwd()

# Defaults (the original hard-coded Polish schema)
VALID_TYPES = {"home", "index", "wiedza", "projekt", "daily", "kurs", "rozmowa", "meta"}

REQUIRED_FIELDS = {
    "projekt": ["status"],
    "daily": ["date"],
    "kurs": ["status"],
    "rozmowa": ["firma", "stanowisko", "data"],
}

EXCLUDED = {
    "README.md",
}

EXCLUDED_DIRS = {".obsidian", "Szablony"}


def load_lint_config(vault: Path) -> dict:
    for d in [vault] + list(vault.parents)[:4]:
        for name in (".viewer.json", "viewer.json", "vault-viewer.json"):
            f = d / name
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8")).get("lint", {}) or {}
    return {}


def apply_config(vault: Path) -> None:
    global VAULT, VALID_TYPES, REQUIRED_FIELDS, EXCLUDED, EXCLUDED_DIRS, _CI_INDEX
    VAULT = vault.resolve()
    _CI_INDEX = None
    cfg = load_lint_config(VAULT)
    if "valid_types" in cfg:
        VALID_TYPES = set(cfg["valid_types"])
    if "required_fields" in cfg:
        REQUIRED_FIELDS = {k: list(v) for k, v in cfg["required_fields"].items()}
    if "excluded_files" in cfg:
        EXCLUDED = set(cfg["excluded_files"])
    if "excluded_dirs" in cfg:
        EXCLUDED_DIRS = set(cfg["excluded_dirs"])


def excluded(path: Path) -> bool:
    if path.name in EXCLUDED:
        return True
    return any(part in EXCLUDED_DIRS for part in path.parts)

def parse_frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm_block = text[4:end]
    result = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result

def extract_wikilinks(text: str) -> list[str]:
    return [m.rstrip("\\").strip() for m in re.findall(r"\[\[([^\]|#\n]+)", text)]

_CI_INDEX: dict | None = None


def _ci_index() -> dict:
    """Lower-cased lookup of every file in the vault: path (with/without .md) and bare name."""
    global _CI_INDEX
    if _CI_INDEX is None:
        idx: dict[str, Path] = {}
        for f in sorted(VAULT.rglob("*")):
            if not f.is_file() or any(part in EXCLUDED_DIRS for part in f.relative_to(VAULT).parts):
                continue
            rel = f.relative_to(VAULT).as_posix().lower()
            idx.setdefault(rel, f)
            if rel.endswith(".md"):
                idx.setdefault(rel[:-3], f)
            idx.setdefault("name:" + f.name.lower(), f)
            if f.name.lower().endswith(".md"):
                idx.setdefault("name:" + f.name.lower()[:-3], f)
        _CI_INDEX = idx
    return _CI_INDEX


def resolve_wikilink(link: str, source_file: Path) -> Path | None:
    """Resolve like Obsidian: case-insensitive, by vault path or bare note name."""
    target = link.strip().replace("\\", "/").lstrip("/")
    # exact matches first (also finds files in excluded dirs, as before)
    for base in (VAULT, source_file.parent):
        for ext in ("", ".md"):
            candidate = base / (target + ext)
            if candidate.exists():
                return candidate
    idx = _ci_index()
    low = target.lower()
    return idx.get(low) or idx.get("name:" + Path(low).name)


def lint() -> int:
    if "--vault" in sys.argv:
        apply_config(Path(sys.argv[sys.argv.index("--vault") + 1]))
    else:
        apply_config(VAULT)
    issues: list[tuple[str, str]] = []

    md_files = [f for f in VAULT.rglob("*.md") if not excluded(f)]

    for f in sorted(md_files):
        rel = f.relative_to(VAULT)
        text = f.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)

        # Check frontmatter presence
        if fm is None:
            issues.append((str(rel), "brak frontmattera"))
            continue

        # Check type field
        note_type = fm.get("type", "").strip()
        if not note_type:
            issues.append((str(rel), "brak pola type: w frontmatterze"))
        elif note_type not in VALID_TYPES:
            issues.append((str(rel), f"nieznany type: '{note_type}' (dozwolone: {', '.join(sorted(VALID_TYPES))})"))

        # Check required fields per type
        for field in REQUIRED_FIELDS.get(note_type, []):
            if not fm.get(field, "").strip():
                issues.append((str(rel), f"brak wymaganego pola '{field}' dla type: {note_type}"))

        # Check wikilinks
        for link in extract_wikilinks(text):
            if resolve_wikilink(link, f) is None:
                issues.append((str(rel), f"zepsuty wikilink: [[{link}]]"))

    if not issues:
        print(f"✓ Vault OK — sprawdzono {len(md_files)} plików, brak problemów.")
        return 0

    print(f"Vault lint: {len(issues)} problem(ów) w {len(md_files)} plikach\n")
    current_file = None
    for path, msg in issues:
        if path != current_file:
            print(f"\n{path}")
            current_file = path
        print(f"  • {msg}")

    return 1

if __name__ == "__main__":
    sys.exit(lint())
