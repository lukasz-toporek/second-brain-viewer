# Design note: vault-specific dashboards ("plugins")

Status: proposal, no code. Goal: let a vault add its own views (e.g. an overdue-tasks
board, a course tracker) without forking `viewer.py` or breaking its constraints
(stdlib only, one runnable file, no build step, local-only, safe against hostile notes).

## Requirements
- Additive: no plugin → behaviour identical to today.
- Vault-owned: a plugin lives in the vault (or next to `viewer.json`), not in this repo.
- Safe by default: a note must never be able to run code; a plugin is code the *user*
  put there and explicitly enabled.
- Works in `--offline` and `--export` (no network, no server-side calls from exports).

## Proposed shape
Config in `viewer.json` (new, optional key):

```json
"plugins": [
  {"id": "tasks-board", "path": ".viewer/plugins/tasks-board", "enabled": true}
]
```

A plugin is a folder with a `plugin.json` manifest and static files:

```json
{
  "id": "tasks-board",
  "title": "Tasks",
  "entry": "board.js",
  "style": "board.css",
  "data": ["tasks.py"]
}
```

### Two layers
1. **Frontend panel (JS).** `entry` is served from `/plugins/<id>/<file>` (exact-filename
   allow-list taken from the manifest, same pattern as `/vendor/`). It registers itself
   against a tiny, versioned host API:

   ```js
   window.VaultViewer.register({
     id: "tasks-board", title: "Tasks",
     mount(el, host) {          // host.api(kind, arg), host.openNote(path), host.notes()
       ...
     }
   });
   ```
   The host adds a toolbar button next to *Graph* / *Chat*; `mount` renders into an
   overlay like the graph. Plugins use `host.api()`, so they work from the embedded data
   in an export too, and `host.openNote()` so hash routing stays consistent.

2. **Optional data provider (Python).** For derived data (e.g. parse every task once),
   a plugin may name a Python file exposing `def provide(index: VaultIndex, config) -> dict`.
   The result is served at `/api/plugin/<id>` and embedded under `EXPORT.plugins[<id>]`
   by `--export`. Loaded with `importlib` only when the plugin is `enabled`.

### Extension points worth reserving (not all needed in v1)
- extra `marked` extensions (new inline syntax like the Dataview chips),
- extra frontmatter chip renderers keyed by field name,
- extra sidebar sections.

## Security model
- Plugins are trusted code, equivalent to running a script: they are **off unless listed
  and `enabled` in the vault's own config**, and the viewer prints the enabled plugin ids
  at startup. A config found in a parent directory is where the user's trust already is.
- Plugin files are served from an allow-list; no directory listing, no path traversal
  (reuse `safe_note_path`-style `resolve()` + `is_relative_to` checks).
- The existing CSP already limits plugins to same-origin scripts/styles and `connect-src 'self'`
  in offline mode; the export keeps `connect-src 'none'`. Plugins cannot add remote origins.
- Note content stays untrusted: the host passes plugins data, never raw HTML, and plugins
  must use the host's `esc()` for anything they put in `innerHTML`.
- Python providers run with the viewer's privileges. Document this plainly; consider
  requiring `--allow-plugins` on the command line (not just config) for Python providers.

## Versioning
`plugin.json` carries `"api": 1`. The host refuses plugins whose `api` is higher than it
supports and logs why, instead of failing silently.

## Open questions
- Should a plugin be able to add `--export` data for notes only (smaller exports)?
- One shared `VaultIndex` per request vs. a cached instance: providers should treat the
  index as read-only; the cache invalidation (mtime snapshot) already exists.
- Is a manifest worth it for a single-file tool, or is `"plugins": ["path/to/file.js"]` enough
  for v1? Start with the file list and add the manifest when a second capability (data provider) lands.
