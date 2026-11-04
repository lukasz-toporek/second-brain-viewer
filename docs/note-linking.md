# How to write notes so the graph works

The graph draws one dot per note and one line per **link that the viewer can resolve**. Two things decide whether it looks connected:
- the **link syntax**: only some forms create a line;
- the **vault structure**: every note needs a path to the rest.

Every rule below is checked against the viewer's one shared implementation (`VaultIndex` / `build_graph()` in `viewer.py`).
The graph, the backlinks, the page's link rendering, `--export` and `tools/vault-lint.py` all use it, so they always agree.

## 1. Link syntax

### Forms that create a line

| You write | Notes |
|---|---|
| `[[Note]]` | The basic form. Matching is case-insensitive, and spaces around the name are ignored (`[[ Note ]]`) |
| `[[Note\|shown text]]` | Alias text; still links to `Note` |
| `[[Note#Heading]]` | Links to the note; the heading doesn't matter for the graph |
| `[[folder/Note]]`, `[[Note.md]]` | Vault-relative path, with or without `.md`. Use this when two notes share a name |
| `![[Note]]` | An embed also counts as a link |
| `[[Note\|x]]` inside a table | The escaped `\|` is handled |
| Frontmatter: `related: "[[Note]]"` | **Quote it.** YAML would otherwise read `[[…]]` as a nested list |
| Frontmatter list: `related:` then `  - "[[Note]]"` lines | One line per item |
| `[text](Note.md)`, `[text](../dir/Note%20x.md#Heading)` | A Markdown link to a note. It is resolved against the **folder of the current note** (`.` and `..` handled, `%20` decoded, `#heading` / `?query` ignored, never above the vault root) and **by path only**: if that path does not exist the link is broken, even when a note with that name exists elsewhere. Images (`![](x.md)`), attachments, `http(s):` / `mailto:` links, `#fragment` links and links without `.md` do not count |
| Frontmatter `aliases: [Nick, "Full Name"]` (or a block list, or the singular `alias: Nick`) | `[[Nick]]` / `[[Full Name]]` then link to that note (see "Aliases" below) |
| A name in a different Unicode form | `[[Zażółć]]` finds a file saved as `Zażółć.md` whether it was composed (NFC) or decomposed (NFD: some sync tools and macOS apps do this); only the lookup is normalised, paths stay as they are on disk |

#### Aliases

- A real file name or path **always wins** over an alias: with a `Bob.md` and another note's `aliases: [Bob]`, `[[Bob]]` opens `Bob.md`.
- If two notes claim the same alias, the usual tie-breaking applies: the first in vault order, except that a note outside the
  `link_deprioritise` folders beats one inside them. The lint warns about both kinds of clash.
- Aliases are matched like names (case-insensitive, NFC). Only frontmatter counts; an `aliases:` line in the body is plain text.
  A scalar is one alias (`aliases: Smith, John` is the single alias "Smith, John"): use a list for several.

### Forms that do NOT create a line

