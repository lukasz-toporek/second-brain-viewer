"""Tests for viewer.py (stdlib unittest). Run: python3 -m unittest discover tests

Paths rule: the viewer works with RESOLVED paths (VAULT, CONFIG_PATH, PLUGINS, ... are
all .resolve()d, the symlink safety checks depend on it). On macOS the temp folder is
reached through a symlink (/var -> /private/var), so a test must compare resolved
paths: `self.root.resolve() / "x"`, never `self.root / "x"`, against a viewer path.
Simulate that on any OS:
    B=$(mktemp -d); mkdir $B/real; ln -s $B/real $B/link
    TMPDIR=$B/link python3 -m unittest discover tests
"""
import base64
import contextlib
import io
import hashlib
import http.client
import importlib.util
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import viewer  # noqa: E402

VAULT = ROOT / "tests" / "fixtures" / "vault"


NODE = shutil.which("node")


def page_script(mode="offline") -> str:
    return re.findall(r"<script>(.*?)</script>", viewer.render_page(mode), re.S)[-1]


def run_page_js(snippet: str, mode="offline", export_data=None):
    """Run the page's note-resolution + Markdown-extension code (real vendored marked) under node.

    The slice of the page script from safeUrl() to the wikilink click handler touches no DOM except
    document.addEventListener, which is stubbed. `snippet` runs after it and prints JSON.
    """
    js = page_script(mode)
    a = js.index("const FM = ")
    b = js.index("// delegated click handler for wikilinks")
    prelude = (
        f"global.window = {{__EXPORT__: {json.dumps(export_data)}}}; global.document = {{addEventListener(){{}}}};\n"
        f"global.marked = require({json.dumps(str(ROOT / 'vendor' / 'marked.min.js'))}).marked;\n"
        "const FLAGS = {};\n")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(prelude + js[a:b] + "\n" + snippet, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


class Server:
    def __init__(self, offline=False, vault=VAULT):
        viewer.init_config(vault)
        viewer.OFFLINE = offline
        self.srv = viewer.make_server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, headers=None, body=None, host=True, declared_length=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        c.putrequest(method, path, skip_host=True)
        if host is True:
            c.putheader("Host", f"localhost:{self.port}")
        elif host:
            c.putheader("Host", host)
        for k, v in h.items():
            c.putheader(k, v)
        if declared_length is not None:
            c.putheader("Content-Length", str(declared_length))
        elif body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        viewer.OFFLINE = False


class OnlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def note(self, rel):
        return self.s.req("GET", "/api/note?path=" + rel)

    def test_valid_note(self):
        st, _, body = self.note("10-knowledge/Alpha.md")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["name"], "Alpha")

    def test_path_traversal_rejected(self):
        cases = [
            "../vault-secret/secret.md",                       # sibling with similar name
            "10-knowledge/../../vault-secret/secret.md",
            "..%2Fvault-secret%2Fsecret.md",
            str(VAULT.parent / "vault-secret" / "secret.md"),  # absolute path
            "/etc/passwd",
            "notes.txt",                                       # not .md
            "README.md",                                       # skipped by name
            ".obsidian/workspace.md",                          # skipped dir
            "",
        ]
        for rel in cases:
            with self.subTest(rel=rel):
                st, _, body = self.note(rel)
                self.assertIn(st, (403, 404))
                self.assertNotIn(b"SECRET", body)

    def test_wrong_host_rejected(self):
        for host in ("evil.example:%d" % self.s.port, "localhost", "localhost:1", None):
            with self.subTest(host=host):
                st, _, _ = self.s.req("GET", "/api/vault", host=host)
                self.assertEqual(st, 403)
        st, _, _ = self.s.req("GET", "/api/vault", host=f"127.0.0.1:{self.s.port}")
        self.assertEqual(st, 200)

    def test_chat_protection(self):
        ok_h = {"Content-Type": "application/json", "X-Vault-Viewer": "1"}
        body = b'{"messages": 5}'
        st, _, _ = self.s.req("POST", "/api/chat", {"Content-Type": "application/json"}, body)
        self.assertEqual(st, 403)                              # no custom header
        st, _, _ = self.s.req("POST", "/api/chat", {"Content-Type": "text/plain", "X-Vault-Viewer": "1"}, body)
        self.assertEqual(st, 415)                              # wrong content type
        # Declare an oversized body but send none: the server answers 413 without reading it, so
        # this cannot hit BrokenPipe on a client that is still writing (macOS socket buffers).
        st, h, _ = self.s.req("POST", "/api/chat", ok_h, None, declared_length=viewer.MAX_BODY + 1)
        self.assertEqual(st, 413)                              # too big
        self.assertEqual(h.get("Connection"), "close")
        st, _, _ = self.s.req("POST", "/api/chat", ok_h, body, host="evil.example")
        self.assertEqual(st, 403)                              # bad host
        st, _, _ = self.s.req("POST", "/api/chat", ok_h, body)
        self.assertEqual(st, 400)                              # passes guards, bad payload

    def test_backlinks(self):
        _, _, b = self.note("10-knowledge/Beta.md")
        names = {x["name"] for x in json.loads(b)["backlinks"]}
        self.assertEqual(names, {"Alpha", "Gamma", "Home"})   # Gamma via path form too
        _, _, b = self.note("20-projects/Tasks.md")
        names = {x["name"] for x in json.loads(b)["backlinks"]}
        self.assertEqual(names, {"Gamma", "Home"})            # Gamma: frontmatter-only link
        _, _, b = self.note("Home.md")
        self.assertEqual([x["name"] for x in json.loads(b)["backlinks"]], ["Beta"])

    def test_graph_and_vendor_not_served_online(self):
        st, _, b = self.s.req("GET", "/api/graph")
        self.assertEqual(st, 200)
        self.assertEqual(len(json.loads(b)["nodes"]), 9)
        st, _, _ = self.s.req("GET", "/vendor/marked.min.js")
        self.assertEqual(st, 404)

    def test_page_online_has_chat_and_cdn(self):
        _, h, b = self.s.req("GET", "/")
        self.assertEqual(h["Content-Security-Policy"], viewer.CSP_ONLINE)
        self.assertIn(b"cdn.jsdelivr.net", b)
        self.assertNotIn(b"cdnjs.cloudflare.com", b)
        self.assertIn(b'"chat": true', b)

    def test_online_csp_value(self):
        csp = viewer.CSP_ONLINE
        self.assertEqual(csp, "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                              "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'")
        for path in ("/api/vault", "/api/graph"):
            self.assertEqual(self.s.req("GET", path)[1]["Content-Security-Policy"], csp)

    def test_online_libs_have_sri_matching_vendor_files(self):
        html = self.s.req("GET", "/")[2].decode()
        tags = re.findall(r'<script src="(https://cdn[^"]+)"([^>]*)></script>', html)
        self.assertEqual(len(tags), 2)
        by_url = {u: a for u, a in tags}
        for name, url in viewer.CDN_URLS.items():
            digest = base64.b64encode(hashlib.sha384((ROOT / "vendor" / name).read_bytes()).digest()).decode()
            self.assertIn(f'integrity="sha384-{digest}"', by_url[url], name)
            self.assertIn('crossorigin="anonymous"', by_url[url], name)

    def test_cdn_urls_match_fetch_vendor(self):
        spec = importlib.util.spec_from_file_location("fetch_vendor", ROOT / "tools" / "fetch-vendor.py")
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        self.assertEqual({k: v["url"] for k, v in fv.LIBS.items()}, viewer.CDN_URLS)

    def test_title_is_escaped(self):
        self.assertNotIn("<img", viewer.render_page("online").split("<body>")[1].split("<script")[0])


class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server(offline=True)

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_chat_and_config_forbidden(self):
        self.assertEqual(self.s.req("GET", "/api/config")[0], 403)
        h = {"Content-Type": "application/json", "X-Vault-Viewer": "1"}
        self.assertEqual(self.s.req("POST", "/api/chat", h, b"{}")[0], 403)

    def test_csp_and_no_external(self):
        st, h, b = self.s.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Security-Policy"], viewer.CSP_OFFLINE)
        self.assertIn("connect-src 'self'", h["Content-Security-Policy"])
        self.assertNotRegex(b.decode(), r'(src|href)="https?://')
        self.assertIn(b'"chat": false', b)
        self.assertIn("Content-Security-Policy", self.s.req("GET", "/api/vault")[1])
        self.assertEqual(
            viewer.CSP_OFFLINE,
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'")

    def test_vendor_allow_list(self):
        for name in viewer.VENDOR_FILES:
            st, h, body = self.s.req("GET", "/vendor/" + name)
            self.assertEqual(st, 200)
            self.assertIn("javascript", h["Content-Type"])
            self.assertGreater(len(body), 1000)
        for bad in ("/vendor/../viewer.py", "/vendor/README.md", "/vendor/%2e%2e/viewer.py", "/vendor/"):
            self.assertEqual(self.s.req("GET", bad)[0], 404, bad)


def make_search_vault(root: Path) -> None:
    (root / "Needle in name.md").write_text("# x\nnothing relevant here\n", encoding="utf-8")
    (root / "Zażółć gęślą.md").write_text("body about ZAŻÓŁĆ\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "Body only.md").write_text(
        "---\ntype: x\n---\n" + "pad " * 30 + "\nthe   NEEDLE\nis\there " + "tail " * 40 + "\n", encoding="utf-8")
    (root / "sub" / "Regex.md").write_text("price is a.b (1+1) [x] \\ done\n", encoding="utf-8")
    for i in range(60):                                     # many body-only matches for the limit
        (root / f"bulk{i:02d}.md").write_text(f"filler {i} needle\n", encoding="utf-8")


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make_search_vault(Path(cls.tmp.name))
        cls.s = Server(vault=Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.tmp.cleanup()

    def search(self, q):
        from urllib.parse import quote
        st, h, b = self.s.req("GET", "/api/search?q=" + quote(q))
        self.assertEqual(st, 200)
        self.assertIn("json", h["Content-Type"])
        return json.loads(b)

    def test_name_matches_first_then_body_and_limit(self):
        r = self.search("needle")
        self.assertEqual(len(r), 50)                                    # capped
        self.assertEqual(r[0]["name"], "Needle in name")                # name match first...
        self.assertEqual(r[0]["path"], "Needle in name.md")
        self.assertEqual(r[0]["folder"], "(vault root)")
        self.assertEqual(r[0]["snippet"], "")                           # ...no body hit -> no snippet
        self.assertEqual([x["name"] for x in r[1:3]], ["bulk00", "bulk01"])   # then body, vault order
        self.assertEqual(len({x["path"] for x in r}), 50)

    def test_snippet_is_collapsed_window_around_first_body_match(self):
        r = self.search("THE   needle\nis")[0]                         # raw (not collapsed) text matches
        self.assertEqual(r["name"], "Body only")
        self.assertEqual(r["folder"], "sub")
        self.assertIn("the NEEDLE is here", r["snippet"])               # whitespace collapsed, case kept
        self.assertTrue(r["snippet"].startswith("…") and r["snippet"].endswith("…"))
        self.assertLessEqual(len(r["snippet"]), 122)
        self.assertNotIn("\n", r["snippet"])

    def test_case_insensitive_unicode_and_regex_chars_are_literal(self):
        self.assertEqual([x["name"] for x in self.search("zażółć")][:1], ["Zażółć gęślą"])
        self.assertEqual([x["name"] for x in self.search("a.b (1+1) [x] \\")], ["Regex"])
        self.assertEqual(self.search("axb"), [])                        # '.' is not a wildcard

    def test_short_or_blank_queries_return_empty(self):
        for q in ("", " ", "n", " n ", "%20%20"):
            self.assertEqual(self.search(q), [], repr(q))
        self.assertEqual(self.s.req("GET", "/api/search")[0], 200)      # no q at all
        self.assertEqual(len(self.search("ne")), 50)                    # 2 chars is enough

    def test_host_guard(self):
        for host in ("evil.example:%d" % self.s.port, "localhost", None):
            self.assertEqual(self.s.req("GET", "/api/search?q=needle", host=host)[0], 403)


@unittest.skipUnless(NODE, "node not available")
class SearchParityTests(unittest.TestCase):
    """The export's client-side searchNotes() must return exactly what /api/search returns."""

    def test_js_matches_python(self):
        with tempfile.TemporaryDirectory() as d:
            make_search_vault(Path(d))
            viewer.init_config(Path(d))
            idx = viewer.VaultIndex()
        notes = [{"path": n["path"], "name": n["name"], "content": n["text"]} for n in idx.notes]
        queries = ["needle", "NEEDLE", "zażółć", "a.b (1+1) [x] \\", "axb", "n", "", " ", "ne", "tail", "pad", "the   needle",
                   "😀"]
        js = run_page_js(f"console.log(JSON.stringify({json.dumps(queries)}.map(q => searchNotes(q, {json.dumps(notes)}))));")
        for q, got in zip(queries, js):
            self.assertEqual(got, viewer.search_notes(q, idx.notes), repr(q))
        viewer.init_config(VAULT)

    def test_fixture_vault(self):
        viewer.init_config(VAULT)
        idx = viewer.VaultIndex()
        notes = [{"path": n["path"], "name": n["name"], "content": n["text"]} for n in idx.notes]
        queries = ["alpha", "beta", "link", "[[", "overdue", "Target Heading"]
        js = run_page_js(f"console.log(JSON.stringify({json.dumps(queries)}.map(q => searchNotes(q, {json.dumps(notes)}))));")
        for q, got in zip(queries, js):
            self.assertEqual(got, viewer.search_notes(q, idx.notes), repr(q))
            self.assertTrue(got, q)


def make_clash_vault(root: Path, deprioritise=None) -> None:
    """`Weekly` exists in Archive/ (sorts first) and Journal/; Linker.md links [[Weekly]] and [[archive/weekly]]."""
    (root / "Archive").mkdir()
    (root / "Journal").mkdir()
    (root / "Archive" / "Weekly.md").write_text("# old\n", encoding="utf-8")
    (root / "Journal" / "Weekly.md").write_text("# real\n", encoding="utf-8")
    (root / "Linker.md").write_text("see [[Weekly]] and [[archive/weekly]]\n", encoding="utf-8")
    if deprioritise is not None:
        (root / ".viewer.json").write_text(json.dumps({"link_deprioritise": deprioritise}), encoding="utf-8")


class LinkDeprioritiseTests(unittest.TestCase):
    def snapshot(self, deprioritise):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_clash_vault(root, deprioritise)
            s = Server(vault=root)
            try:
                vault = json.loads(s.req("GET", "/api/vault")[2])
                graph = json.loads(s.req("GET", "/api/graph")[2])
                back = {p: [b["path"] for b in json.loads(s.req("GET", "/api/note?path=" + p)[2])["backlinks"]]
                        for p in ("Journal/Weekly.md", "Archive/Weekly.md")}
            finally:
                s.stop()
        edges = sorted((e["source"], e["target"]) for e in graph["edges"])
        return vault, edges, back

    def test_default_keeps_current_order(self):
        vault, edges, back = self.snapshot(None)
        self.assertEqual(vault["stems"]["weekly"], "Archive/Weekly.md")        # first in vault order
        self.assertEqual(edges, [("Linker.md", "Archive/Weekly.md")])         # [[Weekly]] and the explicit path
        self.assertEqual(back, {"Journal/Weekly.md": [], "Archive/Weekly.md": ["Linker.md"]})

    def test_empty_list_is_default(self):
        self.assertEqual(self.snapshot([])[1:], self.snapshot(None)[1:])

    def test_deprioritised_folder_loses_name_clash(self):
        vault, edges, back = self.snapshot(["Archive"])
        self.assertEqual(vault["stems"]["weekly"], "Journal/Weekly.md")
        self.assertEqual(sorted(vault["stems"]), ["linker", "weekly"])
        self.assertEqual(edges, [("Linker.md", "Archive/Weekly.md"), ("Linker.md", "Journal/Weekly.md")])
        # the explicit path form still reaches the deprioritised note
        self.assertEqual(back, {"Journal/Weekly.md": ["Linker.md"], "Archive/Weekly.md": ["Linker.md"]})

    def test_prefix_matching(self):
        for prefix in ("archive", "Archive/", "/Archive"):                  # case/slashes forgiven
            self.assertEqual(self.snapshot([prefix])[0]["stems"]["weekly"], "Journal/Weekly.md", prefix)
        self.assertEqual(self.snapshot(["Arch"])[0]["stems"]["weekly"], "Archive/Weekly.md")  # whole folder names only

    def test_all_clashing_notes_deprioritised_keeps_order(self):
        self.assertEqual(self.snapshot(["Archive", ""])[0]["stems"]["weekly"], "Journal/Weekly.md")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for f in ("A", "B"):
                (root / f).mkdir()
                (root / f / "X.md").write_text("x", encoding="utf-8")
            (root / ".viewer.json").write_text('{"link_deprioritise": ["A", "B"]}', encoding="utf-8")
            viewer.init_config(root)
            self.assertEqual(viewer.VaultIndex().by_stem["x"], "A/X.md")
        viewer.init_config(VAULT)

    @unittest.skipUnless(NODE, "node not available")
    def test_frontend_uses_server_stems(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_clash_vault(root, ["Archive"])
            viewer.init_config(root)
            payload = json.dumps(viewer.vault_payload())
        viewer.init_config(VAULT)
        r = run_page_js(
            f"vault = {payload};"
            "const g = t => { const n = resolveNote(t); return n && n.path; };"
            "console.log(JSON.stringify([g('Weekly'), g('weekly'), g('Archive/Weekly'), g('archive/weekly.md'),"
            " g('toString'), g('__proto__'), g('constructor'), g('nope')]));")
        self.assertEqual(r, ["Journal/Weekly.md", "Journal/Weekly.md", "Archive/Weekly.md", "Archive/Weekly.md",
                             None, None, None, None])

    def test_export_contains_stems(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            root.mkdir()
            make_clash_vault(root, ["Archive"])
            out = Path(d) / "out.html"
            subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(root), "--export", str(out)],
                           check=True, capture_output=True)
            data = re.search(r"window\.__EXPORT__ = (.*?);</script>", out.read_text(encoding="utf-8"), re.S).group(1)
        self.assertEqual(json.loads(data.replace("<\\/", "</"))["vault"]["stems"]["weekly"], "Journal/Weekly.md")


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
SVG_DOC = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PDF_DOC = b"%PDF-1.4\n% fake test document\n"


def make_attach_vault(root: Path, config=None):
    """Fake vault with attachments, a name clash (Archive/pic.png vs img/pic.png) and hidden files."""
    (root / "img").mkdir(parents=True)
    (root / "files").mkdir()
    (root / "Archive").mkdir()
    (root / "sub").mkdir()
    (root / ".obsidian").mkdir()
    (root / "img" / "pic.png").write_bytes(PNG_1X1)
    (root / "img" / "vec.svg").write_bytes(SVG_DOC)
    (root / "files" / "spec.pdf").write_bytes(PDF_DOC)
    (root / "Archive" / "pic.png").write_bytes(PNG_1X1 + b"\0")
    (root / ".obsidian" / "hidden.png").write_bytes(PNG_1X1)
    (root / "Note.md").write_text("# Note\n\n![[pic.png]]\n", encoding="utf-8")
    (root / "sub" / "Deep.md").write_text("# Deep\n\n![](../img/pic.png)\n", encoding="utf-8")
    (root / "secret.py").write_text("print('x')\n", encoding="utf-8")
    sibling = root.parent / (root.name + "-secret")
    sibling.mkdir(exist_ok=True)
    (sibling / "s.png").write_bytes(PNG_1X1)
    if config is not None:
        (root / ".viewer.json").write_text(json.dumps(config), encoding="utf-8")


class AttachmentServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault"
        self.root.mkdir()
        make_attach_vault(self.root)
        self.s = Server(vault=self.root)

    def tearDown(self):
        self.s.stop()
        viewer.init_config(VAULT)
        self._tmp.cleanup()

    def get(self, rel):
        return self.s.req("GET", "/api/file?path=" + rel)

    def test_png_is_served_with_fixed_type_and_nosniff(self):
        st, h, body = self.get("img/pic.png")
        self.assertEqual(st, 200)
        self.assertEqual(body, PNG_1X1)
        self.assertEqual(h["Content-Type"], "image/png")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_svg_is_sandboxed(self):
        st, h, body = self.get("img/vec.svg")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "image/svg+xml")
        self.assertEqual(h["Content-Security-Policy"], "sandbox; default-src 'none'; style-src 'unsafe-inline'")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_pdf_is_served(self):
        st, h, body = self.get("files/spec.pdf")
        self.assertEqual((st, h["Content-Type"], body), (200, "application/pdf", PDF_DOC))

    def test_rejections(self):
        sibling = self.root.parent / (self.root.name + "-secret") / "s.png"
        cases = [
            "Note.md", "secret.py", "", "img",                     # wrong type / empty / a directory
            "../vault-secret/s.png", "img/../../vault-secret/s.png",
            "..%2Fvault-secret%2Fs.png", str(sibling), "/etc/passwd",
            ".obsidian/hidden.png",                                 # skipped dir
            "img/pic.png%00.md", "img/missing.png",
        ]
        for rel in cases:
            with self.subTest(rel=rel):
                st, _, body = self.get(rel)
                self.assertIn(st, (403, 404))
                self.assertNotEqual(body, PNG_1X1)

    def test_symlink_out_of_vault_is_rejected_and_not_indexed(self):
        outside = self.root.parent / (self.root.name + "-secret") / "s.png"
        try:
            (self.root / "img" / "link.png").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not supported")
        self.assertIn(self.get("img/link.png")[0], (403, 404))
        self.assertNotIn("img/link.png", viewer.VaultIndex().file_paths)

    def test_size_limit(self):
        old = viewer.ATTACH_MAX_BYTES
        viewer.ATTACH_MAX_BYTES = 10
        try:
            self.assertEqual(self.get("img/pic.png")[0], 413)
        finally:
            viewer.ATTACH_MAX_BYTES = old
        self.assertEqual(self.get("img/pic.png")[0], 200)

    def test_wrong_host_rejected(self):
        self.assertEqual(self.s.req("GET", "/api/file?path=img/pic.png", host="evil.example")[0], 403)

    def test_vault_payload_lists_attachments(self):
        v = json.loads(self.s.req("GET", "/api/vault")[2])
        self.assertEqual(v["files"]["pic.png"], "Archive/pic.png")          # first in vault order
        self.assertEqual(v["files"]["spec.pdf"], "files/spec.pdf")
        self.assertEqual(v["file_paths"], ["Archive/pic.png", "files/spec.pdf", "img/pic.png", "img/vec.svg"])
        self.assertNotIn("hidden.png", v["files"])                          # skipped dir
        self.assertNotIn("secret.py", v["files"])                           # not an attachment type

    def test_attachment_change_invalidates_index(self):
        self.assertNotIn("new.png", json.loads(self.s.req("GET", "/api/vault")[2])["files"])
        (self.root / "img" / "new.png").write_bytes(PNG_1X1)
        self.assertEqual(json.loads(self.s.req("GET", "/api/vault")[2])["files"]["new.png"], "img/new.png")


class AttachmentIndexTests(unittest.TestCase):
    def test_link_deprioritise_applies_to_attachments(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "vault"
            root.mkdir()
            make_attach_vault(root, {"link_deprioritise": ["Archive"]})
            viewer.init_config(root)
            try:
                self.assertEqual(viewer.VaultIndex().files["pic.png"], "img/pic.png")
            finally:
                viewer.init_config(VAULT)


@unittest.skipUnless(NODE, "node not available")
class AttachmentRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "vault"
        cls.root.mkdir()
        make_attach_vault(cls.root, {"link_deprioritise": ["Archive"]})
        viewer.init_config(cls.root)
        cls.vault = json.dumps(viewer.vault_payload())
        cls.idx = viewer.get_index()
        viewer.init_config(VAULT)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def render(self, base, md, export=None):
        return run_page_js(f"vault = {self.vault}; currentNotePath = {json.dumps(base)};"
                           f"console.log(JSON.stringify(marked.parse({json.dumps(md)})));", export_data=export)

    def test_images_resolve_relative_and_by_name(self):
        html = self.render("sub/Deep.md", "![alt text](../img/pic.png) ![[pic.png]] ![[img/vec.svg|vector]] ![[pic.png|120]]")
        self.assertEqual(html.count('src="/api/file?path=img%2Fpic.png"'), 3)     # clash -> img/ wins
        self.assertIn('alt="alt text"', html)
        self.assertIn('src="/api/file?path=img%2Fvec.svg" alt="vector"', html)
        self.assertIn('width="120"', html)
        self.assertNotIn("<script", html)

    def fixture_line(self):
        text = (VAULT / "README.md").read_text(encoding="utf-8")     # a skipped file: no effect on the index or lint
        return next(l for l in text.splitlines() if l.startswith("- attachment wikilinks"))

    def test_wikilink_to_attachment_is_a_link_only_embed_is_an_image(self):
        att = 'href="/api/file?path=img%2Fpic.png" target="_blank" rel="noopener noreferrer"'
        html = self.render("Note.md", "[[pic.png]]")
        self.assertIn(f'<a class="wl att" {att}>pic.png</a>', html)
        self.assertNotIn("<img", html)
        html = self.render("Note.md", "[[pic.png|the label]]")
        self.assertIn(f'<a class="wl att" {att}>the label</a>', html)
        self.assertNotIn("<img", html)
        html = self.render("Note.md", "![[pic.png]]")
        self.assertIn('<img src="/api/file?path=img%2Fpic.png"', html)
        self.assertNotIn("<a ", html)
        html = self.render("Note.md", "[[spec.pdf]] ![[spec.pdf]]")
        self.assertIn('rel="noopener noreferrer">spec.pdf</a>', html)             # plain link
        self.assertIn('rel="noopener noreferrer">📄 spec.pdf</a>', html)           # embed keeps the icon

    def test_fixture_line_renders_one_image_and_two_links(self):
        html = self.render("Note.md", self.fixture_line())      # doc.pdf is not in this vault: placeholder
        self.assertEqual(html.count("<img"), 1)
        self.assertEqual(html.count('class="wl att"'), 2)
        self.assertEqual(html.count('class="wl missing"'), 1)

    def test_wikilink_to_attachment_in_export(self):
        att = self.export_attachments()
        html = self.render("Note.md", "[[pic.png]] [[spec.pdf]]", export={"attachments": att})
        self.assertIn('href="data:image/png;base64,', html)
        self.assertIn("spec.pdf (attachment not included in export)", html)
        self.assertNotIn("<img", html)

    def test_page_script_is_valid_and_image_branch_is_guarded(self):
        for mode in ("online", "offline", "export"):
            self.assertRegex(page_script(mode), r"if \(tok\.embed && ATT_EXT\.test\(tok\.target\)\)")
        if NODE:
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "page.js"
                f.write_text(page_script("online"), encoding="utf-8")
                r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_pdf_becomes_link_in_new_tab(self):
        html = self.render("Note.md", "[spec](files/spec.pdf) ![[spec.pdf]]")
        self.assertEqual(html.count('href="/api/file?path=files%2Fspec.pdf" target="_blank" rel="noopener noreferrer"'), 2)

    def test_unresolved_attachment_is_a_placeholder(self):
        html = self.render("Note.md", "![](nope.png) ![[<b>x</b>.png]] ![](../../../etc/y.png)")
        self.assertEqual(html.count('class="wl missing"'), 3)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)

    def test_other_links_unchanged(self):
        html = self.render("Note.md", "![](https://example.com/a.png) [x](unknown.txt) [y](other.pdf)")
        self.assertIn('<img src="https://example.com/a.png"', html)
        self.assertIn('href="unknown.txt"', html)
        self.assertIn('href="other.pdf"', html)                                   # not a vault file: untouched

    def export_attachments(self):
        viewer.init_config(self.root)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return viewer.export_attachments(self.idx)
        finally:
            viewer.init_config(VAULT)

    def test_export_inlines_images_and_placeholders_the_rest(self):
        att = self.export_attachments()
        self.assertEqual(sorted(att), ["Archive/pic.png", "img/pic.png", "img/vec.svg"])   # PDF left out
        self.assertTrue(att["img/pic.png"].startswith("data:image/png;base64,"))
        html = self.render("Note.md", "![[pic.png]] [spec](files/spec.pdf) ![[spec.pdf]]", export={"attachments": att})
        self.assertIn('<img src="data:image/png;base64,', html)
        self.assertEqual(html.count("attachment not included in export"), 4)        # title + text, twice
        self.assertNotIn("/api/file", html)

    def test_export_limits(self):
        old = (viewer.EXPORT_FILE_MAX, viewer.EXPORT_TOTAL_MAX)
        try:
            viewer.EXPORT_FILE_MAX = len(PNG_1X1)                    # Archive/pic.png is 1 byte bigger
            self.assertEqual(sorted(self.export_attachments()), ["img/pic.png"])   # the others are bigger
            viewer.EXPORT_FILE_MAX = 2 * 1024 * 1024
            viewer.EXPORT_TOTAL_MAX = len(PNG_1X1) * 2           # Archive/pic.png (+1 byte) fits, then nothing else does
            self.assertEqual(sorted(self.export_attachments()), ["Archive/pic.png"])
        finally:
            viewer.EXPORT_FILE_MAX, viewer.EXPORT_TOTAL_MAX = old


class AttachmentExportCliTests(unittest.TestCase):
    def test_export_file_inlines_and_prints_summary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "vault"
            root.mkdir()
            make_attach_vault(root)
            out = Path(d) / "out.html"
            r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(root), "--export", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertIn("Attachments: 3 included", r.stdout)
        self.assertIn("1 not included", r.stdout)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("data:application/pdf", html)
        self.assertNotRegex(html, r'<script[^>]+src=')


FAKE_DOM = r"""
class El {
  constructor() { this.dataset = {}; this._html = ""; this.kids = []; this.body = null; }
  set innerHTML(h) {
    this._html = h; this.kids = [];
    const re = /<span class="embed" data-embed-path="([^"]*)" data-embed-heading="([^"]*)">/g; let m;
    while ((m = re.exec(h))) { const e = new El(); e.dataset.embedPath = m[1]; e.dataset.embedHeading = m[2]; e.body = new El(); this.kids.push(e); }
  }
  get innerHTML() { return this._html; }
  querySelectorAll() { return this.kids; }
  querySelector() { return this.body; }
}
"""


@unittest.skipUnless(NODE, "node not available")
class EmbedTests(unittest.TestCase):
    NOTES = {
        "A.md": "---\ntype: x\n---\n# A\n\nA body\n\n![[B]]\n\n![[A]]\n",
        "B.md": "# B\n\nB body\n\n## Part\n\nB part text\n\n### Sub\n\nB sub text\n\n## Other\n\nB other\n\n![[A]]\n\n![[C]]\n",
        "C.md": "# C\n\nC body ![[D]]\n",
        "D.md": "# D\n\nD body\n",
        "S.md": "# S\n\n![[S]]\n",
        "sub/E.md": "---\ntitle: skip me\n---\n# E\n\n![](pic.png)\n",
        "H.md": "![[B#Part]] and ![[B#nope]] and ![[Zzz]] and ![[sub/E|label]]\n",
    }

    def run_js(self, snippet):
        notes = {p: {"path": p, "name": p.split("/")[-1][:-3], "content": c, "backlinks": []}
                 for p, c in self.NOTES.items()}
        vault = {"folders": [{"folder": "x", "notes": [{"path": p, "name": n["name"], "type": ""} for p, n in notes.items()]}],
                 "stems": {n["name"].lower(): p for p, n in notes.items()},
                 "files": {"pic.png": "sub/pic.png"}, "file_paths": ["sub/pic.png"]}
        return run_page_js(f"vault = {json.dumps(vault)};\n{FAKE_DOM}\n{snippet}", export_data={"notes": notes, "attachments": {"sub/pic.png": "data:image/png;base64,AAAA"}})

    def test_section_extraction(self):
        r = self.run_js(r"""
          const md = "intro\n# Top\n\ntext\n## A\na1\n### A.1\nsub\n## B\nb1\n```\n## not a heading\n```\n# Next\nn";
          console.log(JSON.stringify([extractSection(md, "A"), extractSection(md, "a.1"), extractSection(md, "B"),
            extractSection(md, "Top"), extractSection(md, "Missing"), extractSection(md, "**A**"),
            extractSection("## X\r\nx\r\n## Y\r\ny", "X")]));""")
        self.assertEqual(r[0], "## A\na1\n### A.1\nsub")          # up to the next heading of the same level
        self.assertEqual(r[1], "### A.1\nsub")                      # up to a higher level
        self.assertEqual(r[2], "## B\nb1\n```\n## not a heading\n```")   # fenced "heading" ignored
        self.assertEqual(r[3], "# Top\n\ntext\n## A\na1\n### A.1\nsub\n## B\nb1\n```\n## not a heading\n```")
        self.assertIsNone(r[4])
        self.assertEqual(r[5], r[0])
        self.assertEqual(r[6], "## X\nx")

    def test_frontmatter_stripped(self):
        r = self.run_js(r"""console.log(JSON.stringify([stripFrontmatter("---\na: 1\n---\nbody"),
          stripFrontmatter("---\r\na: 1\r\n---\r\nbody"), stripFrontmatter("no fm\n---\nx"), stripFrontmatter("---\nunclosed")]));""")
        self.assertEqual(r, ["body", "body", "no fm\n---\nx", "---\nunclosed"])

    def test_verdicts(self):
        r = self.run_js("""console.log(JSON.stringify([embedVerdict(["A"], "A"), embedVerdict(["A", "B"], "A"),
          embedVerdict(["A"], "B"), embedVerdict(["A", "B"], "C"), embedVerdict(["A", "B", "C"], "D")]));""")
        self.assertEqual(r, ["circular", "circular", "ok", "ok", "depth"])

    def test_embed_flow_cycles_and_depth(self):
        r = self.run_js(r"""
          currentNotePath = "A.md"; embedsOn = true;
          const root = new El(); root.innerHTML = marked.parse(stripFrontmatter(EXPORT.notes["A.md"].content)); embedsOn = false;
          hydrateEmbeds(root, ["A.md"]).then(() => {
            const [b, selfA] = root.kids, [bA, bC] = b.body.kids, [cD] = bC.body.kids;
            console.log(JSON.stringify({ host: currentNotePath, on: embedsOn,
              b: b.body.innerHTML.includes("B body"), selfA: selfA.innerHTML, bA: bA.innerHTML,
              c: bC.body.innerHTML.includes("C body"), cD: cD.innerHTML }));
          });""")
        self.assertEqual(r["host"], "A.md")                           # outer state restored
        self.assertFalse(r["on"])
        self.assertTrue(r["b"])
        self.assertIn("circular embed", r["selfA"])                   # A embeds itself
        self.assertIn("circular embed", r["bA"])                      # A -> B -> A
        self.assertTrue(r["c"])
        self.assertIn("nesting limit", r["cD"])                       # A -> B -> C -> D is one level too deep

    def test_self_embed_terminates(self):
        r = self.run_js(r"""
          currentNotePath = "S.md"; embedsOn = true;
          const root = new El(); root.innerHTML = marked.parse(EXPORT.notes["S.md"].content); embedsOn = false;
          hydrateEmbeds(root, ["S.md"]).then(() => console.log(JSON.stringify(root.kids[0].innerHTML)));""")
        self.assertIn("circular embed", r)

    def test_heading_missing_target_and_relative_context(self):
        r = self.run_js(r"""
          currentNotePath = "H.md"; embedsOn = true;
          const root = new El(); root.innerHTML = marked.parse(EXPORT.notes["H.md"].content); embedsOn = false;
          const html = root._html;
          hydrateEmbeds(root, ["H.md"]).then(() => console.log(JSON.stringify({ html,
            part: root.kids[0].body.innerHTML, nope: root.kids[1].body.innerHTML, e: root.kids[2].body.innerHTML, n: root.kids.length })));""")
        self.assertEqual(r["n"], 3)                                   # ![[Zzz]] is not an embed box
        self.assertIn("wl missing", r["html"])                        # ...it is the usual missing-note placeholder
        self.assertIn("B part text", r["part"])
        self.assertIn("B sub text", r["part"])                        # deeper heading stays in the section
        self.assertNotIn("B other", r["part"])
        self.assertNotIn("B body", r["part"])
        self.assertIn("Section not found", r["nope"])
        self.assertIn('src="data:image/png;base64,AAAA"', r["e"])    # resolved against sub/E.md, not H.md
        self.assertNotIn("skip me", r["e"])                           # frontmatter left out
        self.assertIn('>label</a>', r["html"])

    def test_embed_markup_and_plain_link_fallback(self):
        r = self.run_js(r"""
          currentNotePath = "H.md";
          const off = marked.parse("![[B#Part|x <b>y</b>]]"); embedsOn = true;
          const on = marked.parse("![[B#Part|x <b>y</b>]]"); embedsOn = false;
          console.log(JSON.stringify({ off, on }));""")
        self.assertNotIn("embed", r["off"])                           # chat bubbles etc.: just a link
        self.assertIn('class="embed" data-embed-path="B.md" data-embed-heading="Part"', r["on"])
        self.assertIn('data-path="B.md">x &lt;b&gt;y&lt;/b&gt;</a>', r["on"])

    def test_page_wires_hydration_after_render(self):
        js = page_script("offline")
        self.assertRegex(js, r'innerHTML =\s*\n\s*`<div class="note-body">[^\n]*\n\n\s*if \(typeof marked !== "undefined"\) hydrateEmbeds\(')
        self.assertIn("embedsOn = false", js)


@unittest.skipUnless(NODE, "node not available")
class CalloutTests(unittest.TestCase):
    def render(self, md):
        return run_page_js(f"console.log(JSON.stringify(marked.parse({json.dumps(md)})));")

    def test_foldable_callouts(self):
        closed = self.render("> [!tip]- Hidden **bold**\n> secret body")
        self.assertIn('<details class="callout c-tip"><summary class="callout-title">Hidden <strong>bold</strong></summary>', closed)
        self.assertIn('<div class="callout-body"><p>secret body</p>', closed)
        self.assertNotIn(" open", closed.split(">")[0] + closed.split(">")[1][:20])
        opened = self.render("> [!warning]+ Shown\n> body")
        self.assertTrue(opened.startswith('<details class="callout c-warning" open><summary class="callout-title">Shown</summary>'))
        untitled = self.render("> [!note]-\n> x")
        self.assertIn(">Note</summary>", untitled)

    def test_plain_callouts_unchanged(self):
        self.assertEqual(self.render("> [!note] A Note\n> body"),
                         '<div class="callout c-note"><div class="callout-title">A Note</div>'
                         '<div class="callout-body"><p>body</p>\n</div></div>\n')
        self.assertNotIn("<details", self.render("> [!warning]\n> No title given."))
        self.assertNotIn("callout", self.render("> Just a plain blockquote."))


class FrontmatterParserTests(unittest.TestCase):
    CASES = [
        # (name, text, expected)
        ("scalars", "---\ntype: wiedza\nstatus: active\n---\nx", {"type": "wiedza", "status": "active"}),
        ("double quoted", '---\na: "hello: world"\nb: "esc \\" q" # note\n---\n', {"a": "hello: world", "b": 'esc " q'}),
        ("single quoted", "---\na: 'it''s'\nb: ' pad '\n---\n", {"a": "it's", "b": " pad "}),
        ("inline list", '---\ntags: [a, "b c", \'d, e\', f]\nnone: []\n---\n', {"tags": ["a", "b c", "d, e", "f"], "none": []}),
        ("inline list with wikilink", "---\nmix: [x, [[Y]]]\n---\n", {"mix": ["x", "[[Y]]"]}),
        ("wikilink scalar is not a list", "---\nr: [[Beta]]\nq: \"[[Beta]]\"\n---\n", {"r": "[[Beta]]", "q": "[[Beta]]"}),
        ("block list", '---\ntags:\n  - one\n  - "two words"\n  - \'three\'\nafter: 1\n---\n',
         {"tags": ["one", "two words", "three"], "after": "1"}),
        ("block list at key indent", "---\nflat:\n- a\n- b\nz: end\n---\n", {"flat": ["a", "b"], "z": "end"}),
        ("comments", "---\n# own line\ntype: x\n  # indented\n\nstatus: y\n---\n", {"type": "x", "status": "y"}),
        ("comment inside a block list", "---\nl:\n  - a\n  # skipped\n  - b\n---\n", {"l": ["a", "b"]}),
        ("empty values", "---\nk:\nj: \nl: ''\n---\n", {"k": "", "j": "", "l": ""}),
        ("windows line endings", "---\r\ntype: a\r\ntags:\r\n  - x\r\n  - y\r\n---\r\nbody", {"type": "a", "tags": ["x", "y"]}),
        ("bom", "\ufeff---\ntype: z\n---\n", {"type": "z"}),
        ("colons in values", "---\ndate: 2024-01-01T10:00:00\nurl: https://x.y/z\n---", {"date": "2024-01-01T10:00:00", "url": "https://x.y/z"}),
        ("hash in plain value is kept", "---\ncolor: #ff0000\n---\n", {"color": "#ff0000"}),
        ("nested map kept as raw text", "---\nmeta:\n  a: 1\n  b: 2\nnext: ok\n---\n", {"meta": "a: 1\nb: 2", "next": "ok"}),
        ("block scalars kept raw", "---\nlit: |\n  line1\n\n  # hash\n  line2\nfold: >-\n  f1\n  f2\n---\n",
         {"lit": "line1\n\n# hash\nline2", "fold": "f1\nf2"}),
        ("multi-line plain value", "---\nplain: first\n  second\nnext: 1\n---\n", {"plain": "first\nsecond", "next": "1"}),
        ("unterminated quote / list keep raw", '---\nu: [a, b\nv: "q\nw: \'x\n---\n', {"u": "[a, b", "v": '"q', "w": "'x"}),
        ("duplicate key: last wins", "---\na: 1\na: 2\n---\n", {"a": "2"}),
        ("garbage lines are ignored", "---\n:::\n- stray\n  indented\nno colon here\nok: 1\n---\n", {"ok": "1"}),
        ("no frontmatter", "# title\n---\ntype: x\n---\n", {}),
        ("unclosed block", "---\ntype: x\n", {}),
        ("empty block", "---\n---\nbody", {}),
        ("empty input", "", {}),
    ]

    def test_table(self):
        for name, text, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(viewer.parse_frontmatter(text), expected)

    def test_never_raises_on_junk(self):
        junk = ["---\n" + "\x00[{\"'\\" * 50 + "\n---\n", "---\n" + ("k: [" * 100) + "\n---\n", "---\n- - - -\n:\n: :\n---\n",
                "---\nk: \"" + "\\" * 99 + "\n---\n", "---" * 100]
        for text in junk:
            with self.subTest(text=text[:20]):
                self.assertIsInstance(viewer.parse_frontmatter(text), dict)

    def test_values_are_str_or_list_of_str(self):
        for _, text, _ in self.CASES:
            for v in viewer.parse_frontmatter(text).values():
                self.assertTrue(isinstance(v, str) or (isinstance(v, list) and all(isinstance(i, str) for i in v)))

    def test_fm_str_normalises_lists(self):
        self.assertEqual(viewer.fm_str({"type": ["a", "b"]}, "type"), "a")
        self.assertEqual(viewer.fm_str({"type": []}, "type"), "")
        self.assertEqual(viewer.fm_str({"type": "x"}, "type"), "x")
        self.assertEqual(viewer.fm_str({}, "status"), "")

    def test_list_valued_type_status_date_stay_strings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "N.md").write_text("---\ntype: [wiedza, extra]\nstatus:\n  - active\ndate: [2025-01-01]\n---\n# N\n",
                                       encoding="utf-8")
            viewer.init_config(root)
            try:
                note = viewer.scan_vault()["folders"][0]["notes"][0]
                self.assertEqual((note["type"], note["status"], note["date"]), ("wiedza", "active", "2025-01-01"))
                self.assertEqual(viewer.VaultIndex().notes[0]["type"], "wiedza")
            finally:
                viewer.init_config(VAULT)


class FrontmatterApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_api_note_has_parsed_frontmatter(self):
        st, _, body = self.s.req("GET", "/api/note?path=10-knowledge/Alpha.md")
        d = json.loads(body)
        self.assertEqual(d["frontmatter"], {"type": "wiedza", "status": "active", "tags": ["testing", "fixture"],
                                            "related": ["[[Beta]]"]})
        self.assertEqual((d["type"], d["status"]), ("wiedza", "active"))
        d = json.loads(self.s.req("GET", "/api/note?path=10-knowledge/Beta.md")[2])
        self.assertEqual(d["frontmatter"], {"type": "wiedza"})
        d = json.loads(self.s.req("GET", "/api/note?path=Home.md")[2])
        self.assertIsInstance(d["frontmatter"], dict)

    def test_export_has_the_same_field(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o.html"
            subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(VAULT), "--export", str(out)],
                           check=True, capture_output=True)
            data = re.search(r"window\.__EXPORT__ = (.*?);</script>", out.read_text(encoding="utf-8"), re.S).group(1)
        notes = json.loads(data.replace("<\\/", "</").replace("<\\u0021--", "<!--"))["notes"]
        self.assertEqual(notes["10-knowledge/Alpha.md"]["frontmatter"]["tags"], ["testing", "fixture"])

    def test_chips_use_server_field_with_fallback(self):
        js = page_script("offline")
        self.assertIn("data.frontmatter || parseFmFallback(", js)


