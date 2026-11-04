"""Tests for viewer.py (stdlib unittest). Run: python3 -m unittest discover tests"""
import http.client
import json
import re
import shutil
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


class Server:
    def __init__(self, offline=False):
        viewer.init_config(VAULT)
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
        self.assertEqual(len(json.loads(b)["nodes"]), 7)
        st, _, _ = self.s.req("GET", "/vendor/marked.min.js")
        self.assertEqual(st, 404)

    def test_page_online_has_chat_and_cdn(self):
        _, h, b = self.s.req("GET", "/")
        self.assertNotIn("Content-Security-Policy", h)
        self.assertIn(b"cdn.jsdelivr.net", b)
        self.assertIn(b'"chat": true', b)

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

    def test_vendor_allow_list(self):
        for name in viewer.VENDOR_FILES:
            st, h, body = self.s.req("GET", "/vendor/" + name)
            self.assertEqual(st, 200)
            self.assertIn("javascript", h["Content-Type"])
            self.assertGreater(len(body), 1000)
        for bad in ("/vendor/../viewer.py", "/vendor/README.md", "/vendor/%2e%2e/viewer.py", "/vendor/"):
            self.assertEqual(self.s.req("GET", bad)[0], 404, bad)


class CliTests(unittest.TestCase):
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
        self.assertEqual(len(payload["notes"]), 7)
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