| You write | What happens | Use instead |
|---|---|---|
| Frontmatter `person: Note` (no brackets) | Plain text, no link | `person: "[[Note]]"` |
| `[[My%20Note]]` | URL-encoding isn't decoded in wikilinks (it is in Markdown links) | `[[My Note]]` |
| `#tag` | Tags aren't links | A hub note (section 2) |
| `[[folder]]` | Folder names aren't notes | Link to the folder's hub note |
| `[[README]]`, `[[_index]]` | Files in `skip_files` (and folders in `skip_dirs`) aren't in the graph | Give the content a normal note name |
| A link to a note that doesn't exist yet | Shown as a missing link, with no line | Create the note, even as a stub |
| A link inside code | Ignored: `` `[[Note]]` ``, fenced blocks (```` ``` ```` and `~~~`, also an unclosed one up to the end of the note) | Write the link outside the code |
| `[text](Note)` without `.md`, `[text](http://…/Note.md)` | Not a note link | `[text](Note.md)` or `[[Note\|text]]` |

### Watch out for

- **Templates count.** A `[[Person]]` placeholder in `Templates/` links the template to a real note if one is called `Person`. Put templates in a folder listed in `skip_dirs` to keep them out of the graph. Alternatively, list it in `link_deprioritise`, which only matters when two notes share a name.
- **Duplicate names.** `[[Weekly]]` links to only one of several `Weekly.md` files: the first in vault order, or the one outside `link_deprioritise`. Use a path link (`[[projects/Weekly]]`) or make the names unique.
- **Indented code.** A code block made by indenting four spaces is *not* recognised (it can't be told from a list continuation without a full parser), so a link inside one still counts. Use a fenced block.
- **Markdown links and renames.** A `[text](../a/Note.md)` link breaks when you move the file; `[[Note]]` does not. Prefer wikilinks in notes that move.

## 2. Vault structure: one connected graph

The force layout pushes **separate groups** of notes apart. If the vault is several islands, the graph looks like scattered clusters around an empty middle. The fix is structural: make sure every note can reach every other note through links.

### The rule: every note links "up" to exactly one hub

```
Home
 ├── [[People]]        (hub: one line per person)
 │     ├── [[Person A]] ──┬── [[2026-10-01 1:1 Person A]]
 │     │                  └── [[Training plan Person A]]
 │     └── [[Person B]] …
 ├── [[Projects]]      (hub)
 │     └── [[Project X]] ── [[Person A]]   (cross-link)
 └── [[Meetings]]      (hub, or the daily notes)
```

1. **One `Home` note** links to every hub.
2. **One hub note per folder or topic**, for example `People.md`, `Projects.md`, `Meetings.md`. Give it `type: index`, and have it list its notes as `[[…]]` lines. A hub with an embedded query doesn't count; it needs real links.
3. **Every note has an `up` link** in its frontmatter to its hub or parent:
   ```yaml
   ---
   type: person
   up: "[[People]]"
   ---
   ```
   For notes that belong to a person or project (1:1s, reviews, trainings), `up` points to that person or project, not to the folder hub:
   ```yaml
   ---
   type: meeting
   date: 2026-10-01
   up: "[[Person A]]"
   related:
     - "[[Project X]]"
   ---
   ```
4. **Cross-links** (`related:` or links in the body) join the clusters to each other: person ↔ project, meeting ↔ topic. They're optional, but they make the graph useful.
5. **Daily notes** link to what they're about (`[[Project X]]`, `[[Person A]]`), or to a `Daily` hub, so they don't float alone.
6. **New notes start from a template** that already has `up: "[[…]]"`. The empty value then reminds you to fill it in.

### Note checklist

- [ ] The file name is the name you'll link with: no `%20`, no duplicates, typed normally.
- [ ] The frontmatter has `up: "[[…]]"`, quoted.
- [ ] Every `[[…]]` target exists. Create a stub if needed.
- [ ] The note isn't in a skipped folder or file name unless it should be hidden.

## 3. Checking a vault

`tools/vault-lint.py` uses the viewer's own resolver, so what it calls a broken link is what the graph shows as missing.
Run it from the viewer's repo folder (or give it `--vault PATH`):

```bash
python3 tools/vault-lint.py --vault /path/to/vault           # frontmatter, broken wikilinks, alias clashes, opt-in checks
python3 tools/vault-lint.py --vault /path/to/vault --groups  # only the link-group report
```

Two optional structure checks live in the `"lint"` key of the vault's `viewer.json` (both default to off, so existing runs don't change):

```json
"lint": {
  "require_up": true,
  "report_groups": true
}
```

- **`"require_up": true`**: every note except `type: home` must have an `up:` value that resolves to a note. A missing or empty
  `up:` and a broken one (nothing it names exists) are reported **separately**, and both make the exit code non-zero. A list is
  fine if at least one item resolves; a bare name (`up: People`) works as well as `up: "[[People]]"`.
- **`"report_groups": true`**: after the checks, how many separate groups the link graph has (connected components, ignoring
  link direction), and the notes that are not in the largest group, up to 20 per group and then a count. This is informational:
  it never changes the exit code.
- **`--groups`**: prints only that report (whatever the config says), exit code 0.

```
Grupy powiązań: 3 (notatek: 12, linków: 11)
  największa grupa: 8 notatek
  poza największą grupą — grupa 2 (3): island/I1.md, island/I2.md, island/I3.md
  poza największą grupą — grupa 3 (1): Lone.md
```

The goal is **one group** and no broken links; each listed note needs an `up` link or a link from the main group.
The report prints note paths, so for a vault with personal data, run it only on your own machine.

Instead of `require_up` you can still require any other field per type with
`"lint": {"required_fields": {"<type>": ["up", …]}}`.

## 4. Not handled (yet)

- Indented code blocks are not blanked before link extraction (see "Watch out for").
- Wikilinks are not URL-decoded (`[[My%20Note]]`).
- `aliases` given as one comma-separated string is a single alias.
- Tags and folder names are not links.
