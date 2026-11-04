/* Example plug-in for second-brain-viewer: "Overdue" board.
 *
 * Lists every open task whose Dataview `due` field is in the past, across the vault:
 *
 *     - [ ] Ship the report [state:: in progress] [due:: 2025-03-01]
 *
 * Install: copy this file into the vault (say .viewer/plugins/tasks-board.js) and enable it in viewer.json:
 *
 *     "plugins": [{"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js", "enabled": true}]
 *
 * It only uses the host API (v1), so it also works in a --export file:
 *   host.api(kind, arg)  host.openNote(path)  host.notes()  host.esc(s)
 */
(function () {
  "use strict";
  const DONE = new Set(["done", "waived", "missed"]);      // same "closed" states as the viewer's own overdue marker
  const SEARCH_LIMIT = 50;                                  // /api/search returns at most this many notes

  const today = () => {
    const d = new Date(), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  // Open tasks with a past due date in one note's Markdown (fenced code is ignored).
  function overdueTasks(markdown, now) {
    const out = [];
    let fence = null;
    for (const line of String(markdown || "").split(/\r?\n/)) {
      const f = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
      if (f) { fence = fence ? (f[1][0] === fence ? null : fence) : f[1][0]; continue; }
      if (fence) continue;
      const m = /^\s*[-*+]\s+\[ \]\s+(.*)$/.exec(line);       // open tasks only
      if (!m) continue;
      const due = /\[due::[ \t]*(\d{4}-\d{2}-\d{2})\s*\]/.exec(m[1]);
      if (!due || due[1] >= now) continue;
      const state = /\[state::[ \t]*([^\]]*?)\s*\]/.exec(m[1]);
      if (state && DONE.has(state[1].toLowerCase())) continue;
      out.push({
        due: due[1],
        state: state ? state[1] : "",
        text: m[1].replace(/\[[A-Za-z][\w -]*::[^\]]*\]/g, "").replace(/\s+/g, " ").trim(),
      });
    }
    return out;
  }

  async function collect(host, now) {
    // Narrow the vault down with the full-text search; if it was cut off at its limit, scan every note instead.
    let paths = (await host.api("search", "due::")).map(r => r.path);
    if (paths.length >= SEARCH_LIMIT) paths = host.notes().map(n => n.path);
    const rows = [];
    for (const path of paths) {
      const note = await host.api("note", path);
      for (const t of overdueTasks(note.content, now)) rows.push({ ...t, path, note: note.name });
    }
    rows.sort((a, b) => a.due.localeCompare(b.due) || a.note.localeCompare(b.note));
    return rows;
  }

  function render(host, el, rows, now) {
    const e = host.esc;
    el.innerHTML = rows.length
      ? `<p style="margin-bottom:12px;color:var(--tx2)">${rows.length} overdue (before ${e(now)})</p>` +
        `<table class="tasks-board" style="width:100%;border-collapse:collapse">` +
        rows.map(r => `<tr><td style="padding:6px 12px 6px 0;white-space:nowrap;color:var(--k-red)">${e(r.due)}</td>` +
          `<td style="padding:6px 12px 6px 0">${e(r.text)}` +
          (r.state ? ` <span style="color:var(--tx2)">· ${e(r.state)}</span>` : "") + `</td>` +
          `<td style="padding:6px 0"><a href="#" data-path="${e(r.path)}" style="color:var(--ac)">${e(r.note)}</a></td></tr>`).join("") +
        `</table>`
      : `<p style="color:var(--tx2)">No overdue tasks.</p>`;
  }

  let stopWatching = null;

  window.VaultViewer.register({
    id: "tasks-board",
    title: "Overdue",
    api: 1,
    async mount(el, host) {
      const now = today();
      el.innerHTML = `<p style="color:var(--tx2)">Loading…</p>`;
      el.onclick = ev => {                                   // assigned, not added: the overlay element is reused
        const a = ev.target.closest && ev.target.closest("a[data-path]");
        if (!a) return;
        ev.preventDefault();
        host.openNote(a.dataset.path);                       // closes this overlay and opens the note
      };
      const draw = async () => render(host, el, await collect(host, now), now);
      await draw();
      stopWatching = host.onVaultChange(() => { draw().catch(() => {}); });   // live reload
    },
    unmount() {
      if (stopWatching) { stopWatching(); stopWatching = null; }
    },
  });
})();
