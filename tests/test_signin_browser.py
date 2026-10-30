"""The sign-in screens, driven in headless Chrome. Supabase is replaced by a small stand-in defined before the page's own script,
so no network and no real account is involved. The server is the real one and checks a real signed test token."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from bedahreview import web
from bedahreview.account import TABLES, AccountAdmin
from bedahreview.auth import SupabaseAuth

try:
    from test_auth import URL, USER, FakeSupabase, new_key, token
    from test_browser import CHROME, _read_probe, _stub_classify
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, USER, FakeSupabase, new_key, token
    from tests.test_browser import CHROME, _read_probe, _stub_classify

FAKE_SUPABASE = """
<script>
window.__sb = { calls: [], session: null, profile: null, listeners: [], fixes: [], fixesError: false };
(function () {
  const hash = location.hash, user = { id: "user-1111-2222", email: "ani@example.com" };
  window.__stage = sessionStorage.getItem("bedahreview-deleted") ? 2 : 1;  // the delete test reloads the page once: stage 2 is the page after the deletion
  const syncing = ["#sync-first", "#sync-other", "#sync-down", "#sync-edit", "#sync-leave", "#delete"];
  if (["#intro", "#returning", "#expired", "#events", "#account"].concat(syncing).includes(hash)) window.__sb.session = { access_token: hash === "#expired" ? "junk" : "__TOKEN__", user };
  if (["#returning", "#expired", "#events", "#account"].concat(syncing).includes(hash)) window.__sb.profile = { user_id: user.id };
  const fixA = { id: "aaa1", theme: "Sizing & fit", variant: "Hitam", product: "kaos boxy retro", productLabel: "Kaos Boxy Retro", note: "size chart diganti", date: "2026-10-20", before: { count: 2, total: 5, sig: "s1", date: "" }, after: null, updated: 1000 };
  const rowB = { user_id: user.id, id: "bbb2", updated_at: "2026-10-25T10:00:00.000Z", theme: "Colour vs photo", variant: "Putih", product: "https://www.tokopedia.com/x/y", product_label: "Kaos Boxy", note: "foto diganti", fixed_on: "2026-10-25",
    before_count: 2, before_total: 4, before_sig: "s2", after_count: 1, after_total: 5, after_sig: "s3", after_on: "2026-10-28" };
  try {
    if (hash === "#sync-first") { localStorage.setItem("bedahreview-fixes-v1", JSON.stringify([fixA])); window.__sb.fixes = [rowB]; }
    if (hash === "#sync-other") { localStorage.setItem("bedahreview-fixes-v1", JSON.stringify([fixA])); localStorage.setItem("bedahreview-fixes-owner", "another-user"); }
    if (hash === "#sync-down") { localStorage.setItem("bedahreview-fixes-v1", JSON.stringify([fixA])); window.__sb.fixesError = true; }
    if (hash === "#sync-edit" || hash === "#sync-leave") window.__sb.fixes = [rowB];
    if (hash === "#delete" && window.__stage === 1) {
      localStorage.setItem("bedahreview-fixes-v1", JSON.stringify([fixA])); localStorage.setItem("bedahreview-device", "3fa9c2d41b7e8a06c5d1e2f3"); localStorage.setItem("bedahreview-fixes-owner", user.id);
    }
    if (hash === "#delete" && window.__stage === 2) { window.__sb.session = null; window.__sb.profile = null; }
    window.__back = hash === "#free-google" && !!sessionStorage.getItem("probe-back");  // the free-analysis test reloads once, as if back from Google
    if (window.__back) { window.__sb.session = { access_token: "__TOKEN__", user }; window.__sb.profile = { user_id: user.id }; }
  } catch (e) { /* storage blocked */ }
  window.supabase = { createClient: (url, key, opts) => { window.__sb.opts = opts; return ({
    auth: {
      getSession: async () => ({ data: { session: window.__sb.session } }),
      onAuthStateChange: (cb) => { window.__sb.listeners.push(cb); return { data: { subscription: { unsubscribe() {} } } }; },
      signInWithOtp: async (a) => { window.__sb.calls.push(["otp", a.email]); return { error: null }; },
      verifyOtp: async (a) => {
        window.__sb.calls.push(["verify", a.email, a.token, a.type]);
        if (a.token !== "123456") return { data: {}, error: { code: "otp_expired", status: 403 } };
        const session = { access_token: "__TOKEN__", user };
        window.__sb.session = session; window.__sb.listeners.forEach((l) => l("SIGNED_IN", session));
        return { data: { session }, error: null };
      },
      signInWithOAuth: async (a) => { window.__sb.calls.push(["oauth", a.provider, a.options.redirectTo]); return { error: null }; },
      signOut: async () => { window.__sb.session = null; return { error: null }; },
    },
    from: (table) => table === "fixes" ? ({
      select: () => {
        window.__sb.calls.push(["select-fixes"]);
        const res = window.__sb.fixesError ? { data: null, error: { message: "paused" } } : { data: window.__sb.fixes.slice(), error: null };
        return { then: (a, b) => Promise.resolve(res).then(a, b) };
      },
      upsert: async (rows) => { window.__sb.calls.push(["upsert-fixes", rows]); rows.forEach((r) => { window.__sb.fixes = window.__sb.fixes.filter((x) => x.id !== r.id).concat(r); }); return { error: null }; },
      delete: () => ({ in: async (col, ids) => { window.__sb.calls.push(["delete-fixes", ids]); window.__sb.fixes = window.__sb.fixes.filter((x) => !ids.includes(x.id)); return { error: null }; } }),
    }) : ({
      select: () => ({ eq: () => ({ maybeSingle: async () => ({ data: window.__sb.profile, error: null }) }) }),
      upsert: async (row) => { window.__sb.calls.push(["upsert", table, row]); window.__sb.profile = { user_id: row.user_id }; return { error: null }; },
    }),
  }); } };
})();
</script>
"""

PROBE = """
<script>
window.__probe = (async () => {
  const out = {}, hash = location.hash;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (fn, n = 60) => { for (let i = 0; i < n; i++) { if (fn()) return true; await wait(100); } return false; };
  const byId = (id) => document.getElementById(id), vis = (id) => !byId(id).hidden;
  const calls = (kind) => window.__sb.calls.filter((c) => c[0] === kind);
  const paste = (text) => { const ev = new Event("paste", { bubbles: true, cancelable: true }); ev.clipboardData = { getData: () => text }; byId("otp-0").dispatchEvent(ev); };
  await until(() => document.body.classList.contains("ready"));

  if (hash === "#out") {
    await until(() => !byId("workspace").hidden);
    out.start = { gate: vis("gate"), app: !byId("workspace").hidden, signinBtn: vis("signin-btn"), acct: vis("acct") };
    byId("signin-btn").click();
    await until(() => vis("gate"));
    out.gate = vis("gate"); out.workspaceHidden = byId("workspace").hidden;
    out.title = byId("gate-h").textContent; out.google = byId("g-google").textContent.trim();
    out.consent = byId("g-choose").textContent.includes("Kami mencatat pemakaian per akun (tanpa isi ulasan) untuk riset produk ini.");
    out.codeLink = vis("g-codelink"); out.signinBtn = vis("signin-btn");
    out.noContactAsk = /hubungi|wawancara|contacted|interview/i.test(byId("gate").textContent);
    out.otp = [byId("otp-0").getAttribute("autocomplete"), byId("otp-0").getAttribute("inputmode"), byId("otp-1").getAttribute("autocomplete"), document.querySelectorAll("#otp input").length];
    byId("lang").click(); out.titleEn = byId("gate-h").textContent; byId("lang").click();
    byId("g-google").click(); await wait(100); out.oauth = calls("oauth")[0].slice(1);
    byId("g-email").value = "not an email"; byId("g-send").click(); out.badEmail = byId("g-error-text").textContent; out.otpCalls = calls("otp").length;
  }

  if (hash === "#otp") {
    await until(() => vis("gate"));
    byId("g-email").value = "ani@example.com"; byId("g-send").click();
    await until(() => vis("g-verify"));
    out.sub = byId("g-vsub").textContent; out.resend = byId("g-resend").textContent; out.resendDisabled = byId("g-resend").disabled;
    paste("000000");
    await until(() => !byId("g-error").hidden);
    out.wrong = byId("g-error-text").textContent;
    out.cleared = [...document.querySelectorAll("#otp input")].every((i) => i.value === "");
    paste("12 34-56");  // spaces and dashes are ignored
    await until(() => vis("g-intro"));
    out.intro = vis("g-intro"); out.verify = calls("verify").map((c) => c.slice(1));
    byId("g-skip").click();
    await until(() => !byId("workspace").hidden);
    out.app = !byId("workspace").hidden; out.gateGone = byId("gate").hidden;
    out.upserts = calls("upsert").map((c) => c[2]); out.logout = vis("logout-row"); out.signinBtn = vis("signin-btn");
  }

  if (hash === "#intro") {
    await until(() => vis("g-intro"));
    out.intro = vis("g-intro"); out.platforms = [...byId("g-platform").options].map((o) => o.value);
    out.noContactAsk = /hubungi|wawancara|contacted|interview|Boleh saya/i.test(byId("g-intro").textContent);
    byId("g-shop").value = "toko-saya"; byId("g-save").click();
    out.shopErr = byId("g-error-text").textContent; out.savedEarly = calls("upsert").length;
    byId("g-platform").value = "tokopedia"; byId("g-category").value = "fashion"; byId("g-shop").value = "https://www.tokopedia.com/tokosaya";
    byId("g-save").click();
    await until(() => !byId("workspace").hidden);
    out.row = calls("upsert")[0][2]; out.app = !byId("workspace").hidden;
  }

  if (hash === "#returning") {
    await until(() => !byId("workspace").hidden);
    out.intro = vis("g-intro"); out.upserts = calls("upsert").length;
    byId("sample").click(); byId("run").click();
    await until(() => !byId("view-results").hidden, 80);
    out.results = !byId("view-results").hidden; out.tabs = document.querySelectorAll("#tabs button").length;
    out.quota = [vis("quota-note"), byId("quota-note").textContent];
    out.dailyText = errText({ limit: "daily", max: 10 }, "x");
    out.paidOff = errText({ code: "paid_off", fallback: "paste" }, "x");
  }

  if (hash === "#expired") {
    await until(() => !byId("workspace").hidden);
    out.appOpened = !byId("workspace").hidden;
    byId("sample").click(); byId("run").click();
    await until(() => vis("gate"), 80);
    out.gate = vis("gate"); out.workspaceHidden = byId("workspace").hidden; out.runEnabled = !byId("run").disabled; out.signedOutSession = window.__sb.session === null;
  }

  if (hash === "#events") {
    await until(() => !byId("workspace").hidden);
    window.__reqs = []; const real = window.fetch;
    window.fetch = (u, o) => { if (String(u).includes("/api/events")) window.__reqs.push({ events: JSON.parse(o.body).events.map((e) => e.event), keepalive: !!o.keepalive, auth: !!(o.headers && o.headers.Authorization) }); return real(u, o); };
    track("tab_viewed", { tab: "fix" }); track("tab_viewed", { tab: "fight" }); track("draft_copied", { kind: "chat" });
    await wait(300); out.beforeHide = window.__reqs.length;  // taps send nothing by themselves
    window.dispatchEvent(new Event("pagehide")); await wait(400);
    out.sent = window.__reqs.slice(); out.queueLeft = eventQueue.length;
    out.explicit = await track("feedback", { answer: "yes" }, true); out.total = window.__reqs.length;  // the feedback answer goes at once and reports whether it was saved
  }

  if (hash.startsWith("#sync-")) {
    await until(() => !byId("workspace").hidden);
    const synced = () => fixSync.state !== "off" && !fixSync.busy;
    await until(synced, 80);
    out.state = fixSync.state;
    if (hash === "#sync-first") {
      await until(() => window.__sb.fixes.length === 2, 80);
      out.local = loadFixes().map((f) => f.id).sort(); out.remote = window.__sb.fixes.map((r) => r.id).sort(); out.owner = localStorage.getItem("bedahreview-fixes-owner");
      const b = loadFixes().find((f) => f.id === "bbb2"); out.fromAccount = [b.productLabel, b.note, b.after && b.after.count, b.after && b.after.total, b.date];
      const pushed = window.__sb.calls.filter((c) => c[0] === "upsert-fixes").flatMap((c) => c[1]);
      out.pushedIds = pushed.map((r) => r.id); out.columns = Object.keys(pushed[0]).sort(); out.pushedUser = pushed[0].user_id;
      out.pushedNote = pushed[0].note; out.pushedProduct = pushed[0].product;
    }
    if (hash === "#sync-other") {
      await wait(1800);
      out.local = loadFixes().map((f) => f.id); out.owner = localStorage.getItem("bedahreview-fixes-owner"); out.upserts = window.__sb.calls.filter((c) => c[0] === "upsert-fixes").length;
    }
    if (hash === "#sync-down") {
      openFixes(); out.status = byId("fixes-sync").textContent; out.statusWarn = byId("fixes-sync").className;
      out.saved = saveFixes(loadFixes().concat(Object.assign({}, loadFixes()[0], { id: "ccc3" })));
      await wait(1800);
      out.local = loadFixes().map((f) => f.id).sort(); out.upserts = window.__sb.calls.filter((c) => c[0] === "upsert-fixes").length; out.badge = byId("fixes-count").textContent;
    }
    if (hash === "#sync-edit") {
      await wait(1500);
      saveFixes(loadFixes().map((f) => (f.id === "bbb2" ? Object.assign({}, f, { note: "catatan baru", updated: Date.now() }) : f)));
      await until(() => window.__sb.fixes.some((r) => r.note === "catatan baru"), 80);
      out.remoteNote = (window.__sb.fixes.find((r) => r.id === "bbb2") || {}).note;
      saveFixes([]);
      await until(() => window.__sb.fixes.length === 0, 80);
      out.deleted = window.__sb.calls.filter((c) => c[0] === "delete-fixes").map((c) => c[1]); out.remoteLeft = window.__sb.fixes.length;
    }
    if (hash === "#sync-leave") {
      await leaveWithFixes();
      out.local = loadFixes().length; out.owner = localStorage.getItem("bedahreview-fixes-owner") || ""; out.remote = window.__sb.fixes.length;
    }
  }

  if (hash === "#delete") {
    if (window.__stage === 1) {
      await until(() => !byId("workspace").hidden);
      out.stage = 1; out.areaShown = !byId("delete-area").hidden;
      byId("delete-open").click(); out.panelOpened = !byId("delete-panel").hidden; out.warns = byId("delete-panel").textContent.includes("Tidak bisa dibatalkan");
      byId("delete-cancel").click(); out.cancelled = byId("delete-panel").hidden;
      byId("delete-open").click();
      sessionStorage.setItem("probe-stage1", JSON.stringify(out));  // the page reloads on success, so what stage 1 saw is carried over for stage 2 to report
      byId("delete-confirm").click();
      await wait(8000);  // the page reloads as soon as the server says it is done
      return out;
    }
    await until(() => !byId("gate").hidden);
    return { stage: 2, stage1: JSON.parse(sessionStorage.getItem("probe-stage1") || "null"), gate: !byId("gate").hidden, workspaceHidden: byId("workspace").hidden, info: byId("g-info").textContent, infoShown: !byId("g-info").hidden,
      fixes: localStorage.getItem("bedahreview-fixes-v1"), device: localStorage.getItem("bedahreview-device"), owner: localStorage.getItem("bedahreview-fixes-owner"), session: window.__sb.session };
  }

  const shown = (id) => getComputedStyle(byId(id)).display !== "none";
  if (hash === "#free") {
    await until(() => !byId("workspace").hidden);
    out.start = { gate: vis("gate"), signinBtn: vis("signin-btn"), acct: vis("acct") };
    byId("sample").click(); byId("run").click();
    await until(() => !byId("view-results").hidden, 80);
    out.locked = { unlock: vis("unlock"), panels: shown("panels"), counts: document.querySelectorAll("#tabs button .n").length, flag: localStorage.getItem("bedahreview-free-used") };
    byId("tab-fight").click(); out.tabPointsToSignIn = document.activeElement === byId("unlock-h");
    show("input"); byId("run").click(); await wait(300);
    out.second = { gate: vis("gate"), info: byId("g-info").textContent, close: vis("g-close") };
    byId("g-email").value = "ani@example.com"; byId("g-send").click();
    await until(() => vis("g-verify")); paste("123456");
    await until(() => vis("g-intro")); byId("g-skip").click();
    await until(() => !byId("workspace").hidden && !byId("view-results").hidden, 80);
    out.after = { unlock: vis("unlock"), panels: shown("panels"), acct: vis("acct"), signinBtn: vis("signin-btn"), email: byId("acct-email").textContent, queued: eventQueue.map((e) => e.event) };
  }

  if (hash === "#free-google") {
    await until(() => !byId("workspace").hidden);
    if (!window.__back) {
      byId("sample").click(); byId("run").click();
      await until(() => vis("unlock"), 80);
      byId("unlock-google").click(); await wait(300);
      const saved = JSON.parse(sessionStorage.getItem("bedahreview-free-result") || "null");
      sessionStorage.setItem("probe-stage1", JSON.stringify({ oauth: calls("oauth").length, saved: !!(saved && saved.free && saved.items.length) }));
      sessionStorage.setItem("probe-back", "1");
      location.reload();
      await wait(8000);
      return out;
    }
    await until(() => !byId("view-results").hidden, 80);
    return { stage1: JSON.parse(sessionStorage.getItem("probe-stage1") || "null"), results: !byId("view-results").hidden, unlock: vis("unlock"), panels: shown("panels"),
      left: sessionStorage.getItem("bedahreview-free-result"), queued: eventQueue.map((e) => e.event) };
  }

  if (hash === "#account") {
    await until(() => !byId("workspace").hidden);
    out.closed = vis("acct-menu");
    byId("acct-btn").click();
    out.open = [vis("acct-menu"), byId("acct-btn").getAttribute("aria-expanded"), document.activeElement && document.activeElement.className];
    out.items = [...byId("acct-menu").querySelectorAll(".mitem")].filter((b) => b.offsetParent).map((b) => b.textContent);
    out.deleteRed = getComputedStyle(byId("delete-open")).color !== getComputedStyle(byId("logout")).color;
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); out.escClosed = !vis("acct-menu") && document.activeElement === byId("acct-btn");
    byId("acct-btn").click(); document.body.click(); out.outsideClosed = !vis("acct-menu");
    out.oldFooter = !!document.querySelector("main #logout, main #delete-open");
  }

  if (hash === "#notes") {
    await until(() => !byId("workspace").hidden);
    lastData = { items: [{ text: "warna tulisannya ijo pas dateng hitem banget" }] };
    out.copied = noteCopiesReview("warna tulisannya ijo pas dateng hitem banget parah"); out.own = noteCopiesReview("ganti foto varian putih"); out.empty = noteCopiesReview("");
  }

  if (hash === "#keep") {
    await until(() => !byId("workspace").hidden); byId("signin-btn").click();
    await until(() => vis("gate"));
    const store = window.__sb.opts.auth.storage;
    out.defaultOn = byId("g-keep").checked; out.label = byId("g-keep").parentElement.textContent.trim();
    store.setItem("t1", "a");
    out.on = [localStorage.getItem("t1"), sessionStorage.getItem("t1")];
    byId("g-keep").click();  // switch it off
    store.setItem("t1", "b");
    out.off = [localStorage.getItem("t1"), sessionStorage.getItem("t1"), store.getItem("t1")];
    out.flagOff = localStorage.getItem("bedahreview-keep");
    store.removeItem("t1"); out.removed = [localStorage.getItem("t1"), sessionStorage.getItem("t1")];
    byId("g-email").value = "ani@example.com"; byId("g-send").click();
    out.flagAfterSend = localStorage.getItem("bedahreview-keep");  // still off: sending a code does not undo the choice
    byId("g-keep").click(); out.flagOn = localStorage.getItem("bedahreview-keep");
    out.ios = vis("g-ios");
  }

  if (hash === "#example") {
    await until(() => !byId("workspace").hidden); byId("signin-btn").click();
    await until(() => vis("gate"));
    byId("g-example").click();
    await until(() => !byId("view-results").hidden, 80);
    out.results = !byId("view-results").hidden; out.note = byId("start").textContent.includes("contoh dengan ulasan rekaan");
    out.feedbackHidden = byId("feedback").hidden; out.fixButtons = document.querySelectorAll("#panel-fix .fixform").length;
    out.signinBtn = vis("signin-btn"); out.counts = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    localStorage.setItem("bedahreview-free-used", "1");  // the free analysis was already used in this browser
    show("input"); byId("sample").click(); byId("run").click();
    await wait(300);
    out.gateAfterRun = vis("gate"); out.closeShown = vis("g-close"); out.info = byId("g-info").textContent;
    byId("g-close").click(); out.backInApp = !byId("workspace").hidden;
  }
  return out;
})();
</script>
"""


@unittest.skipUnless(CHROME, "Chrome is not installed")
class SignInBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        static = Path(cls.tmp) / "static"
        shutil.copytree(web.STATIC, static)
        key, jwk = new_key("k1")
        index = static / "index.html"
        html = index.read_text(encoding="utf-8")
        start = '<script>\n"use strict";'
        assert html.count(start) == 1
        html = html.replace(start, FAKE_SUPABASE.replace("__TOKEN__", token(key)) + start).replace("</body>", PROBE + "</body>")
        index.write_text(html, encoding="utf-8")
        cls.real_static, web.STATIC = web.STATIC, static
        cls.admin_calls = []
        cls.server = web.make_server(0, classify_fn=_stub_classify, access_codes={"ani": "ani-secret-1"}, public=True,
                                     account_admin=AccountAdmin(URL, "sb_secret_x", request=lambda m, u, h, b: (cls.admin_calls.append((m, u.replace(URL, ""))) or 204)),
                                     supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x", free_per_visitor=50)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web.STATIC = cls.real_static
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def probe(self, hash_, user_agent=None):
        profile = tempfile.mkdtemp(dir=self.tmp)
        url = "http://127.0.0.1:%d/%s" % (self.port, hash_)
        flags = ["--user-agent=" + user_agent] if user_agent else []
        chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                   "--window-size=500,900"] + flags + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return _read_probe(profile, url)
        finally:
            chrome.terminate()
            chrome.wait(timeout=20)

    def test_a_visitor_lands_in_the_app_and_the_sign_in_opens_from_the_header(self):
        out = self.probe("#out")
        self.assertEqual(out["start"], {"gate": False, "app": True, "signinBtn": True, "acct": False})  # the first analysis is free, so the app comes first
        self.assertEqual((out["gate"], out["workspaceHidden"]), (True, True))
        self.assertEqual(out["title"], "Masuk untuk membedah ulasanmu")
        self.assertEqual(out["google"], "Masuk dengan Google")
        self.assertTrue(out["consent"])
        self.assertTrue(out["codeLink"])  # interviewers with an access code still have a way in
        self.assertFalse(out["noContactAsk"])  # no "may we contact you" question anywhere
        self.assertEqual(out["otp"], ["one-time-code", "numeric", "off", 6])
        self.assertEqual(out["titleEn"], "Sign in to analyse your reviews")
        self.assertEqual(out["oauth"], ["google", "http://127.0.0.1:%d/" % self.port])
        self.assertEqual((out["badEmail"], out["otpCalls"]), ("Alamat email tidak valid.", 0))  # a bad address never reaches Supabase

    def test_email_code_flow_then_skippable_intro_then_the_app(self):
        out = self.probe("#otp")
        self.assertIn("ani@example.com", out["sub"])
        self.assertRegex(out["resend"], r"Kirim ulang kode \(\d+\)")  # the 60-second wait is shown
        self.assertTrue(out["resendDisabled"])
        self.assertEqual(out["wrong"], "Kode salah atau sudah kedaluwarsa. Minta kode baru.")
        self.assertTrue(out["cleared"])
        self.assertEqual(out["verify"], [["ani@example.com", "000000", "email"], ["ani@example.com", "123456", "email"]])  # a paste fills all six boxes
        self.assertTrue(out["intro"])
        self.assertTrue(out["app"] and out["gateGone"])
        self.assertEqual(out["upserts"], [{"user_id": "user-1111-2222", "lang": "id"}])  # skipping still saves a row, so the questions are not asked again
        self.assertTrue(out["logout"])
        self.assertFalse(out["signinBtn"])

    def test_intro_saves_only_the_fixed_fields_and_checks_the_shop_link(self):
        out = self.probe("#intro")
        self.assertTrue(out["intro"])
        self.assertEqual(out["platforms"], ["", "tokopedia", "lazada", "tiktokshop", "shopee", "other"])
        self.assertFalse(out["noContactAsk"])
        self.assertEqual((out["shopErr"], out["savedEarly"]), ("Tautan toko harus diawali https://", 0))
        self.assertEqual(out["row"], {"user_id": "user-1111-2222", "lang": "id", "platform": "tokopedia", "category": "fashion", "shop_url": "https://www.tokopedia.com/tokosaya"})
        self.assertTrue(out["app"])

    def test_a_returning_person_goes_straight_in_and_can_analyse(self):
        out = self.probe("#returning")
        self.assertEqual((out["intro"], out["upserts"]), (False, 0))
        self.assertTrue(out["results"])  # the server accepted the token the page sent
        self.assertEqual(out["tabs"], 4)
        self.assertEqual(out["quota"], [True, "Sisa analisis hari ini: 9 dari 10."])  # shown once the server says how many are left
        self.assertEqual(out["dailyText"], "Batas harian tercapai: 10 analisis per hari. Coba lagi besok (reset tengah malam WIB).")
        self.assertEqual(out["paidOff"], "Mengambil ulasan dari tautan belum tersedia untuk akun ini. Kamu bisa menempel ulasannya.")

    def test_an_expired_sign_in_asks_again_instead_of_reloading_the_page(self):
        out = self.probe("#expired")
        self.assertTrue(out["appOpened"])
        self.assertTrue(out["gate"] and out["workspaceHidden"])
        self.assertTrue(out["runEnabled"])
        self.assertTrue(out["signedOutSession"])

    def test_events_wait_in_a_queue_and_go_out_together(self):
        out = self.probe("#events")
        self.assertEqual(out["beforeHide"], 0)
        self.assertEqual(out["sent"], [{"events": ["opened", "tab_viewed", "tab_viewed", "draft_copied"], "keepalive": True, "auth": True}])  # one request, kept alive past the page, with the sign-in
        self.assertEqual(out["queueLeft"], 0)
        self.assertEqual((out["explicit"], out["total"]), (True, 2))  # the server accepted it, and it did not wait

    EXPECTED_COLUMNS = sorted(["user_id", "id", "updated_at", "theme", "variant", "product", "product_label", "note", "fixed_on", "before_count", "before_total", "before_sig", "after_count", "after_total", "after_sig", "after_on"])

    def test_the_first_sign_in_merges_this_browsers_fixes_with_the_accounts(self):
        out = self.probe("#sync-first")
        self.assertEqual(out["state"], "ok")
        self.assertEqual((out["local"], out["remote"]), (["aaa1", "bbb2"], ["aaa1", "bbb2"]))  # both lists end up in both places
        self.assertEqual(out["owner"], "user-1111-2222")
        self.assertEqual(out["fromAccount"], ["Kaos Boxy", "foto diganti", 1, 5, "2026-10-25"])  # a fix made on another phone arrives complete
        self.assertEqual(out["pushedIds"], ["aaa1"])  # only what the account did not have is pushed
        self.assertEqual(out["columns"], self.EXPECTED_COLUMNS)  # exactly the columns of the table: nothing else leaves the browser
        self.assertEqual((out["pushedUser"], out["pushedNote"], out["pushedProduct"]), ("user-1111-2222", "size chart diganti", "kaos boxy retro"))

    def test_a_browser_that_held_another_accounts_fixes_is_not_mixed_into_this_one(self):
        out = self.probe("#sync-other")
        self.assertEqual((out["local"], out["upserts"]), ([], 0))
        self.assertEqual(out["owner"], "user-1111-2222")

    def test_when_supabase_is_unavailable_the_page_says_so_and_keeps_working(self):
        out = self.probe("#sync-down")
        self.assertEqual(out["state"], "down")
        self.assertIn("Riwayat sedang tidak tersedia", out["status"])
        self.assertIn("warn", out["statusWarn"])
        self.assertTrue(out["saved"])
        self.assertEqual((out["local"], out["upserts"], out["badge"]), (["aaa1", "ccc3"], 0, "2"))  # still saved on the device, nothing pushed

    def test_edits_and_deletes_follow_to_the_account(self):
        out = self.probe("#sync-edit")
        self.assertEqual(out["remoteNote"], "catatan baru")
        self.assertEqual((out["deleted"], out["remoteLeft"]), ([["bbb2"]], 0))

    def test_signing_out_clears_this_browsers_copy_but_never_the_accounts(self):
        out = self.probe("#sync-leave")
        self.assertEqual((out["local"], out["owner"], out["remote"]), (0, "", 1))

    def test_deleting_the_account_asks_twice_deletes_and_leaves_nothing_behind(self):
        self.admin_calls.clear()
        self.addCleanup(lambda: self.server.RequestHandlerClass.supabase_auth._revoked.clear())  # the deleted test account is blocked on this shared server: let the other tests use it again
        out = self.probe("#delete")
        self.assertEqual(out["stage"], 2, out)  # the page reloaded: the server said it was done
        self.assertEqual(out["stage1"], {"stage": 1, "areaShown": True, "panelOpened": True, "warns": True, "cancelled": True})  # it asked first, and Batal closed the question
        self.assertTrue(out["gate"] and out["workspaceHidden"])  # signed out
        self.assertTrue(out["infoShown"])
        self.assertEqual(out["info"], "Akunmu dan semua datanya sudah dihapus.")
        self.assertEqual((out["fixes"], out["device"], out["owner"]), (None, None, None))  # the fixes copy, the device id and the owner mark are gone from this browser
        self.assertIsNone(out["session"])
        self.assertEqual(self.admin_calls, [("DELETE", "/rest/v1/%s?user_id=eq.%s" % (t, USER)) for t in TABLES] + [("DELETE", "/auth/v1/admin/users/" + USER)])

    def test_a_note_cannot_repeat_review_text(self):
        out = self.probe("#notes")
        self.assertEqual((out["copied"], out["own"], out["empty"]), (True, False, False))

    def test_keep_me_signed_in_chooses_where_the_session_lives(self):
        out = self.probe("#keep")
        self.assertTrue(out["defaultOn"])  # on by default
        self.assertEqual(out["label"], "Tetap masuk di HP ini")
        self.assertEqual(out["on"], ["a", None])  # on: this device's storage
        self.assertEqual(out["off"], [None, "b", "b"])  # off: this tab only, and no second copy left on the device
        self.assertEqual(out["flagOff"], "0")
        self.assertEqual(out["removed"], [None, None])
        self.assertEqual((out["flagAfterSend"], out["flagOn"]), ("0", "1"))
        self.assertFalse(out["ios"])  # no install note on a desktop browser

    def test_an_iphone_in_the_browser_is_told_to_install_first(self):
        iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        out = self.probe("#keep", user_agent=iphone)
        self.assertTrue(out["ios"])

    def test_the_free_example_works_without_signing_in_and_analysing_asks_for_it(self):
        out = self.probe("#example")
        self.assertTrue(out["results"] and out["note"])
        self.assertTrue(out["feedbackHidden"])
        self.assertEqual(out["fixButtons"], 0)  # nothing from the example can be saved as a real fix
        self.assertTrue(out["signinBtn"])
        self.assertTrue(out["gateAfterRun"])  # the free analysis was used: analysing asks for a sign-in
        self.assertEqual(out["info"], "Analisis gratismu sudah terpakai. Masuk untuk lanjut, tetap gratis.")
        self.assertTrue(out["closeShown"] and out["backInApp"])

    def test_one_free_analysis_shows_the_counts_and_a_sign_in_shows_the_rest(self):
        out = self.probe("#free")
        self.assertEqual(out["start"], {"gate": False, "signinBtn": True, "acct": False})
        self.assertEqual(out["locked"], {"unlock": True, "panels": False, "counts": 4, "flag": "1"})  # the four numbers show, the details wait
        self.assertTrue(out["tabPointsToSignIn"])
        self.assertEqual(out["second"], {"gate": True, "info": "Analisis gratismu sudah terpakai. Masuk untuk lanjut, tetap gratis.", "close": True})
        after = out["after"]
        self.assertEqual((after["unlock"], after["panels"], after["acct"], after["signinBtn"]), (False, True, True, False))
        self.assertEqual(after["email"], "ani@example.com")  # shown in this browser's menu only
        self.assertIn("free_unlocked", after["queued"])
        self.assertIn("analysed", after["queued"])  # the free analysis now counts for the account

    def test_a_free_analysis_waits_in_the_tab_during_the_google_sign_in(self):
        out = self.probe("#free-google")
        self.assertEqual(out["stage1"], {"oauth": 1, "saved": True})
        self.assertTrue(out["results"])
        self.assertEqual((out["unlock"], out["panels"]), (False, True))
        self.assertIsNone(out["left"])  # removed as soon as it was shown again
        self.assertIn("free_unlocked", out["queued"])

    def test_the_account_menu_holds_sign_out_privacy_and_a_separate_red_delete(self):
        out = self.probe("#account")
        self.assertFalse(out["closed"])
        self.assertEqual(out["open"], [True, "true", "mitem"])  # opens with the first item focused
        self.assertEqual(out["items"][0], "Keluar")
        self.assertIn("Kebijakan privasi", out["items"])
        self.assertEqual(out["items"][-1], "Hapus akun dan data saya")  # last, apart from the rest
        self.assertTrue(out["deleteRed"])
        self.assertTrue(out["escClosed"] and out["outsideClosed"])
        self.assertFalse(out["oldFooter"])  # no longer at the bottom of the results


if __name__ == "__main__":
    unittest.main()
