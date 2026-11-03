#!/usr/bin/env python3
"""Second Brain Viewer.

Usage:
    python3 viewer.py                        # write _index.md in current dir
    python3 viewer.py --dry-run              # print _index.md to stdout
    python3 viewer.py --serve                # local viewer at http://localhost:5001
    python3 viewer.py --vault PATH --serve   # point at a specific vault
    python3 viewer.py --serve --port 8080
    python3 viewer.py --init                 # scaffold .viewer.json + .gitignore
    python3 viewer.py --vault PATH --init    # same for a specific vault
    python3 viewer.py --serve --offline      # vendored libs, no chat, strict CSP
    python3 viewer.py --export OUT.html      # one self-contained file (contains all notes!)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import date
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

# Populated by init_config() at startup
VAULT: Path = Path.cwd()
OUTPUT: Path = VAULT / "_index.md"
SKIP_NAMES: set = {"README.md", "_index.md"}
SKIP_DIRS: set = {".obsidian"}
FOLDER_ORDER: list = []
CONFIG: dict = {}
OFFLINE: bool = False


def load_config(vault_path: Path) -> dict:
    defaults: dict = {
        "title": vault_path.name,
        "skip_dirs": [".obsidian", ".git"],
        "skip_files": ["README.md", "_index.md"],
        "folder_order": [],
        "folders": {},
        "types": {"daily": "#5b9bd5", "meta": "#888", "index": "#888"},
        "system_prompt": "You are a knowledge base assistant. Answer concisely.",
    }
    for d in [vault_path] + list(vault_path.parents)[:4]:
        for cfg_name in (".viewer.json", "viewer.json", "vault-viewer.json"):
            cfg_path = d / cfg_name
            if cfg_path.exists():
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                defaults.update(loaded)
                return defaults
    return defaults


def init_config(vault_path: Path) -> None:
    global VAULT, OUTPUT, SKIP_NAMES, SKIP_DIRS, FOLDER_ORDER, CONFIG
    VAULT = vault_path.resolve()
    CONFIG = load_config(VAULT)
    OUTPUT = VAULT / "_index.md"
    SKIP_NAMES = set(CONFIG.get("skip_files", ["README.md", "_index.md"]))
    SKIP_DIRS = set(CONFIG.get("skip_dirs", [".obsidian"]))
    FOLDER_ORDER = CONFIG.get("folder_order", [])

# ── Init / scaffold ─────────────────────────────────────────────────────────

KNOWN_TYPE_COLORS: dict[str, str] = {
    "home":    "#e07050",
    "index":   "#888888",
    "meta":    "#888888",
    "daily":   "#5b9bd5",
    "wiedza":  "#4ecdc4",
    "projekt": "#e07050",
    "kurs":    "#c8980a",
    "rozmowa": "#9b8bdb",
}

_FOLDER_PALETTE = [
    {"bg": "#1e1e1e", "fg": "#777777"},
    {"bg": "#102030", "fg": "#5b9bd5"},
    {"bg": "#0a2020", "fg": "#4ecdc4"},
    {"bg": "#201008", "fg": "#e07050"},
    {"bg": "#1c1608", "fg": "#c8980a"},
    {"bg": "#1a1a2a", "fg": "#9b8bdb"},
    {"bg": "#1a2a1a", "fg": "#68b068"},
    {"bg": "#181818", "fg": "#555555"},
]

_EXTRA_TYPE_COLORS = ["#5b9bd5", "#4ecdc4", "#c8980a", "#9b8bdb", "#68b068", "#e07050"]

_INIT_SKIP_DIRS = {".obsidian", ".git", ".claude", "schemas", "decisions", "exports", "__pycache__"}


def _parse_yaml_types(schema_path: Path) -> list[str]:
    """Extract type names from note-types.yaml without external dependencies."""
    types: list[str] = []
    in_types = False
    for line in schema_path.read_text(encoding="utf-8").splitlines():
        stripped = line.rstrip()
        if stripped == "types:":
            in_types = True
            continue
        if in_types:
            if stripped and not stripped[0].isspace():
                break
            m = re.match(r"^  ([a-zA-Z_][a-zA-Z0-9_]*):\s*$", stripped)
            if m:
                types.append(m.group(1))
    return types


def generate_viewer_config(vault_path: Path) -> dict:
    """Build a .viewer.json dict for vault_path, using schema when available."""
    vault = vault_path.resolve()
    folders = sorted(
        d.name for d in vault.iterdir()
        if d.is_dir() and d.name not in _INIT_SKIP_DIRS and not d.name.startswith(".")
    )

    folder_cfg: dict[str, dict] = {}
    for i, name in enumerate(folders):
        pal = _FOLDER_PALETTE[i % len(_FOLDER_PALETTE)]
        letter = next((c.upper() for c in name if c.isalpha()), name[0].upper())
        folder_cfg[name] = {"bg": pal["bg"], "fg": pal["fg"], "letter": letter}
    folder_cfg["(vault root)"] = {"bg": "#201008", "fg": "#e07050", "letter": "~"}

    schema = vault / "schemas" / "notes" / "note-types.yaml"
    schema_types = _parse_yaml_types(schema) if schema.exists() else list(KNOWN_TYPE_COLORS)

    type_cfg: dict[str, str] = {}
    extra_idx = 0
    for t in schema_types:
        if t in KNOWN_TYPE_COLORS:
            type_cfg[t] = KNOWN_TYPE_COLORS[t]
        else:
            type_cfg[t] = _EXTRA_TYPE_COLORS[extra_idx % len(_EXTRA_TYPE_COLORS)]
            extra_idx += 1

    return {
        "title": vault.name,
        "skip_dirs": [".obsidian", ".git"],
        "skip_files": ["README.md", "_index.md"],
        "folder_order": folders,
        "folders": folder_cfg,
        "types": type_cfg,
        "system_prompt": "You are a knowledge base assistant. Answer concisely.",
    }


def init_vault(vault_path: Path) -> None:
    """Write .viewer.json and, if missing, .gitignore to vault_path."""
    vault = vault_path.resolve()
    cfg_path = vault / ".viewer.json"
    cfg = generate_viewer_config(vault)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"✓ Wrote {cfg_path}")

    gitignore = vault / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".viewer.json\n", encoding="utf-8")
        print(f"✓ Created {gitignore}")
    else:
        print(f"  .gitignore already exists — not modified")


# ── Parsing ────────────────────────────────────────────────────────────────

def skip(path: Path) -> bool:
    if path.name in SKIP_NAMES:
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    result = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def extract_title(text: str, fm: dict, path: Path) -> str:
    if fm.get("title"):
        return fm["title"]
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def extract_summary(text: str) -> str:
    in_fm = False
    fm_done = False
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_fm = True
            continue
        if in_fm:
            if stripped == "---":
                fm_done = True
                in_fm = False
            continue
        if not fm_done and i < 3:
            continue
        if stripped.startswith("#") or not stripped:
            continue
        if stripped in ("-", "*", "- [ ]", "- [x]"):
            continue
        if stripped.startswith("- [[") or stripped.startswith("* [["):
            continue
        return stripped[:120] + ("…" if len(stripped) > 120 else "")
    return ""


def folder_key(name: str) -> tuple:
    try:
        return (FOLDER_ORDER.index(name), name)
    except ValueError:
        return (len(FOLDER_ORDER), name)


def scan_vault() -> dict:
    by_folder: dict[str, list[dict]] = {}
    for path in sorted(VAULT.rglob("*.md")):
        if skip(path):
            continue
        rel = path.relative_to(VAULT)
        folder = rel.parts[0] if len(rel.parts) > 1 else "(vault root)"
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        by_folder.setdefault(folder, []).append({
            "name":    path.stem,
            "path":    rel.as_posix(),
            "type":    fm.get("type", ""),
            "status":  fm.get("status", ""),
            "date":    fm.get("date", ""),
            "summary": extract_summary(text),
        })
    folders = [
        {"folder": f, "notes": by_folder[f]}
        for f in sorted(by_folder, key=folder_key)
    ]
    total = sum(len(f["notes"]) for f in folders)
    return {"generated": str(date.today()), "notes": total, "folders": folders}


# ── Wikilink graph ──────────────────────────────────────────────────────────

_WIKILINK_RE = re.compile(r'!?\[\[([^\]\n]+?)\]\]')


def extract_wikilinks(text: str) -> list[str]:
    """Targets of [[target#heading|label]] (also ![[embeds]] and [[a\\|b]] inside tables)."""
    out = []
    for m in _WIKILINK_RE.finditer(text):
        target = re.split(r'\\?\||#', m.group(1), maxsplit=1)[0].strip()
        if target:
            out.append(target)
    return out


class VaultIndex:
    """All notes plus link resolution shared by the graph, backlinks and the export.

    A link target resolves (case-insensitively) to a vault-relative path with or without
    ``.md``, or to a bare note name. The browser's ``resolveNote()`` mirrors this logic.
    """

    def __init__(self) -> None:
        self.notes: list[dict] = []          # {path, name, type, folder, text}
        self.by_path: dict[str, str] = {}
        self.by_stem: dict[str, str] = {}
        for path in sorted(VAULT.rglob("*.md")):
            if skip(path):
                continue
            rel = path.relative_to(VAULT).as_posix()
            text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            self.notes.append({
                "path": rel, "name": path.stem, "type": fm.get("type", ""),
                "folder": rel.split("/")[0] if "/" in rel else "(vault root)", "text": text,
            })
            low = rel.lower()
            self.by_path[low] = rel
            self.by_path[low[:-3]] = rel
            self.by_stem.setdefault(path.stem.lower(), rel)
        self.out_links: dict[str, set] = {}
        self.backlinks: dict[str, list] = {n["path"]: [] for n in self.notes}
        for n in self.notes:
            targets = set()
            for link in extract_wikilinks(n["text"]):      # body and frontmatter alike
                t = self.resolve(link)
                if t and t != n["path"]:
                    targets.add(t)
            self.out_links[n["path"]] = targets
            for t in targets:
                self.backlinks[t].append(n)

    def resolve(self, target: str):
        t = target.strip().replace("\\", "/").lstrip("/").lower()
        if t.endswith(".md"):
            t = t[:-3]
        return self.by_path.get(t) or self.by_stem.get(t)

    def backlinks_for(self, rel: str) -> list[dict]:
        return sorted(({"path": n["path"], "name": n["name"]} for n in self.backlinks.get(rel, [])),
                      key=lambda d: (d["name"].lower(), d["path"]))


_INDEX_CACHE: tuple = (None, None)


def get_index() -> VaultIndex:
    """VaultIndex, rebuilt only when a note's mtime/set of notes changed."""
    global _INDEX_CACHE
    snap = vault_snapshot()
    if _INDEX_CACHE[0] != snap or _INDEX_CACHE[1] is None:
        _INDEX_CACHE = (snap, VaultIndex())
    return _INDEX_CACHE[1]


def build_graph() -> dict:
    idx = get_index()
    nodes = [{"id": n["path"], "name": n["name"], "type": n["type"], "folder": n["folder"]}
             for n in idx.notes]
    edges: list[dict] = []
    seen: set[tuple] = set()
    for n in idx.notes:
        for target in sorted(idx.out_links[n["path"]]):
            key = tuple(sorted([n["path"], target]))
            if key not in seen:
                edges.append({"source": n["path"], "target": target})
                seen.add(key)
    return {"nodes": nodes, "edges": edges}


# ── File watcher ────────────────────────────────────────────────────────────

def vault_snapshot() -> dict:
    """Return {rel_path: mtime} for all tracked .md files."""
    snap = {}
    for p in VAULT.rglob("*.md"):
        if not skip(p):
            snap[p.relative_to(VAULT).as_posix()] = p.stat().st_mtime
    return snap


# ── Markdown output ─────────────────────────────────────────────────────────

def build_index() -> str:
    data = scan_vault()
    lines = [
        "---", f"generated: {data['generated']}", f"notes: {data['notes']}", "---",
        "# Vault — index", "", "Generated by `viewer.py`. Do not edit manually.", "",
    ]
    for sec in data["folders"]:
        lines.append(f"## {sec['folder']} ({len(sec['notes'])})")
        lines.append("")
        for n in sec["notes"]:
            meta_parts = []
            if n["type"] and n["type"] != "daily":
                meta_parts.append(n["type"])
            if n["status"]:
                meta_parts.append(n["status"])
            if n["date"]:
                meta_parts.append(n["date"])
            meta = f" `{', '.join(meta_parts)}`" if meta_parts else ""
            summary = f" — {n['summary']}" if n["summary"] else ""
            lines.append(f"- [[{n['name']}]]{meta}{summary}")
        lines.append("")
    return "\n".join(lines)


# ── Viewer HTML ──────────────────────────────────────────────────────────────

VIEWER_HTML = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vault</title>
__HEAD__
<style>
:root {
  --bg:      #131313;
  --sb:      #191919;
  --sh:      #1e1e1e;
  --sa:      #252525;
  --bd:      #272727;
  --tx:      #d0d0d0;
  --tx2:     #6a6a6a;
  --ac:      #e07050;
  --ac2:     #2a1508;

  --cp: #e07050; --cw: #4ecdc4; --cd: #5b9bd5;
  --cm: #888;    --ci: #888;    --ch: #e07050;
}

*,*::before,*::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body { height: 100%; overflow: hidden; }

body {
  display: flex;
  background: var(--bg);
  color: var(--tx);
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* ── Sidebar ── */
.sb {
  width: 240px;
  flex-shrink: 0;
  background: var(--sb);
  border-right: 1px solid var(--bd);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sb-top {
  padding: 13px 14px 10px;
  border-bottom: 1px solid var(--bd);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.sb-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--tx2);
}

.rbtn {
  display: flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  color: var(--tx2);
  cursor: pointer;
  font-family: inherit;
  font-size: 11px;
  padding: 3px 6px;
  border-radius: 4px;
  transition: color .1s, background .1s;
}
.rbtn:hover { color: var(--tx); background: var(--sh); }
.rbtn.spinning svg { animation: spin .5s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  background: none;
  border: none;
  color: var(--tx2);
  cursor: pointer;
  border-radius: 4px;
  transition: color .1s, background .1s;
}
.icon-btn:hover { color: var(--tx); background: var(--sh); }
.icon-btn.active { color: var(--ac); background: var(--ac2); }

/* graph overlay — sits over content, below tabs */
#graph-wrap {
  position: absolute;
  top: 36px;
  left: 0; right: 0; bottom: 0;
  background: #0d0d0d;
  overflow: hidden;
  z-index: 5;
}
#graph-wrap[hidden] { display: none; }

.graph-hint {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 11px;
  color: rgba(255,255,255,.25);
  pointer-events: none;
  white-space: nowrap;
}

.graph-tooltip {
  position: absolute;
  background: #1e1e1e;
  border: 1px solid #333;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 11px;
  color: var(--tx);
  pointer-events: none;
  white-space: nowrap;
  display: none;
  z-index: 10;
}
.graph-tooltip .gt-type { color: var(--tx2); margin-top: 2px; }

.sb-srch {
  padding: 8px 10px;
  border-bottom: 1px solid var(--bd);
  flex-shrink: 0;
}
.srch {
  width: 100%;
  padding: 5px 9px;
  background: var(--bg);
  border: 1px solid var(--bd);
  border-radius: 5px;
  color: var(--tx);
  font-family: inherit;
  font-size: 12px;
  outline: none;
  transition: border-color .1s;
}
.srch:focus { border-color: var(--ac); }
.srch::placeholder { color: var(--tx2); }

.tree {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0 24px;
}

/* ── Folder ── */
.folder { }

.fhd {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px 5px 8px;
  margin: 0 4px;
  border-radius: 5px;
  cursor: pointer;
  user-select: none;
  transition: background .08s;
}
.fhd:hover { background: var(--sh); }

.ch {
  width: 12px;
  height: 12px;
  color: var(--tx2);
  flex-shrink: 0;
  transition: transform .15s;
}
.fhd.open .ch { transform: rotate(90deg); }

.fi {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0;
}

.fn {
  font-size: 13px;
  font-weight: 500;
  color: var(--tx);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fc {
  font-size: 11px;
  color: var(--tx2);
  font-variant-numeric: tabular-nums;
}

.fnotes { padding: 0 4px 2px 22px; }
.fnotes[hidden] { display: none; }

/* ── Note row in sidebar ── */
.ni {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background .07s;
}
.ni:hover { background: var(--sh); }
.ni.active { background: var(--sa); }

.nd {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

.nn {
  font-size: 12.5px;
  color: var(--tx2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  transition: color .07s;
}
.ni:hover .nn, .ni.active .nn { color: var(--tx); }

/* ── Main ── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
  position: relative;
}

/* ── Tabs ── */
.tabrow {
  display: flex;
  align-items: stretch;
  background: var(--sb);
  border-bottom: 1px solid var(--bd);
  min-height: 36px;
  flex-shrink: 0;
}
.tabs {
  display: flex;
  flex: 1;
  overflow-x: auto;
  min-width: 0;
}
.tabs::-webkit-scrollbar { height: 0; }

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 16px;
  height: 36px;
  font-size: 12px;
  color: var(--tx2);
  cursor: pointer;
  border-right: 1px solid var(--bd);
  white-space: nowrap;
  flex-shrink: 0;
  transition: background .08s, color .08s;
  position: relative;
}
.tab:hover { background: var(--sh); color: var(--tx); }
.tab.active {
  background: var(--bg);
  color: var(--tx);
}
.tab.active::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1.5px;
  background: var(--ac);
}

.ti {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

/* ── Content ── */
.content {
  flex: 1;
  overflow-y: auto;
}

.welcome {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--tx2);
  text-align: center;
  padding: 40px;
}
.welcome-icon {
  font-size: 28px;
  opacity: .2;
  margin-bottom: 4px;
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.welcome h3 { font-size: 14px; font-weight: 500; color: var(--tx2); }
.welcome p { font-size: 12px; color: var(--tx2); opacity: .7; }

.note-body {
  padding: 40px 56px 80px;
  max-width: 780px;
}

/* breadcrumb */
.bc {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 20px;
  font-size: 11px;
  color: var(--tx2);
}
.bc .sep { opacity: .35; }

/* fm chips */
.chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 24px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--bd);
}
.chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}

/* markdown */
.md { color: var(--tx); }
.md h1 { font-size: 22px; font-weight: 600; letter-spacing: -.02em; margin-bottom: 18px; line-height: 1.3; }
.md h2 { font-size: 16px; font-weight: 600; margin: 28px 0 10px; color: var(--tx); }
.md h3 { font-size: 14px; font-weight: 600; margin: 20px 0 8px; color: var(--tx); }
.md p  { margin-bottom: 12px; line-height: 1.7; }
.md ul, .md ol { padding-left: 20px; margin-bottom: 12px; }
.md li { margin-bottom: 4px; line-height: 1.65; }
.md a  { color: var(--ac); text-decoration: none; }
.md a:hover { text-decoration: underline; }
.md blockquote {
  border-left: 2px solid var(--bd);
  padding: 4px 0 4px 16px;
  margin: 0 0 12px;
  color: var(--tx2);
}
.md code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  background: var(--sh);
  padding: 2px 5px;
  border-radius: 3px;
  color: #b8a8d8;
}
.md pre {
  background: #111;
  border: 1px solid var(--bd);
  border-radius: 6px;
  padding: 16px;
  overflow-x: auto;
  margin-bottom: 16px;
}
.md pre code { background: none; padding: 0; color: #c0cce0; }
.md hr { border: none; border-top: 1px solid var(--bd); margin: 24px 0; }
.md table { width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 12.5px; overflow-x: auto; display: block; }
.md th { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--bd); color: var(--tx2); font-weight: 500; white-space: nowrap; }
.md td { padding: 7px 12px; border-bottom: 1px solid var(--bd); }
.md strong { font-weight: 600; color: var(--tx); }

