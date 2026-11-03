# second-brain-viewer

A lightweight local viewer for Markdown knowledge bases. Zero dependencies — Python stdlib only.

## Features

- **File tree** sidebar with folder grouping and search
- **Note rendering** with Markdown and frontmatter chips
- **Wikilink graph** (force-directed, powered by [force-graph](https://github.com/vasturiano/force-graph))
- **Chat** with your vault via Claude, Anthropic API, or any OpenAI-compatible backend (Ollama etc.)
- **Live reload** — file watcher auto-refreshes the browser on vault changes
- **Obsidian syntax** — `[[wikilinks]]` (`[[A|alias]]`, `[[dir/A]]`, `[[A#Heading|x]]`), `> [!note]` callouts, a "Linked from" backlinks section
- **Dataview inline fields** — `[state:: …] [due:: …] [link:: …]` render as chips; open tasks past their `due` date are marked overdue
- **Deep links** — `#<note path>` in the URL; Back/Forward work
- **Offline mode** and **single-file export** (see below)

## Usage

Run from the vault directory:

```bash
cd ~/Projects/second-brain
python3 ~/Projects/second-brain-viewer/viewer.py --serve
```

Or point explicitly with `--vault`:

```bash
python3 ~/Projects/second-brain-viewer/viewer.py --vault ~/Projects/second-brain --serve
```

Other modes:

```bash
# Write _index.md to the vault root
python3 viewer.py --vault PATH

# Print _index.md to stdout (dry run)
python3 viewer.py --vault PATH --dry-run

# Custom port
python3 viewer.py --vault PATH --serve --port 8080

# Offline: vendored libraries, no chat, strict CSP (needs vendor/, see below)
python3 viewer.py --vault PATH --serve --offline

# Export everything to one self-contained HTML file
python3 viewer.py --vault PATH --export vault.html
```

### Offline mode

`--offline` (or `"offline": true` in `viewer.json`) serves `marked` and `force-graph` from `vendor/`,
uses a system font stack, hides chat (`/api/chat` and `/api/config` return 403) and sends
`Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'`.
The pinned libraries are committed in `vendor/` (see `vendor/README.md`). To re-download and verify them
(SHA-256 pinned, mismatches are refused):

```bash
python3 tools/fetch-vendor.py                 # download + verify
python3 tools/fetch-vendor.py --print-hashes  # print hashes when bumping versions
```

If `vendor/` is missing, `--offline` and `--export` stop with an error pointing to this script.

### Static export

`--export OUT.html` writes one file containing the UI, both libraries, every note, backlinks and graph data.
It needs no server or network and opens from `file://` (CSP `connect-src 'none'`, no chat, no live reload).
**The file contains the full text of every note** — share it only with people who may read the whole vault.

### Security notes

- The server binds to `127.0.0.1` only and rejects any `Host` other than `localhost:<port>` / `127.0.0.1:<port>` (DNS-rebinding protection).
- `POST /api/chat` requires `Content-Type: application/json` and the `X-Vault-Viewer: 1` header (sent by the page), and bodies over 1 MB are refused.
- `/api/note` resolves paths and refuses anything outside the vault, skipped files/folders and non-`.md` files.
- Note content is never trusted: raw HTML in Markdown is shown as text, and links/images only allow `http(s)`, `mailto`, `#` and relative URLs. Everything inserted via `innerHTML` is escaped.
- Chat still sends the current note to your configured backend; use `--offline` if that is not acceptable.

### Tests

```bash
python3 -m unittest discover tests     # uses the fake vault in tests/fixtures/vault
```

### Tools

```bash
# Lint frontmatter and wikilinks
cd ~/Projects/second-brain
python3 ~/Projects/second-brain-viewer/tools/vault-lint.py
# (accepts --vault PATH; valid types / required fields come from the optional
#  "lint" key in viewer.json — see the template — defaults match the old built-in schema)

# Create today's daily note from template (90-meta/Szablony/Daily.md)
python3 ~/Projects/second-brain-viewer/tools/daily-note.py
```

## Configuration

Place a `viewer.json` in the vault root (or any parent directory — the viewer walks up to find it):

```json
{
  "title": "My Second Brain",
  "skip_dirs": [".obsidian", ".git"],
  "skip_files": ["README.md", "_index.md"],
  "folder_order": ["00-inbox", "01-daily", "10-knowledge", "20-projects", "30-learning", "90-meta"],
  "folders": {
    "00-inbox":     {"bg": "#1e1e1e", "fg": "#777",    "letter": "I"},
    "01-daily":     {"bg": "#102030", "fg": "#5b9bd5", "letter": "D"},
    "10-knowledge": {"bg": "#0a2020", "fg": "#4ecdc4", "letter": "K"},
    "20-projects":  {"bg": "#201008", "fg": "#e07050", "letter": "P"},
    "30-learning":  {"bg": "#1c1608", "fg": "#c8980a", "letter": "L"},
    "90-meta":      {"bg": "#181818", "fg": "#555",    "letter": "M"}
  },
  "types": {
    "daily": "#5b9bd5",
    "meta":  "#888"
  },
  "system_prompt": "You are a knowledge base assistant. Answer concisely."
}
```

A template config is included at `viewer.json` in this repo.

## Chat / AI backend

The viewer auto-detects the available backend in this order:

| Priority | Backend | Env vars |
|---|---|---|
| 1 | Custom OpenAI-compatible | `KB_API_BASE` + optionally `KB_API_KEY`, `KB_MODEL` |
| 2 | Anthropic API | `ANTHROPIC_API_KEY` |
| 3 | `claude` CLI | (must be on PATH) |
| 4 | Ollama (fallback) | `http://localhost:11434` |

Override the model with `KB_MODEL=claude-opus-5`. Chat is unavailable in `--offline` mode.

## Vault structure

The viewer works with any flat directory of Markdown files. The recommended layout:

```
my-vault/
├── viewer.json       ← viewer config (optional)
├── 00-inbox/
├── 01-daily/
├── 10-knowledge/
├── 20-projects/
├── 30-learning/
├── 90-meta/
└── Home.md
```

## Requirements

Python 3.9+. No pip installs needed.
