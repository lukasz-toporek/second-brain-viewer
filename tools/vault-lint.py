#!/usr/bin/env python3
"""Vault lint: checks frontmatter and wikilinks in the vault.

    python3 vault-lint.py [--vault PATH]
    python3 vault-lint.py [--vault PATH] --groups     # only the link-group report (see report_groups)

The schema comes from the optional "lint" key of the vault's viewer config
(.viewer.json / viewer.json / vault-viewer.json, searched in the vault and up to
4 parent folders). Every sub-key is optional; the defaults below are used otherwise:

    "lint": {
      "valid_types":     ["home", "wiedza", ...],
      "required_fields": {"projekt": ["status"], ...},
      "excluded_files":  ["README.md"],
      "excluded_dirs":   [".obsidian", "Szablony"],
      "require_up":      false,
      "report_groups":   false
    }

Opt-in structure checks (docs/note-linking.md):
  require_up     every note except type: home needs an `up:` value that resolves to a note (missing and broken are
                 reported separately and make the exit code non-zero);
  report_groups  after the checks, how many separate groups the link graph has and which notes are outside the
                 largest one (up to 20 per group). Informational: it never changes the exit code. --groups prints
                 only this report.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# One frontmatter parser for the viewer and the lint: viewer.py sits next to tools/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import viewer  # noqa: E402
from viewer import (extract_wikilinks, fm_str, frontmatter_block,  # noqa: E402
                    parse_frontmatter as _parse_frontmatter, real_file_in)

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


REQUIRE_UP = False
REPORT_GROUPS = False
GROUP_LIST_MAX = 20


def apply_config(vault: Path) -> None:
    global VAULT, VALID_TYPES, REQUIRED_FIELDS, EXCLUDED, EXCLUDED_DIRS, _CI_INDEX, _SHARED, REQUIRE_UP, REPORT_GROUPS
    VAULT = vault.resolve()
    _CI_INDEX = None
    _SHARED = None
    cfg = load_lint_config(VAULT)
    if "valid_types" in cfg:
        VALID_TYPES = set(cfg["valid_types"])
    if "required_fields" in cfg:
        REQUIRED_FIELDS = {k: list(v) for k, v in cfg["required_fields"].items()}
    if "excluded_files" in cfg:
        EXCLUDED = set(cfg["excluded_files"])
    if "excluded_dirs" in cfg:
        EXCLUDED_DIRS = set(cfg["excluded_dirs"])
    REQUIRE_UP = cfg.get("require_up") is True
    REPORT_GROUPS = cfg.get("report_groups") is True


def excluded(path: Path) -> bool:
    if path.name in EXCLUDED:
        return True
    return any(part in EXCLUDED_DIRS for part in path.parts)

def parse_frontmatter(text: str) -> dict | None:
    """The shared parser from viewer.py; None (not {}) when the note has no frontmatter block."""
    if frontmatter_block(text) is None:
        return None
    return _parse_frontmatter(text)


def _present(value) -> bool:
    """A frontmatter value counts as filled in when it is non-blank text or a list with a non-blank item."""
    if isinstance(value, list):
        return any(str(v).strip() for v in value)
    return bool(str(value or "").strip())


# extract_wikilinks() is the viewer's: the same link forms (and no links inside code) for graph, backlinks and lint

_CI_INDEX: dict | None = None
_SHARED = None


def shared_index():
    """The viewer's VaultIndex for this vault: the one place that decides what a link means (graph, backlinks,
    the page and this lint agree: aliases, Markdown links, Unicode names, no links in code)."""
    global _SHARED
    if _SHARED is None:
        viewer.init_config(VAULT, no_plugins=True)
        _SHARED = viewer.get_index()
    return _SHARED


def _ci_index() -> dict:
    """Lower-cased lookup of every file in the vault: path (with/without .md) and bare name."""
    global _CI_INDEX
    if _CI_INDEX is None:
        idx: dict[str, Path] = {}
        for f in sorted(VAULT.rglob("*")):
            if not real_file_in(f, VAULT) or any(part in EXCLUDED_DIRS for part in f.relative_to(VAULT).parts):
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
    """Resolve like Obsidian: case-insensitive, by vault path, bare note name or alias."""
    hit = shared_index().resolve(link)
    if hit:
        return VAULT / hit
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


def up_problem(fm: dict) -> str:
    """"" when the note's `up:` resolves to a note, "missing" when there is no value, "broken" when none resolves."""
    v = fm.get("up")
    items = [str(x).strip() for x in (v if isinstance(v, list) else [v] if v is not None else []) if str(x).strip()]
    if not items:
        return "missing"
    idx = shared_index()
    for item in items:
        if any(idx.resolve(t) for t in (extract_wikilinks(item) or [item])):
            return ""
    return "broken"


