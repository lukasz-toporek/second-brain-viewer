#!/usr/bin/env python3
"""Second Brain Viewer.

Usage:
    python3 viewer.py                        # write _index.md in current dir
    python3 viewer.py --dry-run              # print _index.md to stdout
    python3 viewer.py --serve                # local viewer at http://localhost:5001
    python3 viewer.py --vault PATH --serve   # point at a specific vault
    python3 viewer.py --serve --port 8080
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

# Populated by init_config() at startup
VAULT: Path = Path.cwd()
OUTPUT: Path = VAULT / "_index.md"
SKIP_NAMES: set = {"README.md", "_index.md"}
SKIP_DIRS: set = {".obsidian"}
FOLDER_ORDER: list = []
CONFIG: dict = {}


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
        for cfg_name in ("viewer.json", "vault-viewer.json"):
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
            "path":    str(rel),
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

def extract_wikilinks(text: str) -> list[str]:
    return re.findall(r'\[\[([^\]|#\n]+)', text)


def build_graph() -> dict:
    stem_to_path: dict[str, str] = {}
    raw_notes: list[tuple] = []

    for path in sorted(VAULT.rglob("*.md")):
        if skip(path):
            continue
        rel = path.relative_to(VAULT)
        folder = rel.parts[0] if len(rel.parts) > 1 else "(vault root)"
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        node = {
            "id":     str(rel),
            "name":   path.stem,
            "type":   fm.get("type", ""),
            "folder": folder,
        }
        raw_notes.append((path.stem, str(rel), text, node))
        stem_to_path[path.stem.lower()] = str(rel)

    nodes = [n for _, _, _, n in raw_notes]
    edges: list[dict] = []
    seen: set[tuple] = set()

    for stem, rel_path, text, node in raw_notes:
        for link in extract_wikilinks(text):
            target = stem_to_path.get(link.strip().lower())
            if target and target != rel_path:
                key = tuple(sorted([rel_path, target]))
                if key not in seen:
                    edges.append({"source": rel_path, "target": target})
                    seen.add(key)

    return {"nodes": nodes, "edges": edges}


# ── File watcher ────────────────────────────────────────────────────────────

def vault_snapshot() -> dict:
    """Return {rel_path: mtime} for all tracked .md files."""
    snap = {}
    for p in VAULT.rglob("*.md"):
        if not skip(p):
            snap[str(p.relative_to(VAULT))] = p.stat().st_mtime
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap">
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
  font-family: 'Inter', system-ui, sans-serif;
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
  font-family: 'JetBrains Mono', monospace;
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
  font-family: 'JetBrains Mono', monospace;
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

<script src="https://cdn.jsdelivr.net/npm/force-graph@1.43.5/dist/force-graph.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>
<script>
const FM = __FM__;
const TC = __TC__;
const tcol = t => TC[t] || "#888";

let vault = null, tabs = [], activeTab = null, expanded = new Set(), searchQ = "";

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
      `<span class="fi" style="background:${fm.bg};color:${fm.fg}">${fm.letter}</span>` +
      `<span class="fn">${label}</span>` +
      `<span class="fc">${notes.length}</span>`;

    const fnotes = document.createElement("div");
    fnotes.className = "fnotes";
    if (!open) fnotes.hidden = true;

    notes.forEach(n => {
      const ni = document.createElement("div");
      ni.className = "ni" + (activeTab === n.path ? " active" : "");
      ni.dataset.path = n.path;
      ni.innerHTML =
        `<span class="nd" style="background:${tcol(n.type)}"></span>` +
        `<span class="nn">${n.name}</span>`;
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
    el.innerHTML = `<span class="ti" style="background:${tcol(t.type)}"></span>${t.name}`;
    el.addEventListener("click", () => switchTo(t));
    bar.appendChild(el);
  });
}

function switchTo(t) {
  activeTab = t.path;
  renderTabs();
  loadContent(t);
  highlight();
}

function highlight() {
  document.querySelectorAll(".ni").forEach(el =>
    el.classList.toggle("active", el.dataset.path === activeTab));
}

// ── Open note ─────────────────────────────────────────────────
function openNote(note) {
  if (!tabs.find(t => t.path === note.path)) {
    tabs.push(note);
    if (tabs.length > 8) tabs.shift();
  }
  activeTab = note.path;
  renderTabs();
  loadContent(note);
  highlight();
}

async function loadContent(note) {
  const area = document.getElementById("content");
  area.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    const r = await fetch("/api/note?path=" + encodeURIComponent(note.path));
    if (!r.ok) throw new Error(r.statusText);
    showNote(await r.json());
  } catch(e) {
    area.innerHTML = `<div class="err">Error: ${e.message}</div>`;
  }
}

function showNote(data) {
  let raw = data.content;
  let chips = "";

  const fmMatch = raw.match(/^---\n([\s\S]*?)\n---\n?/);
  if (fmMatch) {
    raw = raw.slice(fmMatch[0].length);
    chips = fmMatch[1].split("\n")
      .filter(l => l.includes(":") && !l.trimStart().startsWith("title:"))
      .map(l => {
        const [k, ...rest] = l.split(":");
        const v = rest.join(":").trim();
        if (!v) return "";
        const key = k.trim();
        const c = key === "type"   ? (tcol(v)) :
                  key === "status" ? "#68b068" : "#888";
        return `<span class="chip" style="background:${c}20;color:${c}">${key}: ${v}</span>`;
      }).filter(Boolean).join("");
    if (chips) chips = `<div class="chips">${chips}</div>`;
  }

  const parts = data.path.split("/");
  const bc = parts.length > 1
    ? `<div class="bc"><span>${parts[0].replace(/^\d+[-\s]+/,"")}</span><span class="sep">/</span><span>${data.name}</span></div>`
    : `<div class="bc"><span>${data.name}</span></div>`;

  let html = "";
  if (typeof marked !== "undefined") {
    html = marked.parse(raw);
  } else {
    html = `<pre style="white-space:pre-wrap;font-family:inherit">${raw.replace(/</g,"&lt;")}</pre>`;
  }

  document.getElementById("content").innerHTML =
    `<div class="note-body">${bc}${chips}<div class="md">${html}</div></div>`;

  setChatContext(data.name, data.content);
}

// ── Load vault ────────────────────────────────────────────────
async function load() {
  const btn = document.getElementById("rbtn");
  btn.classList.add("spinning");
  try {
    vault = await fetch("/api/vault").then(r => r.json());
    renderTree();
  } catch(e) {
    document.getElementById("tree").innerHTML = `<div class="err">${e.message}</div>`;
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
    else if (chatOpen) closeChat();
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
      graphData = await fetch("/api/graph").then(r => r.json());
    } catch(e) {
      wrap.querySelector("#fg-mount").innerHTML = `<div style="padding:40px;color:#c05858">Graph: ${e.message}</div>`;
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
  chatNoteTitle = noteTitle || "";
  chatNoteContext = noteContent || "";
  const ctx = document.getElementById("chat-ctx");
  ctx.innerHTML = noteTitle
    ? `Context: <span>${noteTitle}</span>`
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
      headers: { "Content-Type": "application/json" },
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

document.getElementById("chat-toggle-btn").addEventListener("click", () => {
  chatOpen ? closeChat() : openChat();
});
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

load();

// ── File watcher ──────────────────────────────────────────────────────────
(function startWatcher() {
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

class VaultHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            fm_json = json.dumps(CONFIG.get("folders", {}), ensure_ascii=False)
            tc_json = json.dumps(CONFIG.get("types", {}), ensure_ascii=False)
            title = CONFIG.get("title", VAULT.name)
            html = (VIEWER_HTML
                    .replace("__FM__", fm_json)
                    .replace("__TC__", tc_json)
                    .replace("__TITLE__", title))
            body = html.encode("utf-8")
            self._respond(200, "text/html; charset=utf-8", body)

        elif parsed.path == "/api/vault":
            body = json.dumps(scan_vault(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/graph":
            body = json.dumps(build_graph(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/note":
            params = parse_qs(parsed.query)
            rel = unquote(params.get("path", [""])[0])
            note_path = (VAULT / rel).resolve()
            vault_root = VAULT.resolve()
            if not str(note_path).startswith(str(vault_root)):
                self._respond(403, "text/plain", b"Forbidden")
                return
            if not note_path.exists() or note_path.suffix != ".md":
                self._respond(404, "text/plain", b"Not found")
                return
            text = note_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            payload = {
                "path": rel, "name": note_path.stem, "content": text,
                "type": fm.get("type", ""), "status": fm.get("status", ""),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body, cache=False)

        elif parsed.path == "/api/watch":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
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
        parsed = urlparse(self.path)
        if parsed.path != "/api/chat":
            self._respond(404, "text/plain", b"Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req_body = json.loads(raw)
        except Exception:
            self._respond(400, "text/plain", b"Bad JSON")
            return

        messages = req_body.get("messages", [])
        context  = req_body.get("context", "")
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
        if not cache:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 5001) -> None:
    url = f"http://localhost:{port}"
    server = ThreadingHTTPServer(("localhost", port), VaultHandler)
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