/* wikilinks */
.md a.wl { color: var(--ac); border-bottom: 1px dotted rgba(224,112,80,.5); }
.md a.wl:hover { text-decoration: none; border-bottom-style: solid; }
.md .wl.missing { color: var(--tx2); border-bottom: 1px dashed var(--tx2); cursor: help; }

/* callouts */
.callout {
  --co: #5b9bd5;
  border-left: 3px solid var(--co);
  background: var(--sh);
  background: color-mix(in srgb, var(--co) 10%, transparent);
  border-radius: 0 6px 6px 0;
  padding: 8px 14px;
  margin: 0 0 14px;
}
.callout-title { font-weight: 600; color: var(--co); margin-bottom: 2px; }
.callout-body { color: var(--tx); }
.callout-body > :last-child { margin-bottom: 0; }
.c-info, .c-note, .c-todo { --co: #5b9bd5; }
.c-tip, .c-hint, .c-success, .c-check, .c-done { --co: #4ecdc4; }
.c-abstract, .c-summary, .c-tldr { --co: #68b068; }
.c-warning, .c-caution, .c-attention { --co: #c8980a; }
.c-danger, .c-error, .c-bug, .c-failure, .c-fail, .c-missing { --co: #d05050; }
.c-question, .c-help, .c-faq { --co: #9b8bdb; }
.c-example { --co: #b07ad0; }
.c-quote, .c-cite { --co: #888; }

/* dataview inline fields */
.dv {
  display: inline-block; font-size: 11px; line-height: 1.5; padding: 0 7px; margin: 0 2px;
  border-radius: 9px; background: var(--sa); color: var(--tx2); border: 1px solid var(--bd);
  white-space: nowrap; vertical-align: baseline; text-decoration: none;
}
.dv .k { opacity: .65; margin-right: 4px; }
a.dv-link { color: var(--ac); }
a.dv-link:hover { text-decoration: none; border-color: var(--ac); }
.dv-state { font-weight: 500; }
.s-not-started { color: #9a9a9a; }
.s-in-progress { color: #5b9bd5; background: rgba(91,155,213,.14); border-color: rgba(91,155,213,.35); }
.s-to-verify   { color: #c8980a; background: rgba(200,152,10,.14); border-color: rgba(200,152,10,.35); }
.s-done        { color: #68b068; background: rgba(104,176,104,.14); border-color: rgba(104,176,104,.35); }
.s-waived      { color: #9b8bdb; background: rgba(155,139,219,.14); border-color: rgba(155,139,219,.35); }
.s-missed      { color: #d05050; background: rgba(208,80,80,.14); border-color: rgba(208,80,80,.35); }
.md li.overdue { border-left: 2px solid #d05050; padding-left: 8px; margin-left: -10px; }
.md li.overdue .dv-due { color: #d05050; background: rgba(208,80,80,.14); border-color: rgba(208,80,80,.35); }

/* backlinks */
.backlinks { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--bd); }
.backlinks h4 { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--tx2); margin-bottom: 8px; }
.backlinks ul { list-style: none; padding: 0; }
.backlinks li { margin-bottom: 3px; }
.backlinks a.wl { color: var(--ac); text-decoration: none; }

.loading { padding: 40px 56px; color: var(--tx2); font-size: 13px; }
.err     { padding: 40px 56px; color: #c05050; font-size: 13px; }

/* scrollbar */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--bd); border-radius: 2px; }

/* ── Chat panel ── */
.chat-panel {
  width: 320px;
  min-width: 280px;
  max-width: 480px;
  display: flex;
  flex-direction: column;
  background: var(--sb);
  border-left: 1px solid var(--bd);
  flex-shrink: 0;
  position: relative;
  overflow: hidden;
}
.chat-panel[hidden] { display: none; }

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  height: 36px;
  border-bottom: 1px solid var(--bd);
  flex-shrink: 0;
}
.chat-header-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--tx2);
  text-transform: uppercase;
  letter-spacing: .06em;
}
.chat-close {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--tx2);
  padding: 3px 5px;
  border-radius: 4px;
  line-height: 1;
  font-size: 16px;
}
.chat-close:hover { background: var(--sh); color: var(--tx); }

.chat-ctx {
  padding: 8px 12px;
  font-size: 10.5px;
  color: var(--tx2);
  border-bottom: 1px solid var(--bd);
  background: var(--sh);
  flex-shrink: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.chat-ctx span { color: var(--ac); }

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chat-bubble {
  max-width: 100%;
  font-size: 12.5px;
  line-height: 1.6;
  padding: 8px 12px;
  border-radius: 8px;
  word-break: break-word;
}
.chat-bubble.user {
  background: var(--ac2);
  color: var(--tx);
  align-self: flex-end;
  border-bottom-right-radius: 2px;
}
.chat-bubble.assistant {
  background: var(--sh);
  color: var(--tx);
  align-self: flex-start;
  border-bottom-left-radius: 2px;
}
.chat-bubble.thinking {
  background: var(--sh);
  color: var(--tx2);
  align-self: flex-start;
  font-style: italic;
}

.chat-empty {
  color: var(--tx2);
  font-size: 12px;
  text-align: center;
  padding: 40px 20px;
  line-height: 1.7;
}

.chat-footer {
  flex-shrink: 0;
  border-top: 1px solid var(--bd);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chat-model {
  font-size: 10px;
  color: var(--tx2);
  padding: 0 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.chat-model-dot { width: 5px; height: 5px; border-radius: 50%; background: #4ecdc4; display: inline-block; }
.chat-input-row {
  display: flex;
  gap: 6px;
  align-items: flex-end;
}
.chat-input {
  flex: 1;
  background: var(--sh);
  border: 1px solid var(--bd);
  border-radius: 6px;
  color: var(--tx);
  font-family: inherit;
  font-size: 12.5px;
  padding: 7px 10px;
  resize: none;
  height: 36px;
  max-height: 120px;
  line-height: 1.5;
  outline: none;
  transition: border-color .1s;
}
.chat-input:focus { border-color: var(--ac); }
.chat-input::placeholder { color: var(--tx2); }
.chat-send {
  background: var(--ac);
  border: none;
  border-radius: 6px;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  color: #fff;
  transition: opacity .1s;
}
.chat-send:hover { opacity: .85; }
.chat-send:disabled { opacity: .3; cursor: default; }

/* toolbar buttons in tabs bar */
.toolbar-btns {
  margin-left: auto;
  display: flex;
  gap: 2px;
  align-items: center;
  padding-right: 4px;
  flex-shrink: 0;
}
.toolbar-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--tx2);
  padding: 5px 10px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-family: inherit;
  transition: color .1s, background .1s;
  white-space: nowrap;
}
.toolbar-btn:hover { color: var(--tx); background: var(--sh); }
.toolbar-btn.active { color: var(--ac); background: var(--ac2); }

@media (max-width: 580px) {
  .sb { display: none; }
  .chat-panel { width: 100%; position: absolute; right: 0; top: 0; bottom: 0; z-index: 10; }
}
</style>
</head>
<body>

<aside class="sb">
  <div class="sb-top">
    <span class="sb-title">Explorer</span>
    <button class="rbtn" id="rbtn" title="Refresh (R)">
      <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M1 4v4h4M15 12v-4h-4"/>
        <path d="M13.5 7A6 6 0 1 0 12 12.5"/>
      </svg>
      Refresh
    </button>
  </div>
  <div class="sb-srch">
    <input class="srch" id="srch" placeholder="Search…" autocomplete="off" spellcheck="false">
  </div>
  <nav class="tree" id="tree">
    <div class="loading">Loading…</div>
  </nav>
</aside>

<div class="main">
  <div class="tabrow">
    <div class="tabs" id="tabs"></div>
    <div class="toolbar-btns">
      <button class="toolbar-btn" id="graph-btn">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <circle cx="3" cy="8" r="2.2" fill="currentColor" stroke="none"/>
          <circle cx="13" cy="3.5" r="2.2" fill="currentColor" stroke="none"/>
          <circle cx="13" cy="12.5" r="2.2" fill="currentColor" stroke="none"/>
          <line x1="5.1" y1="7.3" x2="10.9" y2="4.2"/>
          <line x1="5.1" y1="8.7" x2="10.9" y2="11.8"/>
        </svg>
        Graph
      </button>
      <button class="toolbar-btn" id="chat-toggle-btn">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H2a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h3l3 3 3-3h3a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1z"/>
        </svg>
        Chat
      </button>
    </div>
  </div>
  <div class="content" id="content">
    <div class="welcome">
      <div class="welcome-icon">vault/</div>
      <h3>__TITLE__</h3>
      <p>Select a note from the left panel</p>
    </div>
  </div>
  <div id="graph-wrap" hidden>
    <div id="fg-mount" style="width:100%;height:100%"></div>
    <div class="graph-hint">scroll = zoom · drag = pan · click node to open note</div>
  </div>
</div>

<div class="chat-panel" id="chat-panel" hidden>
  <div class="chat-header">
    <span class="chat-header-title">Chat</span>
    <button class="chat-close" id="chat-close" title="Close">×</button>
  </div>
  <div class="chat-ctx" id="chat-ctx">No note open</div>
  <div class="chat-messages" id="chat-messages">
    <div class="chat-empty">Ask a question about the current note or entire vault</div>
  </div>
  <div class="chat-footer">
    <div class="chat-model" id="chat-model-info">
      <span class="chat-model-dot"></span>
      <span id="chat-model-name">ollama / llama3.2</span>
    </div>
    <div class="chat-input-row">
      <textarea class="chat-input" id="chat-input" placeholder="Ask about this note…" rows="1"></textarea>
      <button class="chat-send" id="chat-send" title="Send (Enter)">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
          <path d="M1 1l14 7-14 7V9.5l10-1.5-10-1.5z"/>
        </svg>
      </button>
    </div>
  </div>
</div>

__LIBS__
__DATA__
<script>
const FLAGS = __FLAGS__;   // {chat, watch, export}
const FM = __FM__;
const TC = __TC__;
const tcol = t => TC[t] || "#888";
const DONE_STATES = new Set(["done", "waived", "missed"]);
const todayStr = () => { const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };

// Only http(s), mailto, #fragment and scheme-less (relative) URLs are allowed.
function safeUrl(u) {
  u = String(u ?? "").trim();
  const probe = u.replace(/[\u0000-\u0020\u00ad\u200b-\u200f\u2028\u2029\ufeff]/g, "");
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(probe);
  if (m && !/^(https?|mailto)$/i.test(m[1])) return null;
  if (!m && /^[\\/]{2}/.test(probe)) return null;
  return u;
}
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// All data access goes through api(): live server (fetch) or the data embedded by --export.
const EXPORT = window.__EXPORT__ || null;
async function api(kind, arg) {
  if (EXPORT) {
    if (kind === "vault") return EXPORT.vault;
    if (kind === "graph") return EXPORT.graph;
    const n = EXPORT.notes[arg];
    if (!n) throw new Error("Note not found");
    return n;
  }
  const url = kind === "vault" ? "/api/vault" : kind === "graph" ? "/api/graph"
    : "/api/note?path=" + encodeURIComponent(arg);
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText || ("HTTP " + r.status));
  return r.json();
}

let vault = null, tabs = [], activeTab = null, expanded = new Set(), searchQ = "";

// ── Note resolution (mirrors VaultIndex.resolve in viewer.py) ──
let noteIdx = null;
function resolveNote(target) {
  if (!vault) return null;
  if (!noteIdx) {
    noteIdx = { byPath: new Map(), byStem: new Map() };
    vault.folders.forEach(s => s.notes.forEach(n => {
      const p = n.path.toLowerCase();
      noteIdx.byPath.set(p, n);
      noteIdx.byPath.set(p.replace(/\.md$/, ""), n);
      const st = n.name.toLowerCase();
      if (!noteIdx.byStem.has(st)) noteIdx.byStem.set(st, n);
    }));
  }
  const t = String(target || "").trim().replace(/\\/g, "/").replace(/^\/+/, "")
    .replace(/\.md$/i, "").toLowerCase();
  return noteIdx.byPath.get(t) || noteIdx.byStem.get(t) || null;
}

// ── Markdown extensions: wikilinks + callouts ──
if (typeof marked !== "undefined") {
  marked.use({
    extensions: [{
      name: "wikilink", level: "inline",
      start(src) { const i = src.search(/!?\[\[/); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^(!?)\[\[([^\]\n|#]+?)(?:#([^\]\n|]*?))?(?:\\?\|([^\]\n]*?))?\]\]/.exec(src);
        if (!m) return;
        return { type: "wikilink", raw: m[0], embed: !!m[1],
          target: m[2].replace(/\\$/, "").trim(),
          heading: (m[3] || "").replace(/\\$/, "").trim(),
          label: (m[4] || "").trim() };
      },
      renderer(tok) {
        const label = tok.label || (tok.heading ? `${tok.target} › ${tok.heading}` : tok.target);
        const n = resolveNote(tok.target);
        if (!n) return `<span class="wl missing" title="Note not found">${esc(label)}</span>`;
        return `<a class="wl" href="#" data-path="${esc(n.path)}">${esc(label)}</a>`;
      },
    }],
    renderer: {
      // raw HTML in Markdown is shown as text, never injected
      html(html) { return esc(typeof html === "string" ? html : html.text); },
      link(href, title, text) {
        const u = safeUrl(href);
        if (u === null) return text;
        const t = title ? ` title="${esc(title)}"` : "";
        if (u.startsWith("#")) return `<a href="#" data-anchor="${esc(u.slice(1))}"${t}>${text}</a>`;
        const ext = /^(https?:|mailto:)/i.test(u) ? ` target="_blank" rel="noopener noreferrer"` : "";
        return `<a href="${esc(u)}"${t}${ext}>${text}</a>`;
      },
      image(href, title, text) {
        const u = safeUrl(href);
        if (u === null) return esc(text);
        return `<img src="${esc(u)}" alt="${esc(text)}"${title ? ` title="${esc(title)}"` : ""}>`;
      },
      blockquote(quote) {
        const m = /^\s*<p>\[!([A-Za-z][\w-]*)\][+-]?[ \t]*([^\n]*?)(?:\n([\s\S]*?))?<\/p>([\s\S]*)$/.exec(quote);
        if (!m) return false;
        const type = m[1].toLowerCase();
        const title = m[2].trim() || type.charAt(0).toUpperCase() + type.slice(1);
        const body = (m[3] ? `<p>${m[3]}</p>` : "") + m[4];
        return `<div class="callout c-${type}"><div class="callout-title">${title}</div>` +
          (body.trim() ? `<div class="callout-body">${body}</div>` : "") + `</div>\n`;
      },
    },
  });
}

document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest("a[data-anchor]");
  if (!a) return;
  e.preventDefault();
  const slug = h => h.toLowerCase().trim().replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s+/g, "-");
  const want = a.dataset.anchor.toLowerCase();
  const h = [...document.querySelectorAll(".md h1,.md h2,.md h3,.md h4,.md h5,.md h6")]
    .find(x => slug(x.textContent) === want);
  if (h) h.scrollIntoView();
});

// ── Dataview inline fields: [key:: value] ──
if (typeof marked !== "undefined") {
  marked.use({
    extensions: [{
      name: "dvfield", level: "inline",
      start(src) { const i = src.search(/\[[A-Za-z][\w -]*::/); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\[([A-Za-z][\w -]*?)::[ \t]*([^\]\n]*?)\]/.exec(src);
        if (!m) return;
        return { type: "dvfield", raw: m[0], key: m[1].trim(), value: m[2].trim() };
      },
      renderer(tok) {
        const key = tok.key.toLowerCase(), v = tok.value;
        if (key === "state") {
          const st = v.toLowerCase();
          return `<span class="dv dv-state s-${esc(st.replace(/[^a-z0-9]+/g, "-"))}" data-state="${esc(st)}">${esc(v)}</span>`;
        }
        if (key === "due") {
          const iso = /^\d{4}-\d{2}-\d{2}$/.test(v) ? ` data-due="${esc(v)}"` : "";
          return `<span class="dv dv-due"${iso}>due ${esc(v)}</span>`;
        }
        if (key === "link" && /^https?:\/\//i.test(v) && safeUrl(v) !== null)
          return `<a class="dv dv-link" href="${esc(v)}" target="_blank" rel="noopener noreferrer">Open ↗</a>`;
        return `<span class="dv"><span class="k">${esc(tok.key)}</span>${esc(v)}</span>`;
      },
    }],
    renderer: {
      // open task with a past due date and a still-open state → "overdue"
      listitem(text, task, checked) {
        let cls = "";
        if (task && !checked) {
          const own = text.split(/<[uo]l[\s>]/)[0];            // ignore nested lists
          const due = /data-due="(\d{4}-\d{2}-\d{2})"/.exec(own);
          const st = /data-state="([^"]*)"/.exec(own);
          if (due && due[1] < todayStr() && !(st && DONE_STATES.has(st[1]))) cls = ' class="overdue"';
        }
        return `<li${cls}>${text}</li>\n`;
      },
    },
  });
}

// delegated click handler for wikilinks (content, backlinks, chat bubbles)
document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest("a.wl[data-path]");
  if (!a) return;
  e.preventDefault();
  const n = findNote(a.dataset.path);
  if (n) openNote({ path: n.path, name: n.name, type: n.type, folder: n.path.split("/")[0] });
});

// ── Tree ──────────────────────────────────────────────────────
function renderTree() {
  const tree = document.getElementById("tree");
  if (!vault) return;
  tree.innerHTML = "";
  const q = searchQ.toLowerCase();

  vault.folders.forEach(sec => {
    let notes = sec.notes;
    if (q) notes = notes.filter(n =>
      n.name.toLowerCase().includes(q) || (n.summary||"").toLowerCase().includes(q));
    if (q && !notes.length) return;

    const fm = FM[sec.folder] || { bg:"#1a1a1a", fg:"#777", letter:"?" };
    const open = q || expanded.has(sec.folder);
    const label = sec.folder.replace(/^\d+[-\s]+/, "");

    const el = document.createElement("div");
    el.className = "folder";

    const hd = document.createElement("div");
    hd.className = "fhd" + (open ? " open" : "");
    hd.innerHTML =
      `<svg class="ch" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 4 10 8 6 12"/></svg>` +
      `<span class="fi" style="background:${esc(fm.bg)};color:${esc(fm.fg)}">${esc(fm.letter)}</span>` +
      `<span class="fn">${esc(label)}</span>` +
      `<span class="fc">${notes.length}</span>`;

    const fnotes = document.createElement("div");
    fnotes.className = "fnotes";
    if (!open) fnotes.hidden = true;

    notes.forEach(n => {
      const ni = document.createElement("div");
      ni.className = "ni" + (activeTab === n.path ? " active" : "");
      ni.dataset.path = n.path;
      ni.innerHTML =
        `<span class="nd" style="background:${esc(tcol(n.type))}"></span>` +
        `<span class="nn">${esc(n.name)}</span>`;
      ni.addEventListener("click", () => openNote({
        path: n.path, name: n.name, type: n.type, folder: sec.folder
      }));
      fnotes.appendChild(ni);
    });

    hd.addEventListener("click", () => {
      const now = !hd.classList.contains("open");
      hd.classList.toggle("open", now);
      fnotes.hidden = !now;
      now ? expanded.add(sec.folder) : expanded.delete(sec.folder);
    });

    el.appendChild(hd);
    el.appendChild(fnotes);
    tree.appendChild(el);
  });
}

// ── Tabs ──────────────────────────────────────────────────────
function renderTabs() {
  const bar = document.getElementById("tabs");
  bar.innerHTML = "";
  tabs.forEach(t => {
    const el = document.createElement("div");
    el.className = "tab" + (activeTab === t.path ? " active" : "");
    el.innerHTML = `<span class="ti" style="background:${esc(tcol(t.type))}"></span>${esc(t.name)}`;
    el.addEventListener("click", () => switchTo(t));
    bar.appendChild(el);
  });
}

const switchTo = t => openNote(t);

function highlight() {
  document.querySelectorAll(".ni").forEach(el =>
    el.classList.toggle("active", el.dataset.path === activeTab));
}

// ── Open note ─────────────────────────────────────────────────
const findNote = path => vault?.folders.flatMap(s => s.notes).find(n => n.path === path);
const hashFor = path => "#" + encodeURIComponent(path);

// Opens (or switches to) a note tab. activeTab is set *before* the hash is written, so the
// resulting hashchange sees "already active" and does nothing: no double load, no loop.
function openNote(note) {
  if (graphMode) closeGraph();
  if (!tabs.find(t => t.path === note.path)) {
    tabs.push(note);
    if (tabs.length > 8) tabs.shift();
  }
  activeTab = note.path;
  renderTabs();
  loadContent(note);
  highlight();
  if (location.hash !== hashFor(note.path)) location.hash = hashFor(note.path);
}

const welcomeHTML = document.getElementById("content").innerHTML;
function routeFromHash() {
  if (!vault) return;                         // initial route happens after the first load()
  let p = "";
  try { p = decodeURIComponent(location.hash.replace(/^#/, "")); } catch (_) { return; }
  if (!p) {                                   // back to the empty hash → welcome screen
    if (activeTab === null) return;
    activeTab = null;
    renderTabs(); highlight();
    document.getElementById("content").innerHTML = welcomeHTML;
    return;
  }
  if (p === activeTab) return;
  const n = findNote(p);
  if (n) openNote({ path: n.path, name: n.name, type: n.type, folder: n.path.split("/")[0] });
}
window.addEventListener("hashchange", routeFromHash);

async function loadContent(note) {
  const area = document.getElementById("content");
  area.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    showNote(await api("note", note.path));
  } catch(e) {
    area.innerHTML = `<div class="err">Error: ${esc(e.message)}</div>`;
  }
}

function backlinksHtml(list) {
  if (!list || !list.length) return "";
  return `<div class="backlinks"><h4>Linked from (${list.length})</h4><ul>` +
    list.map(b => `<li><a class="wl" href="#" data-path="${esc(b.path)}">${esc(b.name)}</a></li>`).join("") +
    `</ul></div>`;
}

function showNote(data) {
  let raw = data.content;
  let chips = "";

  const fmMatch = raw.match(/^---\n([\s\S]*?)\n---\n?/);
  if (fmMatch) {
    raw = raw.slice(fmMatch[0].length);
    const lines = fmMatch[1].split("\n"), kv = [];
    for (let i = 0; i < lines.length; i++) {
      const m = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(lines[i]);
      if (!m) continue;
      let v = m[2].trim();
      if (!v) {                       // YAML block list: "key:" followed by "- item" lines
        const items = [];
        while (i + 1 < lines.length && /^\s*-\s+/.test(lines[i + 1]))
          items.push(lines[++i].replace(/^\s*-\s+/, "").trim().replace(/^["']|["']$/g, ""));
        v = items.join(", ");
      }
      if (v && m[1] !== "title") kv.push([m[1], v]);
    }
    chips = kv.map(([key, v]) => {
      const c = key === "type" ? tcol(v) : key === "status" ? "#68b068" : "#888";
      return `<span class="chip" style="background:${esc(c)}20;color:${esc(c)}">${esc(key)}: ${esc(v)}</span>`;
    }).join("");
    if (chips) chips = `<div class="chips">${chips}</div>`;
  }

  const parts = data.path.split("/");
  const bc = parts.length > 1
    ? `<div class="bc"><span>${esc(parts[0].replace(/^\d+[-\s]+/,""))}</span><span class="sep">/</span><span>${esc(data.name)}</span></div>`
    : `<div class="bc"><span>${esc(data.name)}</span></div>`;

  let html = "";
  if (typeof marked !== "undefined") {
    html = marked.parse(raw);
  } else {
    html = `<pre style="white-space:pre-wrap;font-family:inherit">${esc(raw)}</pre>`;
  }

  document.getElementById("content").innerHTML =
    `<div class="note-body">${bc}${chips}<div class="md">${html}</div>${backlinksHtml(data.backlinks)}</div>`;

  setChatContext(data.name, data.content);
}

// ── Load vault ────────────────────────────────────────────────
async function load() {
  const btn = document.getElementById("rbtn");
  btn.classList.add("spinning");
  try {
    const first = !vault;
    vault = await api("vault");
    noteIdx = null;
    renderTree();
    if (first) routeFromHash();
  } catch(e) {
    document.getElementById("tree").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    btn.classList.remove("spinning");
  }
}

// ── Events ────────────────────────────────────────────────────
document.getElementById("srch").addEventListener("input", e => {
  searchQ = e.target.value.trim();
  if (searchQ) vault?.folders.forEach(s => expanded.add(s.folder));
  renderTree();
});

document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "r" && !e.ctrlKey && !e.metaKey) load();
  if (e.key === "/") { e.preventDefault(); document.getElementById("srch").focus(); }
  if (e.key === "Escape") {
    if (graphMode) closeGraph();
    else if (FLAGS.chat && chatOpen) closeChat();
    else document.getElementById("srch").blur();
  }
});

document.getElementById("rbtn").addEventListener("click", load);

// ── Graph ─────────────────────────────────────────────────────
let graphMode = false;
let graphData = null;
let fgInstance = null;

async function openGraph() {
  graphMode = true;
  document.getElementById("graph-btn").classList.add("active");
  const wrap = document.getElementById("graph-wrap");
  wrap.hidden = false;
  if (!graphData) {
    try {
      graphData = await api("graph");
    } catch(e) {
      wrap.querySelector("#fg-mount").innerHTML = `<div style="padding:40px;color:#c05858">Graph: ${esc(e.message)}</div>`;
      return;
    }
  }
  renderGraph();
}

function closeGraph() {
  graphMode = false;
  document.getElementById("graph-btn").classList.remove("active");
  document.getElementById("graph-wrap").hidden = true;
  fgInstance = null;
}

function renderGraph() {
  const wrap = document.getElementById("graph-wrap");
  const mount = document.getElementById("fg-mount");
  mount.innerHTML = "";

  const nodes = graphData.nodes.map(d => ({...d}));
  const links = graphData.edges.map(e => ({source: e.source, target: e.target}));

  const degree = {};
  links.forEach(l => {
    degree[l.source] = (degree[l.source] || 0) + 1;
    degree[l.target] = (degree[l.target] || 0) + 1;
  });

  const nodeRadius = d => {
    const deg = degree[d.id] || 0;
    return deg === 0 ? 5 : 6 + Math.min(deg * 1.2, 6);
  };

  fgInstance = ForceGraph()(mount)
    .width(wrap.offsetWidth)
    .height(wrap.offsetHeight)
    .graphData({ nodes, links })
    .nodeId("id")
    .linkSource("source")
    .linkTarget("target")
    .backgroundColor("#0d0d0d")
    .linkColor(() => "rgba(255,255,255,0.12)")
    .linkWidth(1)
    .nodeLabel(d => `${d.name}${degree[d.id] ? ` · ${degree[d.id]} links` : ""}`)
    .nodeCanvasObject((node, ctx, gs) => {
      const r = nodeRadius(node);
      const col = tcol(node.type);
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = col + (degree[node.id] ? "dd" : "55");
      ctx.fill();
      ctx.strokeStyle = col + "44";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      if (gs > 0.55) {
        const lbl = node.name.length > 22 ? node.name.slice(0, 20) + "…" : node.name;
        ctx.font = `${10 / gs}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "rgba(255,255,255,0.45)";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(lbl, node.x, node.y + r + 3);
      }
    })
    .nodePointerAreaPaint((node, color, ctx) => {
      ctx.beginPath();
      ctx.arc(node.x, node.y, nodeRadius(node) + 5, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    })
    .onNodeClick(node => {
      closeGraph();
      const n = vault?.folders.flatMap(s => s.notes).find(n => n.path === node.id);
      if (n) openNote({ path: n.path, name: n.name, type: n.type, folder: node.folder });
    });

  fgInstance.d3Force("charge").strength(-240);
  fgInstance.d3Force("link").distance(95).strength(0.7);
}

document.getElementById("graph-btn").addEventListener("click", () => {
  graphMode ? closeGraph() : openGraph();
});

// ── Chat ──────────────────────────────────────────────────────────────────────
let chatHistory = [];
let chatNoteContext = "";
let chatNoteTitle = "";
let chatOpen = false;
let chatConfigLoaded = false;

async function openChat() {
  chatOpen = true;
  document.getElementById("chat-panel").hidden = false;
  document.getElementById("chat-toggle-btn").classList.add("active");
  document.getElementById("chat-input").focus();
  if (!chatConfigLoaded) {
    chatConfigLoaded = true;
    try {
      const cfg = await fetch("/api/config").then(r => r.json());
      const dot = document.querySelector(".chat-model-dot");
      document.getElementById("chat-model-name").textContent =
        `${cfg.provider} / ${cfg.model}`;
      dot.style.background = cfg.provider === "anthropic" ? "#e07050" : "#4ecdc4";
    } catch(_) {}
  }
}

function closeChat() {
  chatOpen = false;
  document.getElementById("chat-panel").hidden = true;
  document.getElementById("chat-toggle-btn").classList.remove("active");
}

function setChatContext(noteTitle, noteContent) {
  if (!FLAGS.chat) return;            // chat panel is removed in offline/export mode
  chatNoteTitle = noteTitle || "";
  chatNoteContext = noteContent || "";
  const ctx = document.getElementById("chat-ctx");
  ctx.innerHTML = noteTitle
    ? `Context: <span>${esc(noteTitle)}</span>`
    : "No note open";
}

function chatAddBubble(role, text) {
  const msgs = document.getElementById("chat-messages");
  const empty = msgs.querySelector(".chat-empty");
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = `chat-bubble ${role}`;
  el.textContent = text;
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

async function chatSend() {
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  input.value = "";
  input.style.height = "36px";
  document.getElementById("chat-send").disabled = true;

  chatAddBubble("user", text);
  chatHistory.push({ role: "user", content: text });

  const bubble = chatAddBubble("assistant", "");
  bubble.classList.add("thinking");
  bubble.textContent = "…";

  let fullText = "";
  let started = false;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Vault-Viewer": "1" },
      body: JSON.stringify({ messages: chatHistory, context: chatNoteContext }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${err}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") continue;
        let json;
        try { json = JSON.parse(payload); } catch (_) { continue; }

        if (json.error) { throw new Error(json.error.message || JSON.stringify(json.error)); }

        const delta = json.choices?.[0]?.delta?.content ?? "";
        if (delta) {
          if (!started) {
            started = true;
            bubble.classList.remove("thinking");
            bubble.textContent = "";
          }
          fullText += delta;
          bubble.textContent = fullText;
          const msgs = document.getElementById("chat-messages");
          msgs.scrollTop = msgs.scrollHeight;
        }
      }
    }

    if (fullText) {
      chatHistory.push({ role: "assistant", content: fullText });
      if (typeof marked !== "undefined") {
        bubble.classList.add("md");
        bubble.innerHTML = marked.parse(fullText);
      }
    } else if (!started) {
      bubble.textContent = "(no response)";
    }

  } catch (e) {
    bubble.classList.remove("thinking");
    bubble.classList.add("assistant");
    bubble.textContent = `Error: ${e.message}`;
  }

  document.getElementById("chat-send").disabled = false;
  input.focus();
}

if (!FLAGS.chat) {
  document.getElementById("chat-toggle-btn").remove();
  document.getElementById("chat-panel").remove();
} else {
  document.getElementById("chat-toggle-btn").addEventListener("click", () => {
    chatOpen ? closeChat() : openChat();
  });
}
if (FLAGS.chat) {
  document.getElementById("chat-close").addEventListener("click", closeChat);
  document.getElementById("chat-send").addEventListener("click", chatSend);
  document.getElementById("chat-input").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatSend(); }
    setTimeout(() => {
      const el = e.target;
      el.style.height = "36px";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    }, 0);
  });
}

load();

// ── File watcher ──────────────────────────────────────────────────────────
(function startWatcher() {
  if (!FLAGS.watch) return;
  const es = new EventSource("/api/watch");
  es.onmessage = () => {
    load();
    if (activeTab) {
      const t = tabs.find(x => x.path === activeTab);
      if (t) loadContent(t);
    }
  };
  es.onerror = () => {
    es.close();
    setTimeout(startWatcher, 3000);
  };
})();
</script>
</body>
</html>"""


# ── HTTP server ──────────────────────────────────────────────────────────────

CSP_OFFLINE = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")

VENDOR_DIR: Path = Path(__file__).resolve().parent / "vendor"
VENDOR_FILES = ("marked.min.js", "force-graph.min.js")
CDN_LIBS = (
    '<script src="https://cdn.jsdelivr.net/npm/force-graph@1.43.5/dist/force-graph.min.js"></script>\n'
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>'
)
CDN_FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600'
             '&family=JetBrains+Mono:wght@400&display=swap">')


def check_vendor() -> None:
    """Exit with a clear message if offline mode lacks its vendored libraries."""
    missing = [f for f in VENDOR_FILES if not (VENDOR_DIR / f).is_file()]
    if missing:
        sys.exit(f"error: missing vendored file(s) in {VENDOR_DIR}: {', '.join(missing)}\n"
                 f"       run: python3 {Path(__file__).resolve().parent / 'tools' / 'fetch-vendor.py'}")


def js_json(obj) -> str:
    """JSON safe to inline inside a <script> element."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\u0021--")


def render_page(mode: str = "online", data: dict = None) -> str:
    """mode: 'online' (CDN libs, chat) | 'offline' (vendored, no chat) | 'export' (single file)."""
    flags = {"chat": mode == "online", "watch": mode != "export", "export": mode == "export"}
    head, libs, data_tag = "", "", ""
    if mode == "online":
        head, libs = CDN_FONTS, CDN_LIBS
    elif mode == "offline":
        libs = "\n".join(f'<script src="/vendor/{f}"></script>' for f in VENDOR_FILES)
    else:
        check_vendor()
        head = ('<meta http-equiv="Content-Security-Policy" content="' +
                CSP_OFFLINE.replace("connect-src 'self'", "connect-src 'none'") + '">')
        libs = "\n".join(
            "<script>" + (VENDOR_DIR / f).read_text(encoding="utf-8").replace("</script", "<\\/script") + "</script>"
            for f in VENDOR_FILES)
        data_tag = "<script>window.__EXPORT__ = " + js_json(data) + ";</script>"
    repl = {
        "FM": js_json(CONFIG.get("folders", {})),
        "TC": js_json(CONFIG.get("types", {})),
        "TITLE": html_escape(CONFIG.get("title", VAULT.name)),
        "FLAGS": js_json(flags),
        "HEAD": head, "LIBS": libs, "DATA": data_tag,
    }
    # single pass, so substituted content is never re-scanned for placeholders
    return re.sub(r"__(FM|TC|TITLE|FLAGS|HEAD|LIBS|DATA)__", lambda m: repl[m.group(1)], VIEWER_HTML)


BIND_HOST = "127.0.0.1"
MAX_BODY = 1_000_000  # bytes accepted by POST /api/chat


def safe_note_path(rel: str):
    """Resolve a vault-relative note path; None if it escapes the vault, is skipped or not .md."""
    if not rel or "\0" in rel:
        return None
    try:
        root = VAULT.resolve()
        full = (root / rel).resolve()
        if not full.is_relative_to(root):
            return None
        relp = full.relative_to(root)
    except (ValueError, OSError):
        return None
    if full.suffix != ".md" or skip(relp):
        return None
    return full


class VaultHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ── request guards ──
    def _host_ok(self) -> bool:
        """Reject DNS-rebinding: Host must be localhost:<port> or 127.0.0.1:<port>."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in getattr(self.server, "allowed_hosts", set())

    def _guard(self) -> bool:
        if not self._host_ok():
            self._respond(403, "text/plain", b"Forbidden: bad Host header")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = render_page("offline" if OFFLINE else "online").encode("utf-8")
            self._respond(200, "text/html; charset=utf-8", body)

        elif parsed.path.startswith("/vendor/"):
            name = parsed.path[len("/vendor/"):]
            if not OFFLINE or name not in VENDOR_FILES or not (VENDOR_DIR / name).is_file():
                self._respond(404, "text/plain", b"Not found")
                return
            self._respond(200, "application/javascript; charset=utf-8", (VENDOR_DIR / name).read_bytes())

        elif parsed.path == "/api/vault":
            body = json.dumps(scan_vault(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/graph":
            body = json.dumps(build_graph(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/note":
            params = parse_qs(parsed.query)
            rel = params.get("path", [""])[0]
            note_path = safe_note_path(rel)
            if note_path is None:
                self._respond(403, "text/plain", b"Forbidden")
                return
            if not note_path.is_file():
                self._respond(404, "text/plain", b"Not found")
                return
            text = note_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            rel_posix = note_path.relative_to(VAULT.resolve()).as_posix()
            payload = {
                "path": rel_posix, "name": note_path.stem, "content": text,
                "type": fm.get("type", ""), "status": fm.get("status", ""),
                "backlinks": get_index().backlinks_for(rel_posix),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/watch":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            if OFFLINE:
                self.send_header("Content-Security-Policy", CSP_OFFLINE)
            self.end_headers()
            last = vault_snapshot()
            ticks = 0
            try:
                while True:
                    time.sleep(1.5)
                    ticks += 1
                    current = vault_snapshot()
                    if current != last:
                        last = current
                        self.wfile.write(b"data: changed\n\n")
                        self.wfile.flush()
                    elif ticks % 10 == 0:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass

        elif parsed.path == "/api/config":
            if OFFLINE:
                self._respond(403, "text/plain", b"Chat is disabled in offline mode")
                return
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            kb_key   = os.environ.get("KB_API_KEY", "")
            kb_base  = os.environ.get("KB_API_BASE", "")
            kb_model = os.environ.get("KB_MODEL", "")
            claude_cli = shutil.which("claude")
            explicit = bool(kb_base or kb_key)
            if explicit:
                provider, model = "openai", kb_model or "llama3.2"
            elif bool(anthropic_key):
                provider, model = "anthropic", kb_model or "claude-haiku-4-5-20251001"
            elif claude_cli:
                provider, model = "claude-cli", kb_model or "claude-sonnet-4-6"
            else:
                provider, model = "openai", kb_model or "llama3.2"
            cfg = {"provider": provider, "model": model}
            body = json.dumps(cfg).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/api/chat":
            self._respond(404, "text/plain", b"Not found")
            return
        if OFFLINE:
            self._respond(403, "text/plain", b"Chat is disabled in offline mode")
            return

        # CSRF / cross-origin protection: browsers cannot send a custom header or
        # application/json cross-origin without a CORS preflight, which we never grant.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._respond(415, "text/plain", b"Content-Type must be application/json")
            return
        if self.headers.get("X-Vault-Viewer") != "1":
            self._respond(403, "text/plain", b"Missing X-Vault-Viewer header")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._respond(411, "text/plain", b"Content-Length required")
            return
        if length < 0:
            self._respond(400, "text/plain", b"Bad Content-Length")
            return
        if length > MAX_BODY:
            self._respond(413, "text/plain", b"Request body too large")
            return
        raw = self.rfile.read(length)
        try:
            req_body = json.loads(raw)
        except Exception:
            self._respond(400, "text/plain", b"Bad JSON")
            return

        if not isinstance(req_body, dict) or not isinstance(req_body.get("messages", []), list):
            self._respond(400, "text/plain", b"Bad request")
            return
        messages = req_body.get("messages", [])
        context  = req_body.get("context", "")
        if not isinstance(context, str):
            context = ""
        base_prompt = CONFIG.get("system_prompt", "You are a knowledge base assistant. Answer concisely.")
        system_prompt = (
            f"{base_prompt}\n\nCurrent note:\n\n{context}"
        ) if context else base_prompt

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        kb_key   = os.environ.get("KB_API_KEY", "")
        kb_base  = os.environ.get("KB_API_BASE", "")
        kb_model = os.environ.get("KB_MODEL", "")
        claude_cli = shutil.which("claude")

        explicit = bool(kb_base or kb_key)
        if explicit:
            provider = "openai"
        elif bool(anthropic_key):
            provider = "anthropic"
        elif claude_cli:
            provider = "claude-cli"
        else:
            provider = "openai"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def sse_error(msg: str):
            err = json.dumps({"error": msg})
            self.wfile.write(f"data: {err}\n\n".encode())
            self.wfile.flush()

        def sse_delta(text: str):
            chunk = {"choices": [{"delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        def sse_stream_text(text: str):
            words = text.split(" ")
            for i, word in enumerate(words):
                tok = word + (" " if i < len(words) - 1 else "")
                sse_delta(tok)
                time.sleep(0.015)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        try:
            if provider == "claude-cli":
                model = kb_model or "claude-sonnet-4-6"
                cmd = [claude_cli, "-p",
                       "--model", model,
                       "--output-format", "json"]
                if system_prompt:
                    cmd += ["--system-prompt", system_prompt]

                turns = "\n".join(
                    f"{'Human' if m['role']=='user' else 'Assistant'}: {m['content']}"
                    for m in messages
                )
                cmd.append(turns)

                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                )
                if proc.returncode != 0:
                    raise Exception(proc.stderr.strip() or f"exit {proc.returncode}")
                data = json.loads(proc.stdout)
                reply = data.get("result", "").strip()
                if not reply:
                    raise Exception("Empty response from claude CLI")
                sse_stream_text(reply)

            elif provider == "anthropic":
                model = kb_model or "claude-haiku-4-5-20251001"
                payload = json.dumps({
                    "model": model,
                    "max_tokens": 2048,
                    "system": system_prompt,
                    "messages": messages,
                    "stream": True,
                }).encode("utf-8")
                req = Request(
                    "https://api.anthropic.com/v1/messages",
                    data=payload, method="POST",
                )
                req.add_header("Content-Type", "application/json")
                req.add_header("x-api-key", anthropic_key)
                req.add_header("anthropic-version", "2023-06-01")

                with urlopen(req, timeout=90) as upstream:
                    for raw_line in upstream:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                        if not line.startswith("data: "):
                            continue
                        pl = line[6:].strip()
                        if not pl or pl == "[DONE]":
                            continue
                        try:
                            evt = json.loads(pl)
                        except Exception:
                            continue
                        if evt.get("type") == "content_block_delta":
                            delta = evt.get("delta", {}).get("text", "")
                            if delta:
                                sse_delta(delta)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            else:
                api_base = kb_base or "http://localhost:11434/v1"
                api_key  = kb_key  or "ollama"
                model    = kb_model or "llama3.2"

                oai_messages = [{"role": "system", "content": system_prompt}] + messages
                payload = json.dumps({
                    "model": model,
                    "messages": oai_messages,
                    "stream": True,
                }).encode("utf-8")
                url = api_base.rstrip("/") + "/chat/completions"
                req = Request(url, data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("Authorization", f"Bearer {api_key}")

                with urlopen(req, timeout=90) as upstream:
                    while True:
                        line = upstream.readline()
                        if not line:
                            break
                        self.wfile.write(line)
                        if line in (b"\n", b"\r\n"):
                            self.wfile.flush()

        except URLError as e:
            sse_error(str(e))
        except Exception as e:
            sse_error(str(e))

    def _respond(self, code, ct, body, cache=True):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if OFFLINE:
            self.send_header("Content-Security-Policy", CSP_OFFLINE)
        if not cache:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def export_html(out_path: Path) -> None:
    """Write one self-contained HTML file: UI, libraries and every note (no network needed)."""
    idx = get_index()
    notes = {}
    for n in idx.notes:
        fm = parse_frontmatter(n["text"])
        notes[n["path"]] = {
            "path": n["path"], "name": n["name"], "content": n["text"],
            "type": fm.get("type", ""), "status": fm.get("status", ""),
            "backlinks": idx.backlinks_for(n["path"]),
        }
    html = render_page("export", {"vault": scan_vault(), "graph": build_graph(), "notes": notes})
    out_path.write_text(html, encoding="utf-8")
    print(f"✓ Exported {len(notes)} notes → {out_path} ({out_path.stat().st_size // 1024} KiB)")
    print("⚠ WARNING: this file contains the full content of every note in the vault. "
          "Share it only with people who may read all of it.", file=sys.stderr)


def make_server(port: int = 5001) -> ThreadingHTTPServer:
    """Bind to loopback only and remember which Host headers are acceptable."""
    server = ThreadingHTTPServer((BIND_HOST, port), VaultHandler)
    real = server.server_address[1]
    server.allowed_hosts = {f"localhost:{real}", f"127.0.0.1:{real}"}
    return server


def serve(port: int = 5001) -> None:
    server = make_server(port)
    url = f"http://localhost:{server.server_address[1]}"
    print(f"✓ Vault viewer → {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # --vault PATH  (default: current working directory)
    vault_path = Path.cwd()
    if "--vault" in args:
        vault_path = Path(args[args.index("--vault") + 1])

    init_config(vault_path)

    if "--init" in args:
        init_vault(vault_path)
        return

    if "--export" in args:
        i = args.index("--export")
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            sys.exit("error: --export needs an output file, e.g. --export vault.html")
        check_vendor()
        export_html(Path(args[i + 1]))
        return

    global OFFLINE
    OFFLINE = "--offline" in args or CONFIG.get("offline") is True
    if OFFLINE and "--serve" in args:
        check_vendor()

    if "--serve" in args:
        port = 5001
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        serve(port)
    elif "--dry-run" in args:
        print(build_index())
    else:
        content = build_index()
        OUTPUT.write_text(content, encoding="utf-8")
        print(f"✓ Saved {OUTPUT} ({content.count(chr(10) + '- [[')} notes)")


if __name__ == "__main__":
    main()