def groups_report() -> list[str]:
    """Lines describing the separate groups of the link graph (informational)."""
    shared_index()                                  # points the viewer at this vault first
    groups = viewer.link_groups()
    g = viewer.build_graph()
    lines = [f"Grupy powiązań: {len(groups)} (notatek: {len(g['nodes'])}, linków: {len(g['edges'])})"]
    if groups:
        lines.append(f"  największa grupa: {len(groups[0])} notatek")
    for n, grp in enumerate(groups[1:], 2):
        shown = ", ".join(grp[:GROUP_LIST_MAX]) + (f" … i {len(grp) - GROUP_LIST_MAX} więcej" if len(grp) > GROUP_LIST_MAX else "")
        lines.append(f"  poza największą grupą — grupa {n} ({len(grp)}): {shown}")
    return lines


def lint() -> int:
    if "--vault" in sys.argv:
        apply_config(Path(sys.argv[sys.argv.index("--vault") + 1]))
    else:
        apply_config(VAULT)
    if "--groups" in sys.argv:
        print("\n".join(groups_report()))
        return 0                                    # informational: never a failure
    issues: list[tuple[str, str]] = []

    # notes whose real path is outside the vault (symlinks) are ignored, as in the viewer
    md_files = [f for f in VAULT.rglob("*.md") if not excluded(f) and real_file_in(f, VAULT)]

    for f in sorted(md_files):
        rel = f.relative_to(VAULT)
        text = f.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)

        # Check frontmatter presence
        if fm is None:
            issues.append((str(rel), "brak frontmattera"))
            continue

        # Check type field
        note_type = fm_str(fm, "type").strip()
        if not note_type:
            issues.append((str(rel), "brak pola type: w frontmatterze"))
        elif note_type not in VALID_TYPES:
            issues.append((str(rel), f"nieznany type: '{note_type}' (dozwolone: {', '.join(sorted(VALID_TYPES))})"))

        # Check required fields per type
        for field in REQUIRED_FIELDS.get(note_type, []):
            if not _present(fm.get(field)):
                issues.append((str(rel), f"brak wymaganego pola '{field}' dla type: {note_type}"))

        # Structure: every note except type: home links "up" to a note (opt-in)
        if REQUIRE_UP and note_type != "home":
            problem = up_problem(fm)
            if problem == "missing":
                issues.append((str(rel), "brak pola up: (lint.require_up)"))
            elif problem == "broken":
                issues.append((str(rel), f"zepsute up: {fm_str(fm, 'up')} nie wskazuje żadnej notatki"))

        # Check wikilinks
        for link in extract_wikilinks(text):
            if resolve_wikilink(link, f) is None:
                issues.append((str(rel), f"zepsuty wikilink: [[{link}]]"))

    shown = {str(f.relative_to(VAULT)) for f in md_files}
    for kind, alias, kept, ignored in shared_index().alias_issues:
        if ignored in shown:
            issues.append((ignored, f"alias '{alias}' ignorowany: nazwa pliku {kept} ma pierwszeństwo" if kind == "name"
                           else f"alias '{alias}' jest też w {kept} (tam wygrywa; ten jest ignorowany)"))
    issues.sort(key=lambda x: x[0])

    if not issues:
        print(f"✓ Vault OK — sprawdzono {len(md_files)} plików, brak problemów.")
        if REPORT_GROUPS:
            print("\n".join(groups_report()))
        return 0

    print(f"Vault lint: {len(issues)} problem(ów) w {len(md_files)} plikach\n")
    current_file = None
    for path, msg in issues:
        if path != current_file:
            print(f"\n{path}")
            current_file = path
        print(f"  • {msg}")

    if REPORT_GROUPS:
        print("\n" + "\n".join(groups_report()))
    return 1

if __name__ == "__main__":
    sys.exit(lint())