class LintTests(unittest.TestCase):
    LINT = ROOT / "tools" / "vault-lint.py"

    def run_lint(self, vault):
        return subprocess.run([sys.executable, str(self.LINT), "--vault", str(vault)], capture_output=True, text=True)

    def test_fixture_vault_output_is_unchanged(self):
        r = self.run_lint(VAULT)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("3 problem(ów) w 9 plikach", r.stdout)
        self.assertIn("zepsuty wikilink: [[Does Not Exist]]", r.stdout)
        self.assertEqual(r.stdout.count("brak wymaganego pola 'status' dla type: projekt"), 2)
        self.assertNotIn("brak frontmattera", r.stdout)

    def test_uses_the_viewer_parser(self):
        spec = importlib.util.spec_from_file_location("vault_lint", self.LINT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIsNone(mod.parse_frontmatter("# no frontmatter"))
        self.assertEqual(mod.parse_frontmatter("---\ntags: [a]\n---\n"), {"tags": ["a"]})
        self.assertIs(mod._parse_frontmatter, viewer.parse_frontmatter)

    def test_list_values_and_quotes_in_lint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "viewer.json").write_text(json.dumps({"lint": {"valid_types": ["projekt"],
                                              "required_fields": {"projekt": ["status", "owners"]}}}), encoding="utf-8")
            (root / "Ok.md").write_text('---\ntype: "projekt"\nstatus: \'x\'\nowners:\n  - ann\n---\n', encoding="utf-8")
            (root / "Bad.md").write_text("---\ntype: [projekt]\nstatus: x\nowners: []\n---\n", encoding="utf-8")
            r = self.run_lint(root)
        self.assertIn("Bad.md", r.stdout)
        self.assertIn("brak wymaganego pola 'owners'", r.stdout)
        self.assertNotIn("Ok.md", r.stdout)
        self.assertEqual(r.stdout.count("brak wymaganego"), 1)


