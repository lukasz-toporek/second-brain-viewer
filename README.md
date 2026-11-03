# second-brain-viewer

A lightweight local viewer for Markdown knowledge bases. Zero dependencies — Python stdlib only.

## Features

- **File tree** sidebar with folder grouping and search
- **Note rendering** with Markdown and frontmatter chips
- **Wikilink graph** (force-directed, powered by [force-graph](https://github.com/vasturiano/force-graph))
- **Chat** with your vault via Claude, Anthropic API, or any OpenAI-compatible backend (Ollama etc.)
- **Live reload** — file watcher auto-refreshes the browser on vault changes

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
```

### Tools

```bash
# Lint frontmatter and wikilinks
cd ~/Projects/second-brain
python3 ~/Projects/second-brain-viewer/tools/vault-lint.py

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

Override the model with `KB_MODEL=claude-opus-5`.

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
