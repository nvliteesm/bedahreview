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
from bedahreview.auth import SupabaseAuth

try:
    from test_auth import URL, FakeSupabase, new_key, token
    from test_browser import CHROME, _read_probe, _stub_classify
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, FakeSupabase, new_key, token
    from tests.test_browser import CHROME, _read_probe, _stub_classify

FAKE_SUPABASE = """
<script>
window.__sb = { calls: [], session: null, profile: null, listeners: [] };
(function () {
  const hash = location.hash, user = { id: "user-1111-2222" };
  if (["#intro", "#returning", "#expired"].includes(hash)) window.__sb.session = { access_token: hash === "#expired" ? "junk" : "__TOKEN__", user };
  if (["#returning", "#expired"].includes(hash)) window.__sb.profile = { user_id: user.id };
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
    from: (table) => ({
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
  }

  if (hash === "#expired") {
    await until(() => !byId("workspace").hidden);
    out.appOpened = !byId("workspace").hidden;
    byId("sample").click(); byId("run").click();
    await until(() => vis("gate"), 80);
    out.gate = vis("gate"); out.workspaceHidden = byId("workspace").hidden; out.runEnabled = !byId("run").disabled; out.signedOutSession = window.__sb.session === null;
  }

  if (hash === "#keep") {
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
    await until(() => vis("gate"));
    byId("g-example").click();
    await until(() => !byId("view-results").hidden, 80);
    out.results = !byId("view-results").hidden; out.note = byId("start").textContent.includes("contoh dengan ulasan rekaan");
    out.feedbackHidden = byId("feedback").hidden; out.fixButtons = document.querySelectorAll("#panel-fix .fixform").length;
    out.signinBtn = vis("signin-btn"); out.counts = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    show("input"); byId("sample").click(); byId("run").click();
    await wait(300);
    out.gateAfterRun = vis("gate"); out.closeShown = vis("g-close");
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
        cls.server = web.make_server(0, classify_fn=_stub_classify, access_codes={"ani": "ani-secret-1"}, public=True,
                                     supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
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

    def test_a_visitor_who_is_not_signed_in_sees_the_sign_in_not_the_app(self):
        out = self.probe("#out")
        self.assertEqual((out["gate"], out["workspaceHidden"]), (True, True))
        self.assertEqual(out["title"], "Masuk untuk membedah ulasanmu")
        self.assertEqual(out["google"], "Masuk dengan Google")
        self.assertTrue(out["consent"])
        self.assertTrue(out["codeLink"])  # interviewers with an access code still have a way in
        self.assertFalse(out["signinBtn"])  # the gate itself is the sign-in
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

    def test_an_expired_sign_in_asks_again_instead_of_reloading_the_page(self):
        out = self.probe("#expired")
        self.assertTrue(out["appOpened"])
        self.assertTrue(out["gate"] and out["workspaceHidden"])
        self.assertTrue(out["runEnabled"])
        self.assertTrue(out["signedOutSession"])

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
        self.assertTrue(out["gateAfterRun"])
        self.assertTrue(out["closeShown"] and out["backInApp"])


if __name__ == "__main__":
    unittest.main()
