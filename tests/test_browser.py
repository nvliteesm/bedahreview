"""Headless Chrome checks of the installed-app shell: manifest, service worker, Share-menu pre-fill, results board.

Skipped when Chrome is not installed. The page is served by the real server with a stub model; a small script is
appended to a temp copy of index.html and leaves what the page did in window.__probe. Chrome is driven over its
DevTools socket in real time (a service worker needs real time to install), with a tiny stdlib WebSocket client.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from bedahreview import web

CHROME = next((p for p in (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "",
) if p and os.path.exists(p)), None)


def _stub_classify(reviews, product=None):
    out = []
    for r in reviews:
        base = {"id": r["id"], "variant": r["variant"] or None, "cause": "none", "check_first": False, "proof_quote": r["text"] or None,
                "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"}
        if "koyak" in r["text"]:
            kit = {"why": "Likely transit damage.", "steps": ["Ask for photos."], "chat_to_buyer": "Halo Kak, mohon maaf 🙏", "public_reply": "Mohon maaf, Kak.", "report_text": None}
            out.append(dict(base, bucket="fight", theme="Packaging / damaged on arrival", cause="courier", check_first=True, fight=kit))
        elif any(w in r["text"] for w in ("ijo", "hitem", "hijau")):
            out.append(dict(base, bucket="fix", theme="Colour vs photo", cause="listing", fix_suggestion_id="Ganti foto varian."))
        elif not r["text"]:
            out.append(dict(base, bucket="ignore", theme="No text"))
        else:
            out.append(dict(base, bucket="keep", theme="Praise: quality / material", variant=None))
    return out


PROBE = """
<script>
window.__probe = (async () => {
  const out = {};
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  out.swApi = "serviceWorker" in navigator;
  out.sw = await Promise.race([
    navigator.serviceWorker.ready.then((reg) => "ready:" + (reg.active ? reg.active.state : "none")),
    wait(20000).then(() => "timeout:" + document.documentElement.dataset.sw),
  ]);
  out.pageFlag = document.documentElement.dataset.sw;
  const link = document.querySelector('link[rel=manifest]');
  const m = await (await fetch(link.href)).json();
  out.manifest = { display: m.display, theme: m.theme_color, share: m.share_target && m.share_target.method, icons: m.icons.length };
  out.prefill = document.getElementById("paste").value;
  out.search = location.search;
  out.sharedNote = !document.getElementById("shared-note").hidden;
  out.tabSelected = document.getElementById("intab-paste").getAttribute("aria-selected");
  out.lang = document.documentElement.lang;
  if (location.hash === "#flow") {
    document.getElementById("sample").click();
    document.getElementById("run").click();
    for (let i = 0; i < 40 && document.getElementById("view-results").hidden; i++) await wait(100);
    out.tabs = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    out.cards = document.querySelectorAll("#panel-fix .card").length;
    out.chips = [...document.querySelectorAll("#panel-fight .chip")].map((c) => c.textContent);
    out.keep = document.querySelectorAll("#panel-keep blockquote").length;
    const cache = await caches.open("bedahreview-shell-v1");
    out.cachedPaths = (await cache.keys()).map((r) => new URL(r.url).pathname);
    out.copyButtons = document.querySelectorAll(".copy").length;
    out.fixQuote = (document.querySelector("#panel-fix blockquote") || {}).textContent;
    document.getElementById("lang").click();
    out.enTabs = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    out.enLang = document.documentElement.lang;
    out.draftStillBahasa = (document.querySelector("#panel-fix .bahasa") || {}).textContent;
  }
  if (location.hash === "#desktop") {
    out.cols = getComputedStyle(document.querySelector(".workspace")).gridTemplateColumns.split(" ").length;
    out.hostBefore = document.getElementById("rival-body").parentNode.id;  // the competitor panel has its own column, usable before any analysis
    out.formBefore = !!document.getElementById("rival-url");
    document.getElementById("sample").click();
    document.getElementById("run").click();
    for (let i = 0; i < 40 && document.getElementById("view-results").hidden; i++) await wait(100);
    out.hostAfter = document.getElementById("rival-body").parentNode.id;
    out.rivalTabHidden = document.getElementById("tab-rival").hidden;
    out.board = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    out.chip = document.getElementById("pchip").hidden;
    const before = document.documentElement.dataset.theme || "";
    document.getElementById("theme").click();
    out.themeChanged = (document.documentElement.dataset.theme || "") !== before;
  }

  if (location.hash === "#fixes") {
    const KEY = "bedahreview-fixes-v1";
    const mk = (id, theme, bucket, variant, fake, text) => ({ id, theme, bucket, variant, text, likely_fake: { flag: !!fake } });
    const items = [mk("1", "Sizing & fit", "fix", "Putih, L", 0, "rahasia satu"), mk("2", "Sizing & fit", "fix", "Hitam, M", 0, "rahasia dua"), mk("3", "Sizing & fit", "fix", "Putih, S", 1, "rahasia palsu"),
      mk("4", "Colour vs photo", "fix", "Putih, L", 0, "rahasia empat"), mk("5", "Praise: quality / material", "keep", null, 0, "rahasia lima")];
    out.fx = { all: countTheme(items, "Sizing & fit", ""), putih: countTheme(items, "Sizing & fit", "Putih"), colour: countTheme(items, "Colour vs photo", "") };
    const fix = makeFix({ theme: "Sizing & fit", variant: "" }, "size chart diganti", { kind: "paste", title: "Kaos  Boxy" }, items);
    out.fx.made = [fix.before.count, fix.before.total, fix.product, fix.date.length];
    out.fx.noText = JSON.stringify(fix).indexOf("rahasia") < 0;
    out.fx.same = applyAnalysis([fix], "kaos boxy", items).changed.length;  // the very reviews it was marked on: nothing to compare
    const later = [mk("1", "Sizing & fit", "fix", "Putih, L", 0, "baru satu"), mk("2", "Praise: quality / material", "keep", null, 0, "baru dua"), mk("3", "Praise: quality / material", "keep", null, 0, "baru tiga")];
    const next = applyAnalysis([fix], "kaos boxy", later);
    out.fx.later = [next.changed.length, next.fixes[0].after.count, next.fixes[0].after.total, JSON.stringify(next.fixes).indexOf("baru") < 0];
    out.fx.otherProduct = applyAnalysis([fix], "kemeja", later).changed.length;
    out.fx.sameFix = [sameFix(fix, "kaos boxy", "Sizing & fit", null), sameFix(fix, "kaos boxy", "Sizing & fit", "Putih")];
    const good = JSON.stringify({ fixes: [next.fixes[0], { id: "x", theme: "", date: "nope" }, "junk"] });
    const merged = mergeImport([fix], good);
    out.fx.imp = [merged.added, merged.skipped, merged.fixes.length, merged.fixes[0].after.count];
    out.fx.badImport = [mergeImport([], "{not json").error, mergeImport([], "{}").error, mergeImport([], "x".repeat(600000)).error];
    out.fx.longNote = cleanFix(Object.assign({}, fix, { note: "n".repeat(500) })).note.length;
    out.fx.negative = cleanFix(Object.assign({}, fix, { before: { count: 5, total: 2 } }));
    const realSet = Storage.prototype.setItem;
    Storage.prototype.setItem = function () { throw new Error("blocked"); };
    out.fx.saveWhenBlocked = [saveFixes([fix]), loadFixes().length];  // the list survives in memory and says it could not be stored
    Storage.prototype.setItem = realSet;
    saveFixes([]);
  }
  if (location.hash === "#fixflow") {
    localStorage.removeItem("bedahreview-fixes-v1");
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    const resultsUp = () => !document.getElementById("view-results").hidden;
    document.getElementById("sample").click();
    document.getElementById("run").click();
    await until(resultsUp);
    const card = document.querySelector("#panel-fix .card");
    const noNull = card.textContent.indexOf("null") < 0;
    [...card.querySelectorAll("button")].find((b) => b.textContent === "Tandai sudah diperbaiki").click();
    card.querySelector("input[type=text]").value = "foto varian diganti";
    [...card.querySelectorAll("button")].find((b) => b.textContent === "Simpan").click();
    const raw = localStorage.getItem("bedahreview-fixes-v1");
    const saved = JSON.parse(raw)[0];
    out.fl = { noNull, n: JSON.parse(raw).length, theme: saved.theme, variant: saved.variant, note: saved.note, before: [saved.before.count, saved.before.total], noText: raw.indexOf("ijo") < 0 && raw.indexOf("koyak") < 0 };
    out.fl.cardAfterSave = document.querySelector("#panel-fix .card .track").textContent.indexOf("Ditandai diperbaiki") >= 0;
    out.fl.badge = document.getElementById("fixes-count").textContent;
    out.fl.sameAgain = "";
    document.getElementById("nav-input").click();
    document.getElementById("paste").value = "★★☆☆☆  Putih, M   warna tulisan ijo jadi hitam" + String.fromCharCode(10) + "★★★★★  bagus sekali bahannya";
    document.getElementById("run").click();
    await until(() => !document.getElementById("view-loading").hidden || resultsUp());
    await until(resultsUp);
    out.fl.comparedNote = document.getElementById("notes").textContent.indexOf("dibandingkan") >= 0;
    const after = JSON.parse(localStorage.getItem("bedahreview-fixes-v1"))[0].after;
    out.fl.after = [after.count, after.total];
    document.getElementById("fixes-btn").click();
    const list = document.getElementById("fixes-list").textContent;
    out.fl.listShows = [list.indexOf("Sebelum: 2 dari 5") >= 0, list.indexOf("1 dari 2") >= 0, list.indexOf("foto varian diganti") >= 0, list.indexOf("kurang dari 10") >= 0];
    const del = () => [...document.querySelectorAll("#fixes-list button")].find((b) => b.textContent === "Hapus" || b.textContent === "Yakin hapus?");
    del().click(); del().click();
    out.fl.afterDelete = [JSON.parse(localStorage.getItem("bedahreview-fixes-v1")).length, document.getElementById("fixes-count").hidden];
  }
  return out;
})();
</script>
"""


class _Socket:
    """Just enough WebSocket (RFC 6455) to send DevTools commands and read replies."""

    def __init__(self, ws_url):
        parts = urlparse(ws_url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (parts.path, parts.hostname, parts.port, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        assert b" 101 " in head.split(b"\r\n")[0], head
        self.next_id = 0

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("DevTools socket closed")
            buf += chunk
        return buf

    def _frame(self):
        b1, b2 = self._read(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        return b1 & 0x0F, self._read(length)

    def call(self, method, **params):
        self.next_id += 1
        data = json.dumps({"id": self.next_id, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        message = b""
        while True:
            opcode, payload = self._frame()
            message = message + payload if opcode == 0 else payload
            if opcode in (0, 1):
                reply = json.loads(message)
                if reply.get("id") == self.next_id:
                    return reply


def _read_probe(profile, url):
    port_file = Path(profile) / "DevToolsActivePort"
    for _ in range(100):
        if port_file.exists():
            break
        time.sleep(0.1)
    port = port_file.read_text().split()[0]
    page = None
    for _ in range(100):  # wait for the tab to exist and start loading the page
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%s/json/list" % port))
        page = next((t for t in tabs if t["type"] == "page" and t["url"].startswith("http://127.0.0.1")), None)
        if page:
            break
        time.sleep(0.1)
    ws = _Socket(page["webSocketDebuggerUrl"])
    expr = "typeof window.__probe === 'undefined' ? null : window.__probe"
    for _ in range(100):
        reply = ws.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        value = (reply.get("result") or {}).get("result", {}).get("value")
        if value is not None:
            return value
        time.sleep(0.2)
    raise AssertionError("the page never reported: " + json.dumps(reply)[:300])


@unittest.skipUnless(CHROME, "Chrome is not installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        static = Path(cls.tmp) / "static"
        shutil.copytree(web.STATIC, static)
        index = static / "index.html"
        index.write_text(index.read_text(encoding="utf-8").replace("</body>", PROBE + "</body>"), encoding="utf-8")
        cls.real_static, web.STATIC = web.STATIC, static
        cls.server = web.make_server(0, classify_fn=_stub_classify)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web.STATIC = cls.real_static
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def probe(self, path, width=500):
        profile = tempfile.mkdtemp(dir=self.tmp)
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0",
                                   "--user-data-dir=" + profile, "--window-size=%d,900" % width, url],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return _read_probe(profile, url)
        finally:
            chrome.terminate()
            chrome.wait(timeout=20)

    def test_manifest_service_worker_and_share_prefill(self):
        text = "★★☆☆☆  Hitam, L   Pas sampe koyak bajunya"
        out = self.probe("/?title=&text=" + quote(text) + "&url=")
        self.assertEqual(out["manifest"], {"display": "standalone", "theme": "#0f6b5a", "share": "GET", "icons": 3})
        self.assertIn(out["sw"], ("ready:activating", "ready:activated"))
        self.assertEqual(out["pageFlag"], "ready")
        self.assertEqual(out["prefill"], text)  # the shared text is in the box
        self.assertEqual(out["search"], "")  # and is not left in the address bar
        self.assertTrue(out["sharedNote"])
        self.assertEqual((out["tabSelected"], out["lang"]), ("true", "id"))

    def test_share_with_title_and_text_joins_them(self):
        out = self.probe("/?title=" + quote("Kaos Boxy") + "&text=" + quote("bagus banget"))
        self.assertEqual(out["prefill"], "Kaos Boxy\nbagus banget")

    def test_shared_link_only_is_not_treated_as_a_review(self):
        out = self.probe("/?text=" + quote("https://shopee.co.id/Kaos-i.1.2"))
        self.assertEqual(out["prefill"], "")
        self.assertTrue(out["sharedNote"])

    def test_no_share_leaves_the_box_empty(self):
        out = self.probe("/")
        self.assertEqual((out["prefill"], out["sharedNote"]), ("", False))
        self.assertTrue(out["sw"].startswith("ready:"), out["sw"])

    def test_results_board_and_language_switch(self):
        out = self.probe("/#flow")
        self.assertEqual(out["tabs"][:3], ["2Perbaiki", "1Tanggapi", "0Abaikan"])  # a 2x2 board: Pertahankan is the fourth tab
        self.assertTrue(out["tabs"][3].endswith("Pertahankan"), out["tabs"])
        self.assertEqual(out["cards"], 1)  # the two colour complaints become one card
        self.assertIn("Kurir? Cek dulu", out["chips"])
        self.assertGreaterEqual(out["keep"], 1)  # praise sits in its own Pertahankan tab
        self.assertIn("/", out["cachedPaths"])  # the app shell is cached...
        self.assertFalse([p for p in out["cachedPaths"] if p.startswith("/api/")], out["cachedPaths"])  # ...reviews never are
        self.assertGreaterEqual(out["copyButtons"], 3)
        self.assertEqual(out["enTabs"][:3], ["2Fix it", "1Fight it", "0Ignore"])
        self.assertTrue(out["enTabs"][3].endswith("Keep"), out["enTabs"])
        self.assertEqual(out["enLang"], "en")
        self.assertEqual(out["draftStillBahasa"], "Ganti foto varian.")  # drafts stay Bahasa in English mode

    def test_wide_screen_puts_the_competitor_panel_in_its_own_column(self):
        out = self.probe("/#desktop", width=1300)
        self.assertEqual(out["cols"], 2)
        self.assertEqual((out["hostBefore"], out["formBefore"]), ("rival-home", True))
        self.assertEqual(out["hostAfter"], "rival-home")  # it stays put when results arrive
        self.assertTrue(out["rivalTabHidden"])
        self.assertEqual(out["board"][:2], ["2Perbaiki", "1Tanggapi"])  # the board texts are unchanged by the card layout
        self.assertFalse(out["chip"])  # the product chip shows once results are in
        self.assertTrue(out["themeChanged"])

    def test_fix_tracking_logic(self):
        fx = self.probe("/#fixes")["fx"]
        self.assertEqual(fx["all"], {"count": 2, "total": 4})  # the likely-fake review is left out of both numbers
        self.assertEqual(fx["putih"]["count"], 1)
        self.assertEqual(fx["colour"]["count"], 1)
        self.assertEqual(fx["made"][:3], [2, 4, "kaos boxy"])
        self.assertTrue(fx["noText"])
        self.assertEqual(fx["same"], 0)
        self.assertEqual(fx["later"], [1, 1, 3, True])
        self.assertEqual(fx["otherProduct"], 0)
        self.assertEqual(fx["sameFix"], [True, False])
        self.assertEqual(fx["imp"], [1, 2, 1, 1])  # one valid, two skipped; the same id replaces the old record
        self.assertEqual(fx["badImport"], ["bad", "bad", "big"])
        self.assertEqual(fx["longNote"], 200)
        self.assertIsNone(fx["negative"])  # more complaints than reviews cannot be true
        self.assertEqual(fx["saveWhenBlocked"], [False, 1])

    def test_marking_a_fix_then_analysing_new_reviews_shows_before_and_after(self):
        fl = self.probe("/#fixflow")["fl"]
        self.assertEqual((fl["n"], fl["theme"], fl["variant"], fl["note"], fl["before"]), (1, "Colour vs photo", "Putih", "foto varian diganti", [2, 5]))
        self.assertTrue(fl["noNull"])
        self.assertTrue(fl["noText"])  # only counts and the seller's own note are stored, no review text
        self.assertTrue(fl["cardAfterSave"])
        self.assertEqual(fl["badge"], "1")
        self.assertTrue(fl["comparedNote"])
        self.assertEqual(fl["after"], [1, 2])
        self.assertEqual(fl["listShows"], [True, True, True, True])
        self.assertEqual(fl["afterDelete"], [0, True])


if __name__ == "__main__":
    unittest.main()
