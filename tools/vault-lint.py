#!/usr/bin/env python3
"""Vault lint: checks frontmatter and wikilinks in vault/."""

import re
import sys
from pathlib import Path

VAULT = Path.cwd()

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

def excluded(path: Path) -> bool:
    if path.name in EXCLUDED:
        return True
    parts = path.parts
    return ".obsidian" in parts or "Szablony" in parts

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
    return re.findall(r"\[\[([^\]|#]+)", text)

def resolve_wikilink(link: str, source_file: Path) -> Path | None:
    target = link.strip()
    # Try exact match relative to vault root
    for ext in ("", ".md"):
        candidate = VAULT / (target + ext)
        if candidate.exists():
            return candidate
    # Try relative to source file's directory
    for ext in ("", ".md"):
        candidate = source_file.parent / (target + ext)
        if candidate.exists():
            return candidate
    # Try just filename anywhere in vault
    name = Path(target).name
    for ext in ("", ".md"):
        matches = list(VAULT.rglob(name + ext))
        if matches:
            return matches[0]
    return None

def lint() -> int:
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
