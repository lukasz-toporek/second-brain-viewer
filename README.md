# second-brain-viewer

A lightweight local viewer for Markdown knowledge bases. Zero dependencies — Python stdlib only.

## Features

- **Nested file tree** sidebar and **full-text search** (see below). Folders nest to any depth: sub-folders first, then notes,
  both sorted case-insensitively and number-aware (`2-x` before `10-x`); every folder shows a chevron, its label (number prefix
  stripped at every level) and a recursive note count, and expands on its own. `folder_order` still orders the top level and
  `(vault root)` notes stay in their own group. Which folders are open is remembered per vault title in `localStorage`, by
  vault-relative folder path (entries for folders that no longer exist are dropped); live reload keeps it and the active highlight.
- **Note rendering** with Markdown and frontmatter chips
- **Wikilink graph** (force-directed, powered by [force-graph](https://github.com/vasturiano/force-graph))
- **Chat** with your vault via Claude, Anthropic API, or any OpenAI-compatible backend (Ollama etc.)
- **Live reload** — file watcher auto-refreshes the browser on vault changes
- **Relative Markdown links** — `[text](../dir/Other%20note.md#Heading)` open the note like a wikilink (resolved against the current note's folder)
- **Attachments** — `![](pic.png)`, `![[pic.png]]`, `[doc](files/spec.pdf)` show vault images and link PDFs (see below)
- **Obsidian syntax** — `[[wikilinks]]` (`[[A|alias]]`, `[[dir/A]]`, `[[A#Heading|x]]`), `> [!note]` callouts (foldable with `-` / `+`), `![[Note]]` embeds, a "Linked from" backlinks section
- **Dataview inline fields** — `[state:: …] [due:: …] [link:: …]` render as chips; open tasks past their `due` date are marked overdue
- **Mobile layout** — below 580 px a ☰ button at the left of the tab row opens the sidebar as an overlay with a backdrop (closes when a note opens, on Escape, or on a backdrop click)
- **Frontmatter links** — in the chips above a note, `[[Note]]` values (also list items, `[[Note|label]]`, `[[Note#Heading]]`) are
  clickable wikilinks like in the body, with the dashed "missing" style when the note does not exist; all other values stay escaped text.
- **Navigation** — opening a note from anywhere (tree, link, URL hash, Back/Forward, search, graph, backlinks, plug-in)
  expands its folders in the tree, highlights it and scrolls it into view without taking focus from the search box. The
  breadcrumb shows the whole folder path (`knowledge / deep / nested / Gamma`, number prefixes stripped); clicking a folder
  segment reveals and opens that folder in the tree (and opens the sidebar overlay on a phone). The Explorer header has
  **Collapse all** and **Reveal active note** buttons next to Refresh.
- **Tabs** — every tab has a **×** (on hover, always on the active tab and on touch devices; `aria-label="Close tab"`);
  middle-click or **Delete** on a focused tab closes it too. Closing the active tab activates its neighbour (or shows the empty
  start screen) and updates the URL hash. At 8 tabs the **least recently used** tab that is not active is dropped. A tab's tooltip is the
  note's full vault path; when the tabs don't fit the bar scrolls sideways (also with the mouse wheel) and keeps the active tab in view.
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

### Full-text search

Typing in the sidebar search box (`/` focuses it) searches the **names and full text** of all notes
(case-insensitive substring, at least 2 characters, debounced ~150 ms) and replaces the tree with a result
list: name, folder and a snippet of ~120 characters around the first match. Clearing the box restores the tree.

The same search is available as `GET /api/search?q=<text>` → `[{"path", "name", "folder", "snippet"}, …]`:
name matches first, then body-only matches, at most 50, an empty list for queries shorter than 2 characters.
It is covered by the same `Host` check as the other endpoints. In an exported file (`--export`) the identical
search runs client-side over the embedded notes.

### Frontmatter

One stdlib parser (`parse_frontmatter()` in `viewer.py`) serves the API, the export and `tools/vault-lint.py`. It returns
`str` for scalars and `list[str]` for lists and understands `key: value`, single/double-quoted values, inline lists
`[a, "b c"]`, block lists (`key:` + `- item` lines), own-line `#` comments, empty values and Windows line endings.
Nested maps, `|` / `>` block scalars and multi-line values are kept as raw text in a string; malformed input never raises.
`type`, `status` and `date` are always strings (a list gives its first item). `GET /api/note` (and each note in an
export) carries the parsed values as `"frontmatter": {…}`, and the frontmatter chips are built from it.

### Embeds and foldable callouts

- `![[Note]]`, `![[Note#Heading]]` and `![[Note|label]]` render the target note's body inline in a bordered box whose
  header link opens the note. With `#Heading` only that section is shown (from the heading to the next heading of the
  same or a higher level). The target's frontmatter is left out. Embeds load after the note itself is shown, through
  the same data layer as everything else, so they also work in an export; relative links and attachments inside an
  embed resolve against the *embedded* note's folder.
- Nesting is limited to 2 levels; a note embedding itself or an A → B → A loop shows a "circular embed" placeholder
  (a third level shows "embed nesting limit reached"). A missing note or heading shows the dashed "missing" placeholder.
  `![[pic.png]]` / `![[a.pdf]]` are attachments (see below), not embeds.
- `> [!tip]- Title` renders collapsed and `> [!tip]+ Title` open (a `<details>` element); `> [!tip] Title` is unchanged.

### Attachments: `/api/file`

Images (`png jpg jpeg gif webp avif bmp ico svg`) and PDFs in the vault are served by
`GET /api/file?path=<vault-relative path>` with the same checks as `/api/note` (resolved inside the vault, skipped
files/folders refused, no `\0`). Anything else (`.md`, `.py`, …) gets 403, a missing file 404, a file over 25 MB 413.
`Content-Type` comes from the extension (never sniffed; `X-Content-Type-Options: nosniff`), and SVGs also get
`Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline'`, so opening one directly cannot run script.

How references resolve:

- **Relative paths** (`![](../img/pic.png)`, `[x](files/a.pdf)`) are resolved against the current note's folder.
- **Bare names** (`![[pic.png]]`, `![[pic.png|alt]]`, `![[pic.png|300]]` for a width) use a vault-wide name lookup like Obsidian.
  The server owns that map: `/api/vault` (and the export) carries `"files"` (lower-cased file name → path, with the same
  `link_deprioritise` rule as notes) and `"file_paths"` (every attachment).
- Images render as `<img src="/api/file?path=…">`; PDFs become a link opening in a new tab. A reference that
  matches no vault file shows a dashed "missing" placeholder with the file name.

### Static export

`--export OUT.html` writes one file containing the UI, both libraries, every note, backlinks and graph data.
It needs no server or network and opens from `file://` (CSP `connect-src 'none'`, no chat, no live reload).
**The file contains the full text of every note** — share it only with people who may read the whole vault.

Attachments in an export: images are inlined as `data:` URLs (each ≤ 2 MB, 50 MB in total). PDFs are never inlined, and
images over a limit are left out; both show an "attachment not included in export" placeholder. `--export` prints a
summary line (included/skipped counts and total size).

### Remote images: `remote_images`

By default (`"allow"`) a note with `![](https://…)` makes your browser fetch that image, which also works as a tracking
pixel. The optional `viewer.json` key `"remote_images"` changes that for **online** mode:

| value | Markdown images from `http(s)` | online CSP `img-src` |
|---|---|---|
| `"allow"` (default) | loaded, as before | `'self' data: https:` |
| `"click"` | placeholder `🖼 remote image: <host>` (a link opening the URL in a new tab) plus a **Load image** button that swaps in the `<img>` for that one image | `'self' data: https:` |
| `"block"` | the same placeholder, no button | `'self' data:` |

Trade-off of `"click"`: the browser has to be allowed to load the image once you press the button, so the CSP cannot
forbid `https:` images; only the renderer holds them back until then. Use `"block"` if you want the CSP to enforce it.
Offline and export always behave as `"block"` (their CSPs never allowed remote images). The policy applies to every image
rendered by the app's Markdown setup, i.e. note bodies, embedded notes and plug-in `renderMarkdown()` output. Any other
value is reported on stderr and treated as `"block"`.

### Theme: light / dark

The viewer follows the operating system's appearance setting (`prefers-color-scheme`), and switches live when the OS
setting changes, including an open graph. The optional `"theme"` key in `viewer.json` is `"system"` (default),
`"dark"` or `"light"`; the last two force a theme. Any other value prints a warning and falls back to `"system"`.
It works the same in online, `--offline` and `--export` and needs no network access. Plug-ins should use the theme
CSS variables (list in `docs/plugins.md`).

Colours in `"folders"` and `"types"` are written for the dark theme, which uses them exactly as configured. In the
light theme:

- **Folder badges.** Add optional `"bg_light"` and `"fg_light"` to a `"folders"` entry and they are used instead.
  Without them the badge is derived from the configured colour: a 15% tint of it over the light sidebar colour, with
  the letter darkened (hue kept) until it reaches **4.5:1** contrast against that tint.
- **Type colours.** An optional top-level `"types_light": {"<type>": "#rrggbb"}` map is used for the types it lists. For
  any other type the configured colour is darkened (hue kept) until it reaches **3:1** against the page background,
  which is enough for dots, graph nodes and chip text.
- Switching the OS theme while the page is open re-colours the tree, tabs, chips and an open graph without a reload.

```json
"folders": {"10-knowledge": {"bg": "#0a2020", "fg": "#4ecdc4", "letter": "K", "bg_light": "#d8efec", "fg_light": "#086860"}},
"types": {"wiedza": "#4ecdc4"},
"types_light": {"wiedza": "#086860"}
```

### Default folder badges and type colours

Without a `"folders"` entry a folder gets a **generated badge**: its first letter or digit (Unicode-aware, upper-cased, after the
number prefix is dropped: `10-knowledge` → `K`, `żółw` → `Ż`, `(vault root)` → `~`) on a colour picked from a small palette by
hashing the folder's vault-relative path, so a folder always has the same colour. It works at every tree level (sub-folders get
a smaller badge) and in both themes (the light theme derives a readable badge as above). Configured `"folders"` entries still win.
A note type that `"types"` does not list gets a stable colour from the same palette, hashed from the type name (an empty type
stays grey; configured types win; the light theme darkens it to 3:1 like any other).

### Plug-ins (frontend, v1)

A vault can add its own toolbar views without forking `viewer.py`. List the scripts in the vault's `viewer.json`:

```json
"plugins": [{"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js", "enabled": true}]
```

- `id` must match `^[a-z0-9][a-z0-9-]{0,40}$`; `file` is vault-relative, ends in `.js` and must resolve (symlinks
  included) **inside the vault**. It is deliberately **not** subject to `skip_dirs`/`skip_files`, so it can live in a
  folder like `.viewer/` that the note scanner skips. Only entries with `"enabled": true` load; the enabled ids and files
  are printed at startup, and a bad entry prints a warning and is skipped (the viewer still starts).
- Each enabled plug-in is served at `/plugins/<id>.js` from an exact allow-list built from the config (no directory
  listing, no other path) and added as `<script src>` after the app script; `--export` inlines it instead. The CSPs are
  unchanged, so a plug-in cannot add remote origins (the export keeps `connect-src 'none'`).
- **Trust model.** A plug-in is trusted code with full access to the page, the vault data the page holds and, in
  online mode, `/api/chat`. Plug-ins are enabled by the vault's *own* config, which is looked up in the vault and up
  to 4 folders above it, so opening a vault you didn't write can run its plug-ins. Enable plug-ins only in vaults you
  trust, and start the viewer with **`--no-plugins`** (or set `"plugins_enabled": false` in the config; default
  `true`) when you open someone else's vault: the `"plugins"` key is then ignored entirely (nothing listed, nothing
  served under `/plugins/`, nothing inlined in an export) and one line says so. When plug-ins are enabled, startup
  prints the config file that enabled them, so you can tell when a config from a parent folder is in effect.
- `--offline` narrows what a plug-in can reach (same-origin only, no chat, `connect-src 'self'`, no remote images),
  and `--export` keeps `connect-src 'none'`. It is not a sandbox: a plug-in still runs with page privileges and can,
  for example, navigate the window away or read everything on the page.
- Host API 1: `window.VaultViewer.register({id, title, api: 1, mount(el, host), unmount?()})` (refused with a console
  warning if `api` is greater than 1 or the `id` is not enabled). `host` gives `api(kind, arg)` (the app's own data
  access, so it works in an export), `openNote(path)`, `notes()` (`[{path, name, type, folder}]`), `esc(s)`,
  `renderMarkdown(md, basePath)` (the app's escaping Markdown, including the remote-images policy) and
  `onVaultChange(cb)` (after live reload / refresh; returns an unsubscribe function). Every registered plug-in gets a
  toolbar button next to **Graph** that opens an overlay (Escape closes it, `unmount` is called).
- `examples/plugins/tasks-board.js` lists every open task with a past `due` date: copy it into your vault and enable it.

See `docs/plugins.md` for the design and what is still future work (Python data providers).

### Security notes

- **Online mode** loads `marked` and `force-graph` from jsDelivr (the same URLs as `tools/fetch-vendor.py`) with
  Subresource Integrity: `integrity="sha384-…"` is computed at startup from the files in `vendor/`, so it cannot drift
  from the pins (if `vendor/` is missing, one warning is printed and the tags are emitted without `integrity`).
  Online pages also carry `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'`.
- Note names are escaped before force-graph inserts graph hover labels as HTML.
- The server binds to `127.0.0.1` only and rejects any `Host` other than `localhost:<port>` / `127.0.0.1:<port>` (DNS-rebinding protection).
- `POST /api/chat` requires `Content-Type: application/json` and the `X-Vault-Viewer: 1` header (sent by the page), and bodies over 1 MB are refused.
- `/plugins/<id>.js` only serves the plug-ins enabled in the vault's config (see Plug-ins).
- `/api/note` resolves paths and refuses anything outside the vault, skipped files/folders and non-`.md` files; `/api/file` does the same for the allow-listed attachment types.
- Symlinks whose real path is outside the vault (note files, folders or attachments) are ignored everywhere: sidebar,
  search, graph, backlinks, `/api/note`, `/api/file` and `--export`. Symlinks that stay inside the vault work.
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
    "00-inbox":     {"bg": "#1e1e1e", "fg": "#777",    "letter": "I", "bg_light": "#e4e4e0", "fg_light": "#555555"},
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
  "types_light": {"daily": "#1f63a6", "meta": "#595959"},
  "link_deprioritise": ["90-meta/Szablony", "_Archive"],
  "remote_images": "allow",
  "theme": "system",
  "plugins": [],
  "plugins_enabled": true,
  "system_prompt": "You are a knowledge base assistant. Answer concisely."
}
```

A template config is included at `viewer.json` in this repo.

### Name clashes: `link_deprioritise`

When two notes share a name (say `Weekly.md` in a real folder and in `Templates/`), `[[Weekly]]` used to resolve to
whichever sorts first. List vault-relative folder prefixes in the optional `"link_deprioritise"` key (default `[]`,
which keeps that behaviour) and a note **outside** those folders wins over one inside them; otherwise the order is
unchanged. Prefixes match whole folder names, case-insensitively (`"Templates"` also covers `Templates/Sub/X.md`).
Path-style links (`[[Templates/Weekly]]`) still reach the deprioritised note.

The server decides: `/api/vault` (and the export) carries the final `name → path` map as `"stems"`, and graph edges,
backlinks and the page's link resolution all use it, so they always agree.

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