class RemoteImagesTests(unittest.TestCase):
    def serve(self, cfg, offline=False):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "N.md").write_text("# N\n\n![alt](https://tracker.example/p.gif)\n", encoding="utf-8")
        if cfg is not None:
            (root / ".viewer.json").write_text(json.dumps(cfg), encoding="utf-8")
        s = Server(offline=offline, vault=root)
        self.addCleanup(viewer.init_config, VAULT)
        self.addCleanup(s.stop)
        return s

    def csp(self, s, path="/"):
        return s.req("GET", path)[1]["Content-Security-Policy"]

    def test_csp_per_policy(self):
        base = viewer.CSP_ONLINE
        for cfg, img in [(None, "img-src 'self' data: https:"), ({"remote_images": "allow"}, "img-src 'self' data: https:"),
                         ({"remote_images": "click"}, "img-src 'self' data: https:"),
                         ({"remote_images": "block"}, "img-src 'self' data:")]:
            with self.subTest(cfg=cfg):
                s = self.serve(cfg)
                for path in ("/", "/api/vault", "/api/note?path=N.md"):
                    csp = self.csp(s, path)
                    self.assertIn(img + ";", csp)
                    # nothing else changes
                    self.assertEqual(csp.replace(img, "IMG"), base.replace("img-src 'self' data: https:", "IMG"))
                s.stop()
                viewer.init_config(VAULT)

    def test_default_is_unchanged(self):
        s = self.serve(None)
        self.assertEqual(self.csp(s), viewer.CSP_ONLINE)
        self.assertEqual(viewer.REMOTE_IMAGES, "allow")

    def test_offline_csp_unchanged_for_every_value(self):
        for v in ("allow", "click", "block"):
            with self.subTest(v=v):
                s = self.serve({"remote_images": v}, offline=True)
                self.assertEqual(self.csp(s), viewer.CSP_OFFLINE)
                s.stop()
                viewer.init_config(VAULT)

    def test_client_receives_active_policy(self):
        for v in ("allow", "click", "block"):
            with self.subTest(v=v):
                s = self.serve({"remote_images": v})
                html = s.req("GET", "/")[2].decode()
                self.assertIn(f'"remote_images": "{v}"', html)
                s.stop()
                viewer.init_config(VAULT)
        # offline and export always say "block", whatever the config
        viewer.init_config(VAULT)
        self.assertIn('"remote_images": "block"', viewer.render_page("offline"))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".viewer.json").write_text('{"remote_images": "allow"}', encoding="utf-8")
            viewer.init_config(Path(d))
            try:
                self.assertIn('"remote_images": "allow"', viewer.render_page("online"))
                self.assertIn('"remote_images": "block"', viewer.render_page("offline"))
                self.assertIn('"remote_images": "block"', viewer.render_page("export", {"notes": {}}))
            finally:
                viewer.init_config(VAULT)
        self.assertIn("remoteImagesPolicy()", page_script("online"))

    def test_invalid_value_fails_closed_with_a_warning(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(err):
            (Path(d) / ".viewer.json").write_text('{"remote_images": "blok"}', encoding="utf-8")
            viewer.init_config(Path(d))
            try:
                self.assertEqual(viewer.REMOTE_IMAGES, "block")
                self.assertNotIn("https:", viewer.online_csp().split("img-src")[1].split(";")[0])
            finally:
                viewer.init_config(VAULT)
        self.assertIn('"remote_images" must be one of', err.getvalue())


@unittest.skipUnless(NODE, "node not available")
class RemoteImageRenderTests(unittest.TestCase):
    def render(self, policy, md):
        return run_page_js(f"FLAGS.remote_images = {json.dumps(policy)}; currentNotePath = 'N.md';"
                           f"console.log(JSON.stringify(marked.parse({json.dumps(md)})));")

    MD = '![a <b>pic</b>](https://tracker.example:8443/p.gif?x=1&y="2" "t")'

    def test_allow_keeps_img(self):
        for policy in ("allow", None):
            html = self.render(policy, self.MD)
            self.assertIn('<img src="https://tracker.example:8443/p.gif?x=1&amp;y=&quot;2&quot;"', html)
            self.assertNotIn("remote-img", html)

    def test_block_is_a_placeholder_with_link(self):
        html = self.render("block", self.MD)
        self.assertNotIn("<img", html)
        self.assertNotIn("ri-load", html)
        self.assertIn("🖼 remote image: tracker.example:8443", html)
        self.assertIn('href="https://tracker.example:8443/p.gif?x=1&amp;y=&quot;2&quot;" target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn("<b>", html)

    def test_click_adds_a_load_button(self):
        html = self.render("click", "![x](http://h.example/a.png)")
        self.assertIn('<button type="button" class="ri-load">Load image</button>', html)
        self.assertIn('data-src="http://h.example/a.png"', html)
        self.assertNotIn("<img", html)

    def test_other_images_and_unsafe_urls_unaffected(self):
        for policy in ("block", "click"):
            html = self.render(policy, "![a](pic.png) ![b](javascript:alert(1)) ![c](//evil.example/x.png)")
            self.assertNotIn("remote-img", html)
            self.assertNotIn("javascript", html)
            self.assertNotIn("evil.example", html)

    def test_embeds_use_the_same_renderer(self):
        # a note embedded in a note is rendered by the same marked setup, so the policy applies there too
        r = run_page_js("""FLAGS.remote_images = "block"; vault = {folders: [], stems: {}}; currentNotePath = "E.md"; embedsOn = true;
          console.log(JSON.stringify(marked.parse("![](https://t.example/p.gif)")));""")
        self.assertIn("remote-img", r)

    def test_click_handler_swaps_in_one_image(self):
        js = page_script("online")
        self.assertIn('button.ri-load', js)
        self.assertIn("w.replaceWith(img)", js)
        self.assertRegex(js, r"if \(!src \|\| !/\^https\?:")           # only http(s) sources are ever loaded


EXAMPLE_PLUGIN = ROOT / "examples" / "plugins" / "tasks-board.js"


def make_plugin_vault(root: Path, plugins=None, plugin_src=None):
    """Temp copy of the fixture vault with the example plug-in under .viewer/plugins/ (a folder the
    viewer config would normally skip) and a .viewer.json listing `plugins`."""
    shutil.copytree(VAULT, root)
    (root / ".viewer" / "plugins").mkdir(parents=True)
    (root / ".viewer" / "plugins" / "tasks-board.js").write_text(
        plugin_src if plugin_src is not None else EXAMPLE_PLUGIN.read_text(encoding="utf-8"), encoding="utf-8")
    (root / ".viewer" / "plugins" / "other.js").write_text("window.__other = 1;", encoding="utf-8")
    if plugins is None:
        plugins = [{"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js", "enabled": True}]
    (root / ".viewer.json").write_text(json.dumps({"skip_dirs": [".obsidian", ".git", ".viewer"], "plugins": plugins}),
                                       encoding="utf-8")


class LoadPluginsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault"
        make_plugin_vault(self.root)
        self.addCleanup(self._tmp.cleanup)

    def load(self, plugins):
        return viewer.load_plugins({"plugins": plugins}, self.root)

    def entry(self, **kw):
        e = {"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js", "enabled": True}
        e.update(kw)
        return e

    def test_valid_entry(self):
        found, problems = self.load([self.entry()])
        self.assertEqual(problems, [])
        self.assertEqual(found, {"tasks-board": (self.root / ".viewer/plugins/tasks-board.js").resolve()})

    def test_not_subject_to_skip_dirs(self):
        # .viewer is in skip_dirs in make_plugin_vault, yet the plug-in file loads
        viewer.init_config(self.root)
        try:
            self.assertIn(".viewer", viewer.SKIP_DIRS)
            self.assertIn("tasks-board", viewer.PLUGINS)
        finally:
            viewer.init_config(VAULT)

    def test_no_key_or_disabled_means_nothing_loads(self):
        self.assertEqual(viewer.load_plugins({}, self.root), ({}, []))
        for enabled in (False, None, "true", 1, "yes"):
            with self.subTest(enabled=enabled):
                found, problems = self.load([self.entry(enabled=enabled)])
                self.assertEqual((found, problems), ({}, []))
        found, _ = self.load([{"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js"}])
        self.assertEqual(found, {})

    def test_bad_entries_warn_and_are_skipped(self):
        outside = self.root.parent / "evil.js"
        outside.write_text("x", encoding="utf-8")
        link = self.root / ".viewer" / "plugins" / "link.js"
        try:
            link.symlink_to(outside)
            symlink_case = [self.entry(id="linked", file=".viewer/plugins/link.js")]
        except OSError:
            symlink_case = []
        cases = [
            self.entry(id="Tasks"), self.entry(id="-tasks"), self.entry(id="a" * 42), self.entry(id=""), self.entry(id=5),
            self.entry(id=None), self.entry(id="has space"), self.entry(id="a/b"), self.entry(id="a_b"),
            self.entry(file="x.txt"), self.entry(file=".viewer/plugins/tasks-board.js.map"), self.entry(file=5),
            self.entry(file=None), self.entry(file=".viewer/plugins/missing.js"), self.entry(file=".viewer/plugins"),
            self.entry(file="../evil.js"), self.entry(file=str(outside)), self.entry(file="/etc/passwd.js"),
            self.entry(file=".viewer/../../evil.js"), self.entry(file=".viewer/plugins/tasks-board.js\0.js"),
            "just a string", 5, None, ["list"],
        ] + symlink_case
        for c in cases:
            with self.subTest(entry=c):
                found, problems = self.load([c])
                self.assertEqual(found, {})
                self.assertEqual(len(problems), 1)

    def test_valid_ids_at_the_edges(self):
        for pid in ("a", "0", "a-b-c", "a" * 41, "9lives"):
            with self.subTest(pid=pid):
                self.assertIn(pid, self.load([self.entry(id=pid)])[0])

    def test_duplicate_id_keeps_the_first(self):
        found, problems = self.load([self.entry(), self.entry(file=".viewer/plugins/other.js")])
        self.assertEqual(found["tasks-board"].name, "tasks-board.js")
        self.assertEqual(len(problems), 1)
        self.assertIn("duplicate", problems[0])

    def test_one_bad_entry_does_not_hide_the_good_one(self):
        found, problems = self.load([self.entry(id="BAD"), self.entry(id="ok", file=".viewer/plugins/other.js")])
        self.assertEqual(list(found), ["ok"])
        self.assertEqual(len(problems), 1)

    def test_plugins_not_a_list(self):
        for bad in ({"id": "x"}, "x", 5):
            found, problems = viewer.load_plugins({"plugins": bad}, self.root)
            self.assertEqual((found, len(problems)), ({}, 1))

    def test_too_big_file_rejected(self):
        old = viewer.PLUGIN_MAX_BYTES
        viewer.PLUGIN_MAX_BYTES = 5
        try:
            found, problems = self.load([self.entry()])
        finally:
            viewer.PLUGIN_MAX_BYTES = old
        self.assertEqual((found, len(problems)), ({}, 1))

    def test_init_config_prints_warnings_and_never_crashes(self):
        (self.root / ".viewer.json").write_text(json.dumps({"plugins": [self.entry(file="../evil.js"), 7]}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            viewer.init_config(self.root)
        try:
            self.assertEqual(viewer.PLUGINS, {})
            self.assertEqual(err.getvalue().count("warning: viewer config:"), 2)
        finally:
            viewer.init_config(VAULT)


class PluginServingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault"
        make_plugin_vault(self.root, plugins=[
            {"id": "tasks-board", "file": ".viewer/plugins/tasks-board.js", "enabled": True},
            {"id": "off", "file": ".viewer/plugins/other.js", "enabled": False},
        ])
        (self.root.parent / "secret.js").write_text("SECRET", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def serve(self, offline=False):
        s = Server(offline=offline, vault=self.root)
        self.addCleanup(viewer.init_config, VAULT)
        self.addCleanup(s.stop)
        return s

    def test_enabled_plugin_is_served(self):
        s = self.serve()
        st, h, body = s.req("GET", "/plugins/tasks-board.js")
        self.assertEqual(st, 200)
        self.assertEqual(body, EXAMPLE_PLUGIN.read_bytes())
        self.assertEqual(h["Content-Type"], "application/javascript; charset=utf-8")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Content-Security-Policy"], viewer.CSP_ONLINE)       # plug-ins get no extra origins

    def test_allow_list_is_exact(self):
        s = self.serve()
        for path in ["/plugins/off.js",                       # present on disk, listed but disabled
                     "/plugins/other.js",                     # on disk, not listed at all
                     "/plugins/nope.js", "/plugins/", "/plugins", "/plugins/tasks-board", "/plugins/tasks-board.js/",
                     "/plugins/tasks-board.js/x", "/plugins/TASKS-BOARD.js", "/plugins/../viewer.json",
                     "/plugins/..%2fsecret.js", "/plugins/%2e%2e/secret.js", "/plugins/tasks-board.js%00.png",
                     "/plugins/.viewer/plugins/tasks-board.js", "/plugins//tasks-board.js", "/plugins/tasks-board.JS",
                     "/plugins/-x.js", "/plugins/" + "a" * 60 + ".js"]:
            with self.subTest(path=path):
                st, _, body = s.req("GET", path)
                self.assertIn(st, (404, 403))
                self.assertNotIn(b"SECRET", body)
                self.assertNotIn(b"__other", body)
                self.assertNotIn(b"VaultViewer.register", body)

    def test_host_guard_applies(self):
        s = self.serve()
        self.assertEqual(s.req("GET", "/plugins/tasks-board.js", host="evil.example")[0], 403)

    def test_script_tag_only_for_enabled_plugins(self):
        for offline in (False, True):
            with self.subTest(offline=offline):
                s = self.serve(offline=offline)
                html = s.req("GET", "/")[2].decode()
                self.assertEqual(html.count('<script src="/plugins/'), 1)
                self.assertIn('<script src="/plugins/tasks-board.js"></script>', html)
                self.assertNotIn("off.js", html)
                self.assertNotIn("/plugins/other", html)
                self.assertIn('"plugins": ["tasks-board"]', html)
                # after the app script, so window.VaultViewer exists when the plug-in runs
                self.assertGreater(html.index('<script src="/plugins/tasks-board.js">'), html.index("window.VaultViewer = "))
                s.stop()
                viewer.init_config(VAULT)

    def test_no_plugins_no_tags_and_no_change(self):
        viewer.init_config(VAULT)
        html = viewer.render_page("online")
        self.assertNotIn("/plugins/", html.replace("/plugins/<id>", ""))
        self.assertIn('"plugins": []', html)

    def test_edits_to_an_enabled_plugin_show_without_restart(self):
        s = self.serve()
        (self.root / ".viewer" / "plugins" / "tasks-board.js").write_text("// v2", encoding="utf-8")
        self.assertEqual(s.req("GET", "/plugins/tasks-board.js")[2], b"// v2")

    def test_plugin_replaced_by_symlink_out_of_vault_is_refused(self):
        s = self.serve()
        f = self.root / ".viewer" / "plugins" / "tasks-board.js"
        f.unlink()
        try:
            f.symlink_to(self.root.parent / "secret.js")
        except OSError:
            self.skipTest("symlinks not supported")
        st, _, body = s.req("GET", "/plugins/tasks-board.js")
        self.assertEqual(st, 404)
        self.assertNotIn(b"SECRET", body)


class PluginExportTests(unittest.TestCase):
    def test_export_inlines_plugin_and_escapes_closing_tags(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "vault"
            src = EXAMPLE_PLUGIN.read_text(encoding="utf-8") + '\nwindow.__x = "</script><b>"; var y = "</SCRIPT>";\n'
            make_plugin_vault(root, plugin_src=src)
            out = Path(d) / "out.html"
            r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(root), "--export", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertIn("Plug-in enabled: tasks-board (.viewer/plugins/tasks-board.js)", r.stdout)
        self.assertIn('id: "tasks-board"', html)                              # inlined
        self.assertNotRegex(html, r'<script[^>]+src=')                         # nothing external
        self.assertNotIn('src="/plugins/', html)
        self.assertNotIn("__other", html)                                      # the unlisted file is not included
        self.assertIn('window.__x = "<\\/script><b>"', html)
        self.assertIn('var y = "<\\/SCRIPT>"', html)
        # marked, force-graph, data, app, plug-in: the plug-in's own "</script" strings did not close anything early
        self.assertEqual(len(re.findall(r"</script>", html, re.I)), 5)
        self.assertIn("connect-src 'none'", html)
        self.assertIn('"plugins": ["tasks-board"]', html)
        self.assertLess(html.index("window.VaultViewer = "), html.index('id: "tasks-board"'))

    def test_export_without_plugins_is_unchanged(self):
        viewer.init_config(VAULT)
        html = viewer.render_page("export", {"notes": {}})
        self.assertNotIn("tasks-board", html)


@unittest.skipUnless(NODE, "node not available")
class PluginHostTests(unittest.TestCase):
    def test_register_validation(self):
        r = run_page_js("""
          FLAGS.plugins = ["good", "future", "nomount"];
          console.warn = () => {};
          const ok = { id: "good", title: "Good", api: 1, mount() {} };
          const results = {
            good: window.VaultViewer.register(ok),
            again: window.VaultViewer.register({ ...ok }),                               // duplicate
            future: window.VaultViewer.register({ ...ok, id: "future", api: 2 }),         // api > 1
            noapi: window.VaultViewer.register({ id: "future", title: "x", mount() {} }),
            zero: window.VaultViewer.register({ id: "future", title: "x", api: 0, mount() {} }),
            notEnabled: window.VaultViewer.register({ ...ok, id: "evil" }),
            badId: window.VaultViewer.register({ ...ok, id: "../x" }),
            nomount: window.VaultViewer.register({ id: "nomount", title: "x", api: 1 }),
            notitle: window.VaultViewer.register({ id: "nomount", api: 1, mount() {} }),
            notObj: window.VaultViewer.register(null),
            str: window.VaultViewer.register("good"),
          };
          let replaced = true;
          try { "use strict"; window.VaultViewer.register = () => 1; replaced = window.VaultViewer.register(null) === 1; } catch (_) { replaced = false; }
          console.log(JSON.stringify({ results, version: window.VaultViewer.apiVersion, registered: [...pluginRegistry.keys()], replaced }));""")
        self.assertEqual(r["results"], {"good": True, "again": False, "future": False, "noapi": False, "zero": False,
                                        "notEnabled": False, "badId": False, "nomount": False, "notitle": False,
                                        "notObj": False, "str": False})
        self.assertEqual(r["version"], 1)
        self.assertEqual(r["registered"], ["good"])
        self.assertFalse(r["replaced"])                                  # the host object is frozen

    def test_refusals_are_logged_with_a_reason(self):
        r = run_page_js("""
          FLAGS.plugins = ["p"]; const logs = []; console.warn = (...a) => logs.push(a.join(" "));
          window.VaultViewer.register({ id: "p", title: "t", api: 2, mount() {} });
          window.VaultViewer.register({ id: "q", title: "t", api: 1, mount() {} });
          console.log(JSON.stringify(logs));""")
        self.assertIn("needs host API 2", r[0])
        self.assertIn("not enabled", r[1])

    def test_host_object(self):
        r = run_page_js("""
          FLAGS.remote_images = "block";
          vault = { folders: [{ folder: "10-k", notes: [{ path: "10-k/A.md", name: "A", type: "wiedza" }, { path: "B.md", name: "B" }] }], stems: {} };
          currentNotePath = "outer.md";
          const host = makeHost();
          const html = host.renderMarkdown("<b>raw</b> ![](https://t.example/p.gif) ![[Nope]] [x](javascript:alert(1))", "10-k/A.md");
          const hits = []; const off = host.onVaultChange(() => hits.push(1));
          fireVaultChange(); off(); fireVaultChange();
          vaultChangeCbs.add(() => { throw new Error("boom"); }); console.warn = () => {}; fireVaultChange();
          console.log(JSON.stringify({ html, version: host.apiVersion, notes: host.notes(), esc: host.esc("<&>\\"'"),
            frozen: Object.isFrozen(host), hits: hits.length, restored: currentNotePath, embeds: embedsOn,
            apiIsFn: typeof host.api, openNote: typeof host.openNote }));""")
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", r["html"])
        self.assertIn("remote-img", r["html"])                           # the remote-images policy applies
        self.assertNotIn("<img", r["html"])
        self.assertNotIn("javascript:", r["html"])
        self.assertEqual(r["notes"], [{"path": "10-k/A.md", "name": "A", "type": "wiedza", "folder": "10-k"},
                                      {"path": "B.md", "name": "B", "type": "", "folder": "10-k"}])
        self.assertEqual(r["esc"], "&lt;&amp;&gt;&quot;&#39;")
        self.assertEqual((r["version"], r["frozen"], r["hits"]), (1, True, 1))   # unsubscribed after the first fire
        self.assertEqual((r["restored"], r["embeds"]), ("outer.md", False))
        self.assertEqual((r["apiIsFn"], r["openNote"]), ("function", "function"))

    def test_render_markdown_resolves_relative_to_base_path(self):
        r = run_page_js("""
          vault = { folders: [{ folder: "x", notes: [{ path: "d/Other.md", name: "Other" }] }], stems: {} };
          console.log(JSON.stringify(makeHost().renderMarkdown("[o](Other.md)", "d/Here.md")));""")
        self.assertIn('data-path="d/Other.md"', r)

    def test_host_api_is_the_app_api(self):
        r = run_page_js("""
          const notes = {"A.md": {path: "A.md", name: "A", content: "x"}};
          Promise.all([makeHost().api("note", "A.md"), makeHost().api("search", "hell")]).then(v => console.log(JSON.stringify(v)));""",
                        export_data={"notes": {"A.md": {"path": "A.md", "name": "A", "content": "hello"}}})
        self.assertEqual(r[0]["content"], "hello")
        self.assertEqual(r[1][0]["path"], "A.md")                        # search runs client-side in an export

    def test_page_has_overlay_button_slot_and_escape(self):
        js = page_script("online")
        html = viewer.render_page("online")
        self.assertIn('id="plugin-wrap" hidden', html)
        self.assertIn('id="plugin-mount"', html)
        self.assertIn('anchor.after(btn)', js)
        self.assertIn('document.getElementById("graph-btn")', js)
        self.assertRegex(js, r'if \(e\.key === "Escape"\) \{\s*if \(activePlugin\) closePlugin\(\);')
        self.assertRegex(js, r"function openNote\(note\) \{\s*closeSidebar\(\);\s*if \(graphMode\) closeGraph\(\);\s*closePlugin\(\);")
        self.assertIn("rec.def.mount(mount, makeHost())", js)
        self.assertIn("rec.def.unmount()", js)
        self.assertIn("fireVaultChange()", js)


@unittest.skipUnless(NODE, "node not available")
class ExamplePluginTests(unittest.TestCase):
    """Runs examples/plugins/tasks-board.js under node against a fake host fed with the fixture vault."""

    def run_plugin(self, extra_notes=None, search_override=None):
        viewer.init_config(VAULT)
        notes = {n["path"]: {"path": n["path"], "name": n["name"], "content": n["text"]} for n in viewer.get_index().notes}
        notes.update(extra_notes or {})
        script = f"""
          const src = require("fs").readFileSync({json.dumps(str(EXAMPLE_PLUGIN))}, "utf8");
          const notes = {json.dumps(notes)}; let searchResult = {json.dumps(search_override)};
          const win = {{ VaultViewer: {{ register(d) {{ win.def = d; return true; }} }} }};
          new Function("window", src)(win);
          const log = {{ opened: [], subs: 0, unsubs: 0, calls: [] }};
          const host = {{
            esc: s => String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c])),
            api: async (kind, arg) => {{ log.calls.push(kind); if (kind === "search") return searchResult || Object.values(notes).filter(n => n.content.includes(arg)).map(n => ({{path: n.path}}));
              return notes[arg]; }},
            notes: () => Object.values(notes).map(n => ({{path: n.path}})),
            openNote: p => log.opened.push(p),
            onVaultChange: cb => {{ log.subs++; return () => log.unsubs++; }},
          }};
          const el = {{ innerHTML: "", onclick: null }};
          (async () => {{
            await win.def.mount(el, host);
            const html = el.innerHTML;
            el.onclick({{ target: {{ closest: () => ({{ dataset: {{ path: "20-projects/Tasks.md" }} }}) }}, preventDefault() {{}} }});
            el.onclick({{ target: {{ closest: () => null }}, preventDefault() {{}} }});
            win.def.unmount();
            console.log(JSON.stringify({{ meta: [win.def.id, win.def.title, win.def.api], html, log }}));
          }})();"""
        return self._node(script)

    def _node(self, script):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.js"
            f.write_text(script, encoding="utf-8")
            r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        viewer.init_config(VAULT)
        return json.loads(r.stdout)

    def test_lists_only_open_overdue_tasks(self):
        r = self.run_plugin()
        self.assertEqual(r["meta"], ["tasks-board", "Overdue", 1])
        h = r["html"]
        self.assertIn("Overdue job", h)
        self.assertIn("2020-01-01", h)
        self.assertIn("in progress", h)
        self.assertIn('data-path="20-projects/Tasks.md"', h)
        self.assertIn("1 overdue", h)
        for absent in ("Future job", "Finished job", "Waived job", "Missed job", "Verify job", "Bad link"):
            self.assertNotIn(absent, h)
        self.assertNotIn("[due::", h)                                     # inline fields are stripped from the row text

    def test_links_open_through_the_host_and_unmount_unsubscribes(self):
        r = self.run_plugin()
        self.assertEqual(r["log"]["opened"], ["20-projects/Tasks.md"])    # the click on a non-link did nothing
        self.assertEqual((r["log"]["subs"], r["log"]["unsubs"]), (1, 1))

    def test_hostile_text_is_escaped_and_code_fences_ignored(self):
        md = ("- [ ] <img src=x onerror=alert(1)> [due:: 2001-02-03]\n```\n- [ ] in fence [due:: 2001-01-01]\n```\n"
              "- [ ] closed by state [state:: Done] [due:: 2001-01-01]\n")
        r = self.run_plugin({"X <b>.md": {"path": "X <b>.md", "name": "X <b>", "content": md}})
        self.assertNotIn("<img", r["html"])
        self.assertNotIn("<b>", r["html"])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", r["html"])
        self.assertIn("X &lt;b&gt;", r["html"])
        self.assertNotIn("in fence", r["html"])
        self.assertNotIn("closed by state", r["html"])

    def test_scans_all_notes_when_search_is_cut_off(self):
        cut = [{"path": "01-daily/2025-01-01.md"}] * 50                    # a full page: results may be missing
        r = self.run_plugin(search_override=cut)
        self.assertIn("Overdue job", r["html"])                           # found by scanning host.notes()


class GraphXssTests(unittest.TestCase):
    NAME = "<img src=x onerror=alert(1)>"

    def test_graph_label_is_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            try:
                (root / (self.NAME + ".md")).write_text("# hostile\n\n[[Other]]\n", encoding="utf-8")
            except OSError:
                self.skipTest("file system rejects < > in names")
            (root / "Other.md").write_text("# Other\n", encoding="utf-8")
            s = Server(vault=root)
            try:
                st, _, b = s.req("GET", "/api/graph")
                self.assertEqual(st, 200)
                names = {n["name"] for n in json.loads(b)["nodes"]}
                self.assertIn(self.NAME, names)                 # raw name stays data...
                _, _, page = s.req("GET", "/")
            finally:
                s.stop()
        js = page.decode()
        # ...and the page must escape it before force-graph inserts the label as HTML
        self.assertRegex(js, r"\.nodeLabel\(d => esc\(d\.name\)")
        self.assertNotRegex(js, r"\.nodeLabel\(d => `\$\{d\.name\}")
        self.assertNotIn("<img src=x", js)                      # not embedded in the page itself


class CliTests(unittest.TestCase):
    def test_export_has_client_side_search(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.html"
            subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(VAULT), "--export", str(out)],
                           check=True, capture_output=True)
            html = out.read_text(encoding="utf-8")
        self.assertIn("function searchNotes(", html)
        self.assertIn('kind === "search") return searchNotes(arg, Object.values(EXPORT.notes))', html)
        self.assertIn("/api/search?q=", html)                  # live mode still calls the server

    def test_export(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.html"
            r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(VAULT),
                                "--export", str(out)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("WARNING", r.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertNotRegex(html, r'<script[^>]+src=')
        self.assertNotRegex(html, r'<link[^>]+href=')
        self.assertNotRegex(html, r'(?i)@import|url\(\s*["\']?https?:')
        self.assertIn("connect-src 'none'", html)
        self.assertIn('"chat": false', html)
        data = re.search(r"window\.__EXPORT__ = (.*?);</script>", html, re.S).group(1)
        self.assertNotIn("</", data)
        self.assertIn("<\\/script>", data)                    # the XSS probe note's text, escaped
        payload = json.loads(data.replace("<\\/", "</").replace("<\\u0021--", "<!--"))
        self.assertEqual(len(payload["notes"]), 9)
        self.assertEqual({b["name"] for b in payload["notes"]["10-knowledge/Beta.md"]["backlinks"]},
                         {"Alpha", "Gamma", "Home"})
        self.assertNotIn("SECRET", html)

    def test_offline_without_vendor_fails_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / "viewer.py", d)               # no vendor/ next to this copy
            r = subprocess.run([sys.executable, str(Path(d) / "viewer.py"), "--vault", str(VAULT),
                                "--serve", "--offline"], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fetch-vendor.py", r.stderr)

    def test_online_without_vendor_still_renders_without_sri(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / "viewer.py", d)               # no vendor/ next to this copy
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import viewer; "
                    "viewer.init_config(__import__('pathlib').Path(sys.argv[2])); "
                    "viewer.render_page('online'); print(viewer.render_page('online'))")
            r = subprocess.run([sys.executable, "-c", code, d, str(VAULT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js", r.stdout)
        self.assertNotIn("integrity=", r.stdout)
        self.assertEqual(r.stderr.count("warning:"), 1)      # warned once, not per render

    def test_page_script_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        html = viewer.render_page("offline")
        js = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "page.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(NODE, "node not available")
class RelativeLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        viewer.init_config(VAULT)
        cls.vault = json.dumps(viewer.scan_vault())

    def render(self, base, md):
        return run_page_js(f"vault = {self.vault}; currentNotePath = {json.dumps(base)};"
                           f"console.log(JSON.stringify(marked.parse({json.dumps(md)})));")

    def test_fixture_note_links(self):
        text = (VAULT / "10-knowledge/deep/Links.md").read_text(encoding="utf-8")
        html = self.render("10-knowledge/deep/Links.md", text)
        spaced = 'data-path="10-knowledge/deep/Spaced Note.md"'
        self.assertEqual(html.count(spaced), 3)                       # %20, #heading, ./nested/../
        self.assertIn('class="wl" href="#" data-path="10-knowledge/Alpha.md">up</a>', html)
        self.assertIn('data-path="01-daily/2025-01-01.md">daily</a>', html)
        self.assertIn('data-path="10-knowledge/Beta.md">clamped</a>', html)     # never above the root
        self.assertIn('<span class="wl missing" title="Note not found">gone</span>', html)
        self.assertIn('href="pic.png"', html)                         # other relative links untouched
        self.assertIn('href="https://example.com/page.md" target="_blank"', html)

    def test_resolved_relative_to_rendered_note(self):
        # same link text, different base folder -> different (or missing) target
        self.assertIn('data-path="10-knowledge/Alpha.md"', self.render("10-knowledge/Beta.md", "[a](Alpha.md)"))
        self.assertIn("wl missing", self.render("Home.md", "[a](Alpha.md)"))
        self.assertIn('data-path="10-knowledge/Alpha.md"', self.render("Home.md", "[a](10-knowledge/Alpha.md#x)"))
        self.assertIn('data-path="10-knowledge/Alpha.md"', self.render("20-projects/Tasks.md", "[a](/10-knowledge/Alpha.md)"))

    def test_unsafe_and_non_note_links_unchanged(self):
        html = self.render("Home.md", "[a](javascript:alert(1)) [b](//evil.example/x.md) [c](#top) [d](a.pdf)")
        self.assertNotIn("javascript", html)
        self.assertNotIn("data-path", html)
        self.assertIn('data-anchor="top"', html)
        self.assertIn('href="a.pdf"', html)


class MobileSidebarTests(unittest.TestCase):
    """Static checks only; the layout itself needs a browser (see the report)."""

    def test_toggle_markup_css_and_handlers_exist(self):
        for mode in ("online", "offline"):
            with self.subTest(mode=mode):
                viewer.init_config(VAULT)
                html = viewer.render_page(mode)
                self.assertIn('id="menu-btn"', html)
                self.assertIn('id="sb-backdrop"', html)
                # button precedes the tabs inside the tab row
                self.assertLess(html.index('id="menu-btn"'), html.index('id="tabs"'))
                css = html.split("<style>")[1].split("</style>")[0]
                self.assertRegex(css, r"\.menu-btn \{ display: none; \}")             # hidden on desktop
                media = css[css.index("@media (max-width: 580px)"):]
                self.assertRegex(media, r"\.menu-btn \{\s*display: flex")              # shown below 580px
                self.assertIn("body.sb-open .sb {", media)
                self.assertIn("body.sb-open .sb-backdrop {", media)
                js = page_script(mode)
                self.assertIn('getElementById("sb-backdrop").addEventListener("click", closeSidebar)', js)
                self.assertRegex(js, r'e\.key === "Escape" && sidebarOpen\(\)\) \{ closeSidebar\(\)')
                self.assertRegex(js, r"function openNote\(note\) \{\s*closeSidebar\(\);")


class PluginsDisabledTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault"
        make_plugin_vault(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(viewer.init_config, VAULT)

    def disable_in_config(self):
        cfgp = self.root / ".viewer.json"
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        cfg["plugins_enabled"] = False
        cfgp.write_text(json.dumps(cfg), encoding="utf-8")

    def test_flag_disables_everything(self):
        viewer.init_config(self.root, no_plugins=True)
        self.assertEqual(viewer.PLUGINS, {})
        srv = viewer.make_server(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/plugins/tasks-board.js", headers={"Host": f"localhost:{port}"})
        self.assertEqual(c.getresponse().status, 404)
        c.close()
        html = viewer.render_page("export", {"notes": {}})
        self.assertNotIn("tasks-board", html)
        self.assertNotIn("/plugins/", viewer.render_page("online"))
        self.assertIn('"plugins": []', html)

    def test_config_key_disables_everything(self):
        self.disable_in_config()
        s = Server(vault=self.root)
        self.addCleanup(s.stop)
        self.assertEqual(viewer.PLUGINS, {})
        self.assertEqual(s.req("GET", "/plugins/tasks-board.js")[0], 404)
        self.assertNotIn("tasks-board", viewer.render_page("export", {"notes": {}}))

    def test_default_is_unchanged(self):
        s = Server(vault=self.root)
        self.addCleanup(s.stop)
        self.assertEqual(list(viewer.PLUGINS), ["tasks-board"])
        self.assertEqual(s.req("GET", "/plugins/tasks-board.js")[0], 200)

    def run_cli(self, *extra):
        out = Path(self._tmp.name) / "o.html"
        r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(self.root), "--export", str(out), *extra],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, out.read_text(encoding="utf-8")

    def test_cli_flag_in_export(self):
        stdout, html = self.run_cli("--no-plugins")
        self.assertIn("Plug-ins disabled (--no-plugins)", stdout)
        self.assertNotIn("Plug-in enabled", stdout)
        self.assertNotIn("tasks-board", html)

    def test_cli_config_key_in_export(self):
        self.disable_in_config()
        stdout, html = self.run_cli()
        self.assertIn('Plug-ins disabled ("plugins_enabled": false)', stdout)
        self.assertNotIn("tasks-board", html)

    def test_startup_prints_the_config_path(self):
        child = self.root / "child"               # no config of its own: the parent folder's one is in effect
        child.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):       # its plug-in file is not under child/: a warning
            viewer.init_config(child)
        real_cfg = self.root.resolve() / ".viewer.json"       # the viewer works with resolved paths
        self.assertEqual(viewer.CONFIG_PATH, real_cfg)
        viewer.init_config(self.root)
        self.assertEqual(viewer.CONFIG_PATH, real_cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            viewer.print_plugins()
        self.assertIn(f"Plug-ins enabled by {real_cfg}", buf.getvalue())
        self.assertIn("Plug-in enabled: tasks-board", buf.getvalue())


class SymlinkLeakTests(unittest.TestCase):
    """Notes whose real path is outside the vault must not reach any index, API or export."""
    MARKER = "ZXQ-LEAK-MARKER-7731"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "vault"
        outside = base / "outside"
        (outside / "dir").mkdir(parents=True)
        (outside / "Leak.md").write_text(f"---\ntype: wiedza\n---\n# Leak\n{self.MARKER} [[Inside]]\n", encoding="utf-8")
        (outside / "dir" / "Deep.md").write_text(f"# Deep\n{self.MARKER}\n", encoding="utf-8")
        self.root.mkdir()
        (self.root / "Real.md").write_text("# Real\nreal body [[Inside]]\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "Loop.md").write_text("# Loop\n", encoding="utf-8")
        try:
            (self.root / "Leak.md").symlink_to(outside / "Leak.md")
            (self.root / "Inside.md").symlink_to(self.root / "Real.md")
            (self.root / "linkdir").symlink_to(outside / "dir", target_is_directory=True)
            (self.root / "Broken.md").symlink_to(base / "nowhere.md")
            (self.root / "Self.md").symlink_to(self.root / "Self.md")       # loop
            (self.root / "innerdir").symlink_to(self.root / "sub", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not supported")
        self.addCleanup(viewer.init_config, VAULT)

    def test_nothing_leaks_through_the_api(self):
        s = Server(vault=self.root)
        self.addCleanup(s.stop)
        for url in ("/api/vault", f"/api/search?q={self.MARKER}", "/api/graph"):
            with self.subTest(url=url):
                st, _, body = s.req("GET", url)
                self.assertEqual(st, 200)
                self.assertNotIn(self.MARKER, body.decode())
                self.assertNotIn("Leak.md", body.decode())
                self.assertNotIn("Deep.md", body.decode())
        self.assertEqual(json.loads(s.req("GET", f"/api/search?q={self.MARKER}")[2]), [])
        for rel in ("Leak.md", "linkdir/Deep.md"):
            self.assertIn(s.req("GET", "/api/note?path=" + rel)[0], (403, 404))
        st, _, body = s.req("GET", "/api/note?path=Real.md")
        self.assertEqual(st, 200)
        self.assertNotIn(self.MARKER, body.decode())                          # backlinks included

    def test_symlinks_inside_the_vault_still_work(self):
        s = Server(vault=self.root)
        self.addCleanup(s.stop)
        v = json.loads(s.req("GET", "/api/vault")[2])
        self.assertIn("Inside.md", json.dumps(v["tree"] if "tree" in v else v))
        self.assertEqual(s.req("GET", "/api/note?path=Inside.md")[0], 200)
        self.assertEqual(s.req("GET", "/api/note?path=innerdir/Loop.md")[0], 200)
        self.assertEqual(viewer.VaultIndex().resolve("Inside"), "Inside.md")
        paths = [p.relative_to(viewer.VAULT).as_posix() for p in viewer.vault_files()]
        self.assertNotIn("Broken.md", paths)
        self.assertNotIn("Self.md", paths)

    def test_export_has_no_marker(self):
        out = Path(self._tmp.name) / "out.html"
        r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(self.root), "--export", str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = out.read_text(encoding="utf-8")
        self.assertNotIn(self.MARKER, html)
        self.assertIn("Inside.md", html)

    def test_only_vault_files_walks_the_vault(self):
        src = (ROOT / "viewer.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"VAULT\.rglob\(", src)), 1)
        self.assertIn('for p in VAULT.rglob("*")', src.split("def vault_files")[1].split("\ndef ")[0])

    def test_lint_ignores_outside_notes(self):
        spec = importlib.util.spec_from_file_location("vault_lint", ROOT / "tools" / "vault-lint.py")
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["vault-lint.py", "--vault", str(self.root)]
        try:
            with contextlib.redirect_stdout(buf):
                lint.lint()
        finally:
            sys.argv = old
        self.assertNotIn("Leak.md", buf.getvalue())
        self.assertNotIn("Deep.md", buf.getvalue())


def _lum(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def css_blocks():
    """(dark vars, light vars in the media query, light vars in [data-theme=light]) from the page CSS."""
    css = re.search(r"<style>(.*?)</style>", viewer.VIEWER_HTML, re.S).group(1)
    dark = re.search(r"^:root \{(.*?)^\}", css, re.S | re.M).group(1)
    media = re.search(r"@media \(prefers-color-scheme: light\) \{\s*:root:not\(\[data-theme=\"dark\"\]\) \{(.*?)\n  \}\n\}", css, re.S).group(1)
    forced = re.search(r':root\[data-theme="light"\] \{(.*?)^\}', css, re.S | re.M).group(1)
    var = lambda block: dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))
    return var(dark), var(media), var(forced), css


class ThemeTests(unittest.TestCase):
    def tearDown(self):
        viewer.init_config(VAULT)

    def test_css_has_both_themes_and_color_scheme(self):
        dark, light, forced, css = css_blocks()
        self.assertIn("color-scheme: light dark;", css)
        self.assertIn('@media (prefers-color-scheme: light)', css)
        self.assertEqual(dark["--bg"], "#131313")                      # today's dark palette is unchanged
        self.assertEqual(dark["--ac"], "#e07050")
        self.assertEqual(light, forced)                                 # media query and forced light agree
        self.assertEqual(set(light), set(dark) - set())                 # same variable names in both themes
        for mode in ("online", "offline", "export"):
            html = viewer.render_page(mode, {"notes": {}})
            self.assertIn('<meta name="color-scheme" content="light dark">', html)

    def test_every_var_used_is_defined_in_both_themes(self):
        dark, light, _, css = css_blocks()
        js = viewer.VIEWER_HTML
        used = set(re.findall(r"var\((--[\w-]+)", js)) | set(re.findall(r'cssVar\("(--[\w-]+)"\)', js))
        used.discard("--co")                 # per-callout variable, set on the element itself
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(dark)), [])
        self.assertEqual(sorted(used - set(light)), [])
        # --co / --st style per-element variables are set where they are used, not in :root
        self.assertEqual(sorted(set(dark) - set(light)), [])

    def test_light_theme_contrast(self):
        _, light, _, _ = css_blocks()
        bg = light["--bg"]
        pairs = [("--tx", "--bg"), ("--tx2", "--bg"), ("--tx2", "--sb"), ("--tx", "--sh"), ("--ac", "--bg"),
                 ("--code-fg", "--sh"), ("--pre-fg", "--pre-bg"), ("--err", "--bg"), ("--tx", "--sa"),
                 ("--tx2", "--sh"), ("--ac", "--sh")]
        pairs += [(k, "--bg") for k in light if k.startswith("--k-")]
        for fg, back in pairs:
            with self.subTest(fg=fg, bg=back):
                self.assertGreaterEqual(contrast(light[fg], light[back]), 4.5, f"{fg} on {back}")
        # callout / state text on its own 14% tint of the same hue over the page background
        for k in [k for k in light if k.startswith("--k-")]:
            fg = light[k].lstrip("#")
            f = [int(fg[i:i + 2], 16) for i in (0, 2, 4)] if len(fg) == 6 else [int(c * 2, 16) for c in fg]
            b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
            tint = "#" + "".join(f"{round(c * .14 + d * .86):02x}" for c, d in zip(f, b))
            with self.subTest(tint=k):
                self.assertGreaterEqual(contrast(light[k], tint), 4.5)

    def test_dark_palette_text_contrast_unchanged_or_better(self):
        dark, _, _, _ = css_blocks()
        self.assertGreaterEqual(contrast(dark["--tx"], dark["--bg"]), 9)

    def test_theme_setting_reaches_the_page_in_all_modes(self):
        for theme, attr, scheme in (("light", ' data-theme="light"', "light"), ("dark", ' data-theme="dark"', "dark"),
                                    ("system", "", "light dark")):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d) / "v"
                shutil.copytree(VAULT, root)
                (root / ".viewer.json").write_text(json.dumps({"theme": theme}), encoding="utf-8")
                viewer.init_config(root)
                for mode in ("online", "offline", "export"):
                    with self.subTest(theme=theme, mode=mode):
                        html = viewer.render_page(mode, {"notes": {}})
                        self.assertIn(f'<html lang="pl"{attr}>', html)
                        self.assertIn(f'<meta name="color-scheme" content="{scheme}">', html)
                        self.assertIn(f'"theme": "{theme}"', html)
                        self.assertNotIn("__THEMEATTR__", html)

    def test_default_is_system_and_invalid_falls_back_with_warning(self):
        viewer.init_config(VAULT)
        self.assertEqual(viewer.THEME, "system")
        self.assertIn('<html lang="pl">', viewer.render_page("offline"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            shutil.copytree(VAULT, root)
            (root / ".viewer.json").write_text(json.dumps({"theme": "sepia"}), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                viewer.init_config(root)
            self.assertEqual(viewer.THEME, "system")
            self.assertIn('"theme" must be one of system, dark, light', err.getvalue())
            self.assertIn('<html lang="pl">', viewer.render_page("online"))

    def test_graph_uses_theme_variables(self):
        js = viewer.VIEWER_HTML
        self.assertNotIn("#0d0d0d", js.split("</style>", 1)[1])
        self.assertIn("--graph-bg", js)
        self.assertIn("getComputedStyle", js)
        self.assertIn("(prefers-color-scheme: light)", js.split("</style>", 1)[1])
        self.assertIn('addEventListener("change"', js)
        self.assertNotIn("rgba(255,255,255", js.split("</style>", 1)[1])

    def test_no_hardcoded_neutrals_left_in_rules(self):
        """Colours that depend on the theme are variables; literals remain only in the palette blocks."""
        _, _, _, css = css_blocks()
        body = re.sub(r"^:root.*?^\}", "", css, flags=re.S | re.M)
        body = re.sub(r"@media \(prefers-color-scheme: light\) \{.*?\n  \}\n\}", "", body, flags=re.S)
        body = re.sub(r':root\[data-theme="light"\] \{.*?^\}', "", body, flags=re.S | re.M)
        for bad in ("#0d0d0d", "#111", "#1e1e1e", "#333", "#b8a8d8", "#c0cce0", "rgba(255,255,255", "#c05050"):
            self.assertNotIn(bad, body)

    def test_cli_warns_on_invalid_theme(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            shutil.copytree(VAULT, root)
            (root / ".viewer.json").write_text(json.dumps({"theme": "neon"}), encoding="utf-8")
            out = Path(d) / "o.html"
            r = subprocess.run([sys.executable, str(ROOT / "viewer.py"), "--vault", str(root), "--export", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('"theme" must be one of', r.stderr)
            self.assertIn('<html lang="pl">', out.read_text(encoding="utf-8"))


# ── Light-theme colours: a Python mirror of the page's colour maths (keep it parallel to darkenTo() etc.) ──
LIGHT_BG, LIGHT_SH = "#fbfbfa", "#e9e9e5"


def hex_rgb(h):
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", h.strip())
    if not m:
        return None
    s = m.group(1)
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return [int(s[i:i + 2], 16) for i in (0, 2, 4)]


def js_round(x):                       # Math.round: halves go up (Python's round() goes to even)
    return int(x + 0.5)


def rgb_hex(c):
    return "#" + "".join(f"{max(0, min(255, js_round(v))):02x}" for v in c)


def contrast_rgb(a, b):
    def lum(c):
        ch = [v / 255 for v in c]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    x, y = lum(a), lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def darken_to(hex_, bg_hex, minimum):
    c, bg = hex_rgb(hex_), hex_rgb(bg_hex)
    if c is None or bg is None:
        return hex_
    for i in range(51):
        t = [js_round(v * (1 - i * 0.02)) for v in c]
        if contrast_rgb(t, bg) >= minimum:
            return rgb_hex(t)
    return "#000000"


PALETTE = ["#5b9bd5", "#4ecdc4", "#e07050", "#c8980a", "#9b8bdb", "#68b068", "#d4709a", "#7fa6c9"]


def fnv1a(s):                                # mirror of hash32() on the page (UTF-16 code units)
    h = 0x811c9dc5
    for u in struct.unpack(f"<{len(s.encode('utf-16-le')) // 2}H", s.encode("utf-16-le")):
        h = ((h ^ u) * 0x01000193) & 0xFFFFFFFF
    return h


def derived_light_badge(base):
    c = hex_rgb(base)
    bg = rgb_hex([v * 0.15 + w * 0.85 for v, w in zip(c, hex_rgb(LIGHT_SH))])
    return bg, darken_to(base, bg, 4.5)


class LightColourTests(unittest.TestCase):
    """bg_light / fg_light / types_light: config reaches the page, and the derived colours are readable."""
    CFG = {"folders": {"k": {"bg": "#0a2020", "fg": "#4ecdc4", "letter": "K", "bg_light": "#d8efec", "fg_light": "#086860"}},
           "types": {"wiedza": "#4ecdc4", "x": "#888"}, "types_light": {"wiedza": "#086860"}}

    def tearDown(self):
        viewer.init_config(VAULT)

    def test_light_config_reaches_the_page_in_all_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            shutil.copytree(VAULT, root)
            (root / ".viewer.json").write_text(json.dumps(self.CFG), encoding="utf-8")
            viewer.init_config(root)
            for mode in ("online", "offline", "export"):
                with self.subTest(mode=mode):
                    html = viewer.render_page(mode, {"notes": {}})
                    self.assertIn('"bg_light": "#d8efec"', html)
                    self.assertIn('"fg_light": "#086860"', html)
                    self.assertIn('const TCL = {"wiedza": "#086860"}', html)
                    self.assertNotIn("__TCL__", html)

    def test_types_light_defaults_to_empty_and_ignores_garbage(self):
        viewer.init_config(VAULT)
        self.assertIn("const TCL = {};", viewer.render_page("offline"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            shutil.copytree(VAULT, root)
            (root / ".viewer.json").write_text(json.dumps({"types_light": ["no"]}), encoding="utf-8")
            viewer.init_config(root)
            self.assertIn("const TCL = {};", viewer.render_page("offline"))

    def test_repo_viewer_json_light_values_are_readable(self):
        cfg = json.loads((ROOT / "viewer.json").read_text(encoding="utf-8"))
        for name, f in cfg["folders"].items():
            with self.subTest(folder=name, kind="explicit"):
                self.assertGreaterEqual(contrast_rgb(hex_rgb(f["fg_light"]), hex_rgb(f["bg_light"])), 4.5)
            with self.subTest(folder=name, kind="derived"):     # what the page derives when bg_light/fg_light are absent
                bg, fg = derived_light_badge(f["fg"] if f["fg"].startswith("#") else f["bg"])
                self.assertGreaterEqual(contrast_rgb(hex_rgb(fg), hex_rgb(bg)), 4.5)
        for t, col in cfg["types_light"].items():
            with self.subTest(type=t):
                self.assertGreaterEqual(contrast_rgb(hex_rgb(col), hex_rgb(LIGHT_BG)), 3)
        for t, col in cfg["types"].items():
            with self.subTest(type=t, kind="darkened fallback"):
                self.assertGreaterEqual(contrast_rgb(hex_rgb(darken_to(col, LIGHT_BG, 3)), hex_rgb(LIGHT_BG)), 3)
        self.assertGreaterEqual(contrast_rgb(hex_rgb(darken_to("#888", LIGHT_BG, 3)), hex_rgb(LIGHT_BG)), 3)

    def test_page_constants_match_the_css_palette(self):
        _, light, _, _ = css_blocks()
        js = page_script("offline")
        self.assertIn(f'LIGHT_BG = "{light["--bg"]}"', js)
        self.assertIn(f'LIGHT_SH = "{light["--sh"]}"', js)

    @unittest.skipUnless(NODE, "node not available")
    def test_js_matches_the_python_mirror(self):
        colours = ["#888", "#888888", "#4ecdc4", "#5b9bd5", "#e07050", "#c8980a", "#9b8bdb", "#68b068", "#fff", "#000", "#777", "#555", "#d4709a"]
        out = run_page_js(f"""
          const cols = {json.dumps(colours)};
          console.log(JSON.stringify({{
            dark: cols.map(c => darkenTo(c, LIGHT_BG, 3)),
            badge: cols.map(c => derivedLightBadge(c)),
          }}));""")
        self.assertEqual(out["dark"], [darken_to(c, LIGHT_BG, 3) for c in colours])
        self.assertEqual([[b["bg"], b["fg"]] for b in out["badge"]], [list(derived_light_badge(c)) for c in colours])
        for c in colours:
            bg, fg = derived_light_badge(c)
            self.assertGreaterEqual(contrast_rgb(hex_rgb(fg), hex_rgb(bg)), 4.5, c)

    @unittest.skipUnless(NODE, "node not available")
    def test_dark_theme_is_untouched_and_light_theme_uses_the_new_keys(self):
        r = run_page_js("""
          const fm = {k: {bg: "#0a2020", fg: "#4ecdc4", letter: "K", bg_light: "#d8efec", fg_light: "#086860"},
                      plain: {bg: "#102030", fg: "#5b9bd5", letter: "D"}, onlybg: {bg: "#181818", letter: "M"}};
          const tc = {wiedza: "#4ecdc4", grey: "#888"}, tcl = {wiedza: "#086860"};
          console.log(JSON.stringify({
            darkK: folderBadge("k", false, fm), lightK: folderBadge("k", true, fm),
            darkPlain: folderBadge("plain", false, fm), lightPlain: folderBadge("plain", true, fm),
            lightOnlyBg: folderBadge("onlybg", true, fm),
            darkMissing: folderBadge("zzz", false, fm), lightMissing: folderBadge("zzz", true, fm),
            tDark: [tcol("wiedza", false, tc, tcl), tcol("grey", false, tc, tcl), tcol("", false, tc, tcl)],
            tLight: [tcol("wiedza", true, tc, tcl), tcol("grey", true, tc, tcl), tcol("", true, tc, tcl)],
            alpha: [hexAlpha("#888", "dd"), hexAlpha("#4ecdc4", "44"), hexAlpha("var(--x)", "44")],
          }));""")
        self.assertEqual(r["darkK"], {"bg": "#0a2020", "fg": "#4ecdc4", "letter": "K"})           # exactly as configured
        self.assertEqual(r["lightK"], {"bg": "#d8efec", "fg": "#086860", "letter": "K"})
        self.assertEqual(r["darkPlain"], {"bg": "#102030", "fg": "#5b9bd5", "letter": "D"})
        bg, fg = derived_light_badge("#5b9bd5")
        self.assertEqual(r["lightPlain"], {"bg": bg, "fg": fg, "letter": "D"})
        self.assertNotEqual(r["lightPlain"]["bg"], "#102030")                                  # no dark square in light mode
        acc = PALETTE[fnv1a("zzz") % len(PALETTE)]                                                # generated for unconfigured folders
        self.assertEqual(r["darkMissing"], {"bg": rgb_hex([v * 0.2 + w * 0.8 for v, w in zip(hex_rgb(acc), hex_rgb("#191919"))]),
                                            "fg": acc, "letter": "Z"})
        bg, fg = derived_light_badge(acc)
        self.assertEqual(r["lightMissing"], {"bg": bg, "fg": fg, "letter": "Z"})
        self.assertEqual(r["tDark"], ["#4ecdc4", "#888", "#888"])
        self.assertEqual(r["tLight"][0], "#086860")                                              # types_light wins
        self.assertEqual(r["tLight"][1], darken_to("#888", LIGHT_BG, 3))                          # kept, darkened to >= 3:1
        self.assertEqual(r["alpha"], ["#888888dd", "#4ecdc444", "var(--x)"])


DEEP = ROOT / "tests" / "fixtures" / "vault-deep"


class NestedTreeTests(unittest.TestCase):
    """The sidebar tree is built in the page from notes[*].path (/api/vault is unchanged)."""

    @classmethod
    def setUpClass(cls):
        cls.s = Server(vault=DEEP)

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        viewer.init_config(VAULT)

    def vault_json(self):
        st, _, body = self.s.req("GET", "/api/vault")
        self.assertEqual(st, 200)
        return json.loads(body)

    def test_every_note_path_appears_exactly_once_in_the_tree_data(self):
        data = self.vault_json()
        paths = [n["path"] for sec in data["folders"] for n in sec["notes"]]
        on_disk = sorted(p.relative_to(DEEP.resolve()).as_posix() for p in DEEP.resolve().rglob("*.md"))
        self.assertEqual(sorted(paths), on_disk)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(any(p.count("/") >= 3 for p in paths))                 # at least 3 folder levels
        self.assertEqual(set(data), {"generated", "notes", "folders", "stems", "files", "file_paths"})   # API unchanged

    @unittest.skipUnless(NODE, "node not available")
    def test_build_tree_nesting_order_counts_and_labels(self):
        data = self.vault_json()
        r = run_page_js(f"""
          const roots = buildTree({json.dumps(data["folders"])});
          const strip = n => ({{path: n.path, label: n.label, count: n.count,
            folders: n.folders.map(strip), notes: n.notes.map(x => x.name)}});
          console.log(JSON.stringify(roots.map(strip)));""", export_data=None)
        by = {n["path"]: n for n in r}
        self.assertEqual([n["path"] for n in r], ["(vault root)", "00-inbox", "10-knowledge", "20-projects"])   # server order kept
        k = by["10-knowledge"]
        self.assertEqual(k["label"], "knowledge")                              # number prefix stripped
        self.assertEqual(k["count"], 7)                                         # recursive
        # sub-folders first, number-aware and case-insensitive: 2-first < 10-second < deep < żółw
        self.assertEqual([f["path"] for f in k["folders"]],
                         ["10-knowledge/2-first", "10-knowledge/10-second", "10-knowledge/deep", "10-knowledge/żółw"])
        self.assertEqual([f["label"] for f in k["folders"]], ["first", "second", "deep", "żółw"])   # prefix stripped at every level
        self.assertEqual(k["notes"], ["Alpha"])
        deep = k["folders"][2]
        self.assertEqual((deep["count"], deep["notes"], [f["path"] for f in deep["folders"]]),
                         (3, ["Beta"], ["10-knowledge/deep/nested"]))
        nested = deep["folders"][0]
        self.assertEqual((nested["notes"], nested["count"]), (["Gamma"], 2))
        self.assertEqual(nested["folders"][0]["path"], "10-knowledge/deep/nested/more")
        self.assertEqual(nested["folders"][0]["notes"], ["Delta"])
        # two notes with the same name in different sub-folders stay two distinct rows
        p = by["20-projects"]
        self.assertEqual([f["path"] for f in p["folders"]], ["20-projects/eta", "20-projects/Zeta"])
        self.assertEqual(p["count"], 2)
        self.assertEqual(by["(vault root)"]["notes"], ["Root"])                 # vault root stays its own group
        self.assertEqual(by["(vault root)"]["folders"], [])

    @unittest.skipUnless(NODE, "node not available")
    def test_tree_html_state_aria_and_prune(self):
        data = self.vault_json()
        r = run_page_js(f"""
          const roots = buildTree({json.dumps(data["folders"])});
          const ctx = {{expanded: new Set(["10-knowledge", "10-knowledge/deep"]), active: "10-knowledge/deep/Beta.md", light: false}};
          const html = roots.map(n => folderHtml(n, 0, ctx)).join("");
          const live = [...folderPaths(roots)];
          console.log(JSON.stringify({{html, live}}));""")
        html = r["html"]
        self.assertIn('class="fhd open" role="treeitem" aria-expanded="true" aria-level="1" tabindex="-1" data-fpath="10-knowledge"', html)
        self.assertIn('data-fpath="10-knowledge/deep"', html)
        self.assertRegex(html, r'class="fhd" role="treeitem" aria-expanded="false" aria-level="3" tabindex="-1" data-fpath="10-knowledge/deep/nested"')
        self.assertIn('<div class="fnotes" role="group" hidden>', html)         # collapsed folders hide their group
        self.assertIn('class="ni active" role="treeitem" aria-level="3" aria-selected="true"', html)
        self.assertEqual(html.count('data-path="'), 11)                          # every note exactly once
        self.assertEqual(html.count("aria-selected=\"true\""), 1)
        self.assertIn('class="folder sub" role="none" style="--depth:1"', html)
        self.assertIn("10-knowledge/deep/nested/more", r["live"])
        self.assertNotIn("90-gone", r["live"])

    @unittest.skipUnless(NODE, "node not available")
    def test_two_thousand_notes_build_and_render_quickly(self):
        r = run_page_js("""
          const notes = [];
          for (let i = 0; i < 2000; i++) notes.push({path: `${i % 5}0-top/sub${i % 20}/deeper${i % 7}/Note ${i}.md`, name: `Note ${i}`, type: i % 3 ? "x" : ""});
          const secs = [0, 1, 2, 3, 4].map(k => ({folder: `${k}0-top`, notes: notes.filter(n => n.path.startsWith(`${k}0-top/`))}));
          const ctx = {expanded: new Set(), active: null, light: false};
          const t0 = Date.now();
          const html = buildTree(secs).map(n => folderHtml(n, 0, ctx)).join("");
          console.log(JSON.stringify({ms: Date.now() - t0, rows: (html.match(/class="ni/g) || []).length}));""")
        self.assertEqual(r["rows"], 2000)
        self.assertLess(r["ms"], 100)

    def test_page_keeps_the_existing_class_names_and_has_the_nested_css(self):
        html = viewer.render_page("offline")
        css = html.split("<style>")[1].split("</style>")[0]
        for sel in (".folder", ".fhd", ".fnotes", ".ni", ".folder.sub > .fnotes"):
            self.assertIn(sel, css)
        js = page_script("offline")
        self.assertIn("sbv-tree:", js)                                           # state is kept per vault title in localStorage
        self.assertIn('"title": "Deep Fixture"', html)


class DefaultBadgeTests(unittest.TestCase):
    """Unconfigured folders and types get generated, stable badges and colours (instead of grey "?" / #888)."""

    @unittest.skipUnless(NODE, "node not available")
    def test_letter_is_unicode_aware_and_ignores_the_number_prefix(self):
        names = ["10-knowledge", "01 daily", "żółw", "ćma", "2024", "10-", "Ünï", "(vault root)", "99-ñandú", "---"]
        r = run_page_js(f"console.log(JSON.stringify({json.dumps(names)}.map(n => folderBadge(n, false, {{}}).letter)));")
        self.assertEqual(r, ["K", "D", "Ż", "Ć", "2", "1", "Ü", "~", "Ñ", "?"])

    @unittest.skipUnless(NODE, "node not available")
    def test_vault_root_gets_its_tilde_and_colour_is_stable_per_path(self):
        r = run_page_js("""
          const a = folderBadge("10-knowledge", false, {}), b = folderBadge("10-knowledge", false, {});
          const sub = folderBadge("10-knowledge/deep", false, {}), sub2 = folderBadge("10-knowledge/deep", false, {});
          const paths = Array.from({length: 40}, (_, i) => "folder-" + i);
          console.log(JSON.stringify({a, b, sub, sub2, root: folderBadge("(vault root)", true, {}),
            fgs: paths.map(p => folderBadge(p, false, {}).fg)}));""")
        self.assertEqual(r["a"], r["b"])
        self.assertEqual(r["sub"], r["sub2"])
        self.assertEqual(r["root"]["letter"], "~")
        self.assertEqual(r["a"]["fg"], PALETTE[fnv1a("10-knowledge") % 8])          # same hash as the Python mirror
        self.assertEqual(r["sub"]["fg"], PALETTE[fnv1a("10-knowledge/deep") % 8])
        self.assertGreater(len(set(r["fgs"])), 4)                                   # the palette really varies

    @unittest.skipUnless(NODE, "node not available")
    def test_configured_folder_entries_still_win(self):
        r = run_page_js("""
          const fm = {"10-knowledge": {bg: "#0a2020", fg: "#4ecdc4", letter: "K"}};
          console.log(JSON.stringify({d: folderBadge("10-knowledge", false, fm), partial: folderBadge("x", false, {x: {letter: "Q"}})}));""")
        self.assertEqual(r["d"], {"bg": "#0a2020", "fg": "#4ecdc4", "letter": "K"})
        self.assertEqual(r["partial"]["letter"], "Q")
        self.assertEqual(r["partial"]["fg"], PALETTE[fnv1a("x") % 8])

    @unittest.skipUnless(NODE, "node not available")
    def test_unknown_types_are_coloured_stably_and_readable_in_light(self):
        types = ["wiedza", "projekt", "person", "meeting", "weekly", "zażółć", "a", "b", "c", "d"]
        r = run_page_js(f"""
          const ts = {json.dumps(types)};
          console.log(JSON.stringify({{dark: ts.map(t => tcol(t, false, {{}}, {{}})), again: ts.map(t => tcol(t, false, {{}}, {{}})),
            light: ts.map(t => tcol(t, true, {{}}, {{}})), cfgd: tcol("meta", false, {{meta: "#888"}}, {{}}), empty: tcol("", false, {{}}, {{}}),
            proto: tcol("constructor", false, {{}}, {{}})}}));""")
        self.assertEqual(r["dark"], r["again"])
        for t, col in zip(types, r["dark"]):
            self.assertEqual(col, PALETTE[fnv1a(t) % 8], t)
            c = hex_rgb(col)
            self.assertFalse(c[0] == c[1] == c[2], f"{t} is grey")                 # not grey
        self.assertGreater(len(set(r["dark"])), 3)
        for col in r["light"]:
            self.assertGreaterEqual(contrast_rgb(hex_rgb(col), hex_rgb(LIGHT_BG)), 3, col)
        self.assertEqual(r["cfgd"], "#888")                                          # configured types win
        self.assertEqual(r["empty"], "#888")                                         # an empty type stays grey
        self.assertEqual(r["proto"], PALETTE[fnv1a("constructor") % 8])              # prototype keys are not config entries

    def test_palette_light_variants_meet_the_threshold(self):
        for col in PALETTE:
            with self.subTest(col=col):
                self.assertGreaterEqual(contrast_rgb(hex_rgb(darken_to(col, LIGHT_BG, 3)), hex_rgb(LIGHT_BG)), 3)
                bg, fg = derived_light_badge(col)
                self.assertGreaterEqual(contrast_rgb(hex_rgb(fg), hex_rgb(bg)), 4.5)
        self.assertIn("const PALETTE = " + json.dumps(PALETTE), page_script("offline"))


class IndexTests(unittest.TestCase):
    def test_resolution_forms(self):
        viewer.init_config(VAULT)
        idx = viewer.VaultIndex()
        a = "10-knowledge/Alpha.md"
        for t in ("Alpha", "alpha", "10-knowledge/Alpha", "10-knowledge/Alpha.md", "/10-knowledge/alpha"):
            self.assertEqual(idx.resolve(t), a, t)
        self.assertIsNone(idx.resolve("Does Not Exist"))
        self.assertEqual(viewer.extract_wikilinks(r"[[A\|x]] [[B#H|y]] ![[C]]"), ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
