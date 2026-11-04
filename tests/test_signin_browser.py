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
from bedahreview.fetchers.base import Fetcher, Review

try:
    from test_auth import URL, USER, FakeSupabase, new_key, token
    from test_browser import CHROME, _read_probe, _stub_classify
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, USER, FakeSupabase, new_key, token
    from tests.test_browser import CHROME, _read_probe, _stub_classify

class LinkFetch(Fetcher):
    """Stands in for the paid review service: any link gives the same three reviews."""
    last_platform = "tokopedia"

    def check_link(self, url):
        return url

    def fetch_reviews(self, url, limit, mode="latest"):
        return [Review(3, "warna tulisannya ijo pas dateng hitem", "Putih, M", "2026-10-01", 0, False, "Kaos Boxy Retro"),
                Review(2, "Pas sampe koyak bajunya", "Hitam, L", "2026-10-02", 0, False, "Kaos Boxy Retro"),
                Review(5, "bahannya tebel, bagus", "Putih, L", "2026-10-03", 0, False, "Kaos Boxy Retro")]


FAKE_SUPABASE = """
<script>
window.__sb = { calls: [], session: null, profile: null, listeners: [], fixes: [], fixesError: false, history: [], boxes: [], boxesError: false };
(function () {
  const hash = location.hash, user = { id: "user-1111-2222", email: "ani@example.com" };
  window.__stage = sessionStorage.getItem("bedahreview-deleted") ? 2 : 1;  // the delete test reloads the page once: stage 2 is the page after the deletion
  const syncing = ["#sync-first", "#sync-other", "#sync-down", "#sync-edit", "#sync-leave", "#delete", "#history", "#rival-flow", "#boxsync", "#boxdown"];
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
    from: (table) => table === "history" ? ({
      insert: (row) => {
        window.__sb.calls.push(["insert-history", row]);
        const id = (window.__sb.nextId = (window.__sb.nextId || 100) + 1);
        window.__sb.history.unshift(Object.assign({ id, created_at: new Date().toISOString() }, row));
        const done = Promise.resolve({ error: null }); done.select = () => Promise.resolve({ data: [{ id }], error: null });
        return done;
      },
      delete: () => ({ lt: async (col, value) => { window.__sb.calls.push(["prune-history", col, value]); window.__sb.history = window.__sb.history.filter((r) => r.created_at >= value); return { error: null }; } }),
      select: (cols) => {
        window.__sb.calls.push(["select-history", cols]);
        const q = { gte: () => q, order: () => q, limit: () => q, then: (a, b) => Promise.resolve({ data: window.__sb.history.slice(), error: null }).then(a, b) };
        return q;
      },
    }) : table === "fixes" ? ({
      select: () => {
        window.__sb.calls.push(["select-fixes"]);
        const res = window.__sb.fixesError ? { data: null, error: { message: "paused" } } : { data: window.__sb.fixes.slice(), error: null };
        return { then: (a, b) => Promise.resolve(res).then(a, b) };
      },
      upsert: async (rows) => { window.__sb.calls.push(["upsert-fixes", rows]); rows.forEach((r) => { window.__sb.fixes = window.__sb.fixes.filter((x) => x.id !== r.id).concat(r); }); return { error: null }; },
      delete: () => ({ in: async (col, ids) => { window.__sb.calls.push(["delete-fixes", ids]); window.__sb.fixes = window.__sb.fixes.filter((x) => !ids.includes(x.id)); return { error: null }; } }),
    }) : table === "task_boxes" ? ({
      select: () => {
        window.__sb.calls.push(["select-boxes"]);
        const res = window.__sb.boxesError ? { data: null, error: { message: "relation does not exist" } } : { data: JSON.parse(JSON.stringify(window.__sb.boxes)), error: null };
        return { then: (a, b) => Promise.resolve(res).then(a, b) };
      },
      upsert: async (rows) => { window.__sb.calls.push(["upsert-boxes", rows.map((r) => r.sig)]); rows.forEach((r) => { window.__sb.boxes = window.__sb.boxes.filter((x) => x.sig !== r.sig).concat(JSON.parse(JSON.stringify(r))); }); return { error: null }; },
      delete: () => ({ in: async (col, sigs) => { window.__sb.calls.push(["delete-boxes", sigs]); window.__sb.boxes = window.__sb.boxes.filter((x) => !sigs.includes(x.sig)); return { error: null }; } }),
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
  const NL = String.fromCharCode(10);
  const SAMPLE = [  // pasting reviews is gone from the page; the server route and the page's send() remain, so the tests feed them directly
    "★★★★★  Putih, L   bagus banget pakenya sama bahannya bagus luamayan tebel",
    "★★★★☆  Putih, L   nyaman bet dah pokoknya, bahannya tebel cuman pas dateng kok ada dikit noda kecil ya, tapi ini gacor sih bagus banget desainnya",
    "★★★☆☆  Putih, M   di tampilan sebelom beli, warna tulisannya ijo, pas dateng warna tulisannya hitem",
    "★★☆☆☆  Hitam, L   Pas sampe koyak bajunya",
    "★☆☆☆☆  Putih, L   produk tidak sesuai deskripsi, warna hijau tapi realitanya hitam, kalo emg terima dari pabrikan nya hitam kenapa ga pasang foto hitam aja?",
  ].join(NL);
  const runSample = () => { if (!ensureSignedIn(true)) return; send({ text: SAMPLE, product: "Kaos Boxy Retro" }, "t"); };
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
    out.histHidden = byId("acct-history").hidden && byId("tasks-history").hidden;
    out.consent = byId("g-choose").textContent.includes("Teks ulasanmu tidak kami simpan.");
    out.twoChoices = vis("g-google") && vis("g-send") && vis("g-close") && !byId("g-example") && !byId("g-codelink") && !document.querySelector("details.gate-opts");
    out.signinBtn = vis("signin-btn");
    out.noContactAsk = /hubungi|wawancara|contacted|interview/i.test(byId("gate").textContent);
    byId("acct-btn").click(); out.menuVisitor = [...document.querySelectorAll("#acct-menu .mitem")].filter((b) => b.offsetParent).map((b) => b.id || "link");  // access code, theme and the stay-signed-in choice live in the avatar menu
    out.keepInMenu = byId("g-keep").checked && !!byId("g-keep").closest("#acct-menu");  // on by default, and out of the way
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
    runSample();
    await until(() => !byId("view-results").hidden, 80);
    out.results = !byId("view-results").hidden; out.tabs = document.querySelectorAll("#tabs button").length;
    out.quota = [vis("quota-link"), byId("quota-link").textContent];
    out.dailyText = errText({ limit: "daily", max: 10 }, "x");
  }

  if (hash === "#expired") {
    await until(() => !byId("workspace").hidden);
    out.appOpened = !byId("workspace").hidden;
    runSample();
    await until(() => vis("gate"), 80);
    out.gate = vis("gate"); out.workspaceHidden = byId("workspace").hidden; out.runEnabled = !byId("fetch").disabled; out.signedOutSession = window.__sb.session === null;
  }

  if (hash === "#history") {
    await until(() => !byId("workspace").hidden);
    const daysAgo = (n) => new Date(Date.now() - n * 864e5).toISOString();
    window.__sb.history = [{ id: 1, user_id: "user-1111-2222", created_at: daysAgo(70), product: "Kaos Lama", reviews: 4, fix_n: 1, fight_n: 0, ignore_n: 0, keep_n: 3, top_theme: "Sizing & fit" },
      { id: 2, user_id: "user-1111-2222", created_at: daysAgo(10), product: "Kaos Tengah", reviews: 6, fix_n: 2, fight_n: 0, ignore_n: 0, keep_n: 4, top_theme: "Sizing & fit" }];
    out.menuShown = !byId("acct-history").hidden;
    runSample();
    await until(() => !byId("view-results").hidden, 80);
    await until(() => calls("insert-history").length > 0, 40);
    const row = (calls("insert-history")[0] || [])[1] || {};
    out.row = row; out.rowText = JSON.stringify(row);
    await saveHistory(lastData, lastCounts);  // the same result drawn again is not saved twice
    out.inserts = calls("insert-history").length;
    out.heldBefore = !!heldFor(reviewSig(lastData.items));
    byId("acct-btn").click(); byId("acct-history").click();
    out.opened = eventQueue.some((e) => e.event === "history_opened");
    await until(() => byId("history-body").querySelector(".hrow, .empty2, .callout"), 40);
    await wait(100);
    out.view = vis("view-history");
    out.noFooter = !byId("view-history").textContent.includes("Dihapus otomatis") && !byId("view-history").textContent.includes("hanya ada di HP");
    const cards = () => [...byId("history-body").querySelectorAll(".hrow")].map((c) => [c.querySelector("b").textContent, c.querySelector(".hm").textContent, (c.querySelector(".st") || {}).textContent || "", [...c.querySelectorAll(".hl button")].map((b) => b.textContent)]);
    out.cards = cards();
    out.expectDay = fmtDayShort(today());
    const prunes = calls("prune-history"); out.prunes = prunes.length;
    out.cutoffDays = prunes.map((c) => Math.round((Date.now() - Date.parse(c[2])) / 864e5));
    out.oldGone = !window.__sb.history.some((r) => r.product === "Kaos Lama");
    // "Belum selesai": Lanjutkan draws the held result again, Selesai makes the box, plays the animation, and Riwayat says Selesai
    [...byId("history-body").querySelectorAll("button")].find((b) => b.textContent === "Lanjutkan").click();
    out.resumed = [vis("view-results"), byId("results-h").textContent, calls("insert-history").length];
    byId("done-btn").click();
    out.anim = vis("done-anim");
    await until(() => vis("view-fixes") && !vis("done-anim"), 80);
    out.landedOn = [vis("view-fixes"), vis("view-history")];  // Selesai opens Tugas for a signed-in seller too
    const landed = { toast: byId("toast2").textContent, badge: byId("tasks-count").textContent, held: !!heldFor(reviewSig(lastData.items)) };
    const taskBox = () => ({ boxes: document.querySelectorAll(".tbox").length });
    out.landedBox = taskBox();
    byId("tasks-history").click();  // the result was saved in Riwayat as Selesai
    await until(() => byId("history-body").querySelector(".st.done"), 40);
    out.afterDone = Object.assign({ cards: cards() }, landed);
    byId("nav-tasks").click();
    const box = () => ({ boxes: document.querySelectorAll(".tbox").length, items: document.querySelectorAll(".tbox .it").length, meta: (document.querySelector(".tbox .tmeta") || {}).textContent, chip: (document.querySelector(".tbox .tchip") || {}).textContent,
      bar: (document.querySelector(".tbox .bar") || document.body).getAttribute("aria-valuenow"), cal: !!document.querySelector(".tbox .tlinks button") });
    out.box = box(); out.boxText = document.querySelector(".tbox").textContent;
    document.querySelector(".tbox .it input").click();
    out.ticked = [box().meta, box().bar, byId("tasks-count").textContent];
    [...document.querySelectorAll(".tbox .it input")].forEach((c) => { if (!c.checked) c.click(); });
    out.allDone = [!!document.querySelector(".tbox.alldone"), (document.querySelector(".tbox.alldone .btn") || {}).textContent, byId("tasks-count").hidden];
    out.stored = localStorage.getItem("bedahreview-boxes-v1");
    byId("tasks-history").click();
    await until(() => byId("history-body").querySelector(".hrow"), 40);
    [...byId("history-body").querySelectorAll("button")].filter((b) => b.textContent === "Bedah lagi").pop().click();  // a row this device does not know: the link screen, empty
    out.again = [vis("view-input"), byId("url").value];
  }

  if (hash === "#boxsync") {  // Tugas follows the account: phone A makes a box, phone B (an empty browser) gets it, ticks it, and A sees the tick
    await until(() => !byId("workspace").hidden);
    await until(() => calls("select-boxes").length > 0);
    await wait(100);
    runSample();
    await until(() => !byId("view-results").hidden, 80);
    byId("done-btn").click();
    await until(() => vis("view-fixes") && !vis("done-anim"), 80);
    await until(() => window.__sb.boxes.length > 0, 40);
    const row = window.__sb.boxes[0];
    out.a = { rows: window.__sb.boxes.length, items: row.items.map((i) => [i.kind, i.done]), text: JSON.stringify(row), cols: Object.keys(row).sort() };
    // phone B: nothing in this browser, same account
    clearBoxes(); resetBoxSync(); renderFixes();
    out.bBefore = document.querySelectorAll(".tbox").length;
    await syncBoxes(true);
    out.bAfter = [document.querySelectorAll(".tbox").length, document.querySelectorAll(".tbox .it").length];
    document.querySelector(".tbox .it input").click();
    await until(() => window.__sb.boxes[0].items.some((i) => i.done), 40);
    out.bTick = window.__sb.boxes[0].items.map((i) => i.done);
    // phone A again: the account has a newer copy with the second item ticked too
    const remote = JSON.parse(JSON.stringify(window.__sb.boxes[0]));
    remote.items = remote.items.map((i) => Object.assign({}, i, { done: true })); remote.updated_at = new Date(Date.now() + 5000).toISOString(); remote.done_at = remote.updated_at;
    window.__sb.boxes = [remote];
    await syncBoxes(true);
    out.aSees = [document.querySelectorAll(".tbox .it input:checked").length, !!document.querySelector(".tbox.alldone")];
    // removed on another phone: gone here too
    window.__sb.boxes = [];
    await syncBoxes(true);
    out.removed = document.querySelectorAll(".tbox, .trow").length;
    // a new box made here is pushed
    runSample(); await until(() => !byId("view-results").hidden, 80); byId("done-btn").click();
    await until(() => vis("view-fixes") && !vis("done-anim"), 80); await until(() => window.__sb.boxes.length > 0, 40);
    out.pushedAgain = window.__sb.boxes.length;
    // signing out clears the browser copy; the account keeps the box
    await leaveWithFixes();
    out.leave = [loadBoxes().length, window.__sb.boxes.length, localStorage.getItem("bedahreview-boxes-v1")];
  }

  if (hash === "#boxdown") {  // the table is missing or Supabase is down: Tugas still works from this browser
    await until(() => !byId("workspace").hidden);
    window.__sb.boxesError = true; await syncBoxes(true);  // the table cannot be read
    runSample(); await until(() => !byId("view-results").hidden, 80);
    byId("done-btn").click();
    await until(() => vis("view-fixes") && !vis("done-anim"), 80);
    document.querySelector(".tbox .it input").click();
    await wait(300);
    out.local = [document.querySelectorAll(".tbox").length, document.querySelectorAll(".tbox .it input:checked").length, boxSync.state];
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
      openFixes(); out.status = byId("fixes-sync").textContent; out.statusWarn = byId("fixes-sync").className; out.moreHidden = byId("fixes-more").hidden;
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
    runSample();
    await until(() => !byId("view-results").hidden, 80);
    out.locked = { unlock: vis("unlock"), panels: shown("panels"), counts: document.querySelectorAll("#tabs button .n").length, flag: localStorage.getItem("bedahreview-free-used") };
    byId("tab-fight").click(); out.tabPointsToSignIn = document.activeElement === byId("unlock-h");
    show("input"); byId("url").value = "https://www.tokopedia.com/toko/kaos-boxy"; byId("url").dispatchEvent(new Event("input")); byId("fetch").click(); await wait(300);
    out.second = { gate: vis("gate"), info: byId("g-info").textContent, close: vis("g-close") };
    byId("g-email").value = "ani@example.com"; byId("g-send").click();
    await until(() => vis("g-verify")); paste("123456");
    await until(() => vis("g-intro")); byId("g-skip").click();
    await until(() => !byId("workspace").hidden && !byId("view-results").hidden, 80);
    out.after = { unlock: vis("unlock"), panels: shown("panels"), acct: vis("acct"), signinBtn: vis("signin-btn"), email: byId("acct-email").textContent, queued: eventQueue.map((e) => e.event),
      plan: shown("res-end") };  // the email code keeps the tab: the result is drawn again, now with the Selesai button
  }

  if (hash === "#free-link") {
    await until(() => !byId("workspace").hidden);
    out.noDemo = !/demo|sen dolar|cents/i.test(byId("view-input").textContent);
    out.emptyFirst = byId("fetch").classList.contains("off") && byId("fetch").textContent === "Bedah ulasan" && byId("detect").hidden && byId("link-extra").hidden;  // an empty box waits; nothing to choose
    const put = (v) => { byId("url").value = v; byId("url").dispatchEvent(new Event("input")); };
    put("https://www.tokopedia.com/toko/kaos-boxy");
    out.linkFirst = !byId("detect").hidden && byId("detect-text").textContent.includes("Tokopedia") && byId("fetch").textContent === "Ambil & bedah" && !byId("fetch").classList.contains("off") && !byId("link-extra").hidden;  // a pasted link is detected, no tab to pick
    put("★☆☆☆☆ ukurannya kekecilan" + NL + "★★☆☆☆ bahannya tipis");  // review text is not read any more
    byId("fetch").click();
    out.text = { off: byId("fetch").classList.contains("off"), chip: byId("detect").hidden, err: byId("url-error-text").textContent };
    put("https://shopee.co.id/produk-i.1.2");
    out.shopee = { err: byId("url-error-text").textContent, chip: byId("detect").hidden, off: byId("fetch").classList.contains("off") };  // a Shopee link says "not yet" and promises nothing
    put("https://www.tokopedia.com/toko/kaos-boxy");
    out.shopeeCleared = byId("url-error").hidden;
    byId("fetch").click();
    await until(() => !byId("view-results").hidden, 80);
    out.locked = { unlock: vis("unlock"), panels: shown("panels"), counts: [...document.querySelectorAll("#tabs button .n")].map((n) => n.textContent), summary: byId("summary").textContent };
  }

  if (hash === "#rival-visitor") {
    await until(() => !byId("workspace").hidden);
    out.noEntry = [byId("rivals-open").offsetParent === null || byId("view-results").hidden, document.querySelectorAll(".bottombar .nav").length];  // no result, no competitor button; two tabs only
    openRivals({ from: "results", url: "https://www.tokopedia.com/toko/kaos", title: "Kaos" });  // whatever the way in, a visitor is asked to sign in first
    await wait(200);
    out.gate = vis("gate"); out.info = byId("g-info").textContent;
    byId("g-email").value = "ani@example.com"; byId("g-send").click();
    await until(() => vis("g-verify")); paste("123456");
    await until(() => vis("g-intro")); byId("g-skip").click();
    await until(() => !byId("workspace").hidden);
    out.after = { gate: vis("gate"), app: !byId("workspace").hidden };
  }

  if (hash === "#rival-flow") {
    await until(() => !byId("workspace").hidden);
    const real = window.fetch; window.__paths = [];
    const cand = (n, picked) => ({ url: "https://www.tokopedia.com/shop" + n + "/p" + n, title: "Kaos Pesaing " + n, shop: "Toko " + n, price: 89000, rating: 4.8, sold: 1200, picked });
    window.fetch = (u, o) => {
      const path = String(u);
      if (path.includes("/api/competitors/suggest")) { window.__paths.push("suggest"); return Promise.resolve(new Response(JSON.stringify({ candidates: [cand(1, true), cand(2, false), cand(3, false)] }), { status: 200, headers: { "Content-Type": "application/json" } })); }
      if (path.includes("/api/competitors/compare")) { window.__paths.push("compare:" + JSON.parse(o.body).competitors.length); const d = JSON.parse(JSON.stringify(EXAMPLE_COMPARE)); d.quota = { used: 2, max: 10 }; return Promise.resolve(new Response(JSON.stringify(d), { status: 200, headers: { "Content-Type": "application/json" } })); }
      return real(u, o);
    };
    runSample();
    await until(() => !byId("view-results").hidden, 80);
    byId("done-btn").click();   // the product gets its box first
    await until(() => vis("view-fixes") && !vis("done-anim"), 80);
    byId("nav-check").click();
    byId("rivals-open").click();   // pasted reviews have no link: the form asks for it
    out.form = [vis("view-rival"), !!byId("rival-url"), byId("rival-name").value];
    byId("rival-url").value = "https://www.tokopedia.com/toko/kaos-boxy"; byId("rival-url").dispatchEvent(new Event("input"));
    [...byId("rival-body").querySelectorAll("button.btn.primary")].pop().click();
    out.searching = !!byId("rival-body").querySelector(".spinner") || byId("rival-body").textContent.includes("Mencari produk serupa");
    await until(() => byId("rival-body").querySelector(".pick"), 60);
    const rows = () => [...byId("rival-body").querySelectorAll(".pick input")].map((c) => c.checked);
    out.pick = { title: byId("rival-body").querySelector("h2").textContent, rows: rows(), go: byId("rival-go").textContent, quota: byId("rival-body").textContent.includes("Pakai 1 jatah analisis hari ini"), back: byId("rival-body").querySelector(".back").textContent };
    byId("rival-body").querySelectorAll(".pick input")[1].click();
    out.two = [rows(), byId("rival-go").textContent];
    byId("rival-body").querySelectorAll(".pick input")[2].click();   // a third is refused: 1 or 2 shops
    out.three = rows();
    byId("rival-go").click();
    await until(() => byId("rival-body").querySelector(".sumbox"), 60);
    out.result = { head: byId("rival-body").querySelector("h2").textContent, tabs: [...byId("rival-body").querySelectorAll(".gtab")].map((x) => x.textContent), paths: window.__paths.slice(), selesai: !![...byId("rival-body").querySelectorAll(".res-end .btn.primary")].find((b) => !b.hidden) };
    [...byId("rival-body").querySelectorAll(".res-end .btn.primary")].pop().click();
    out.anim = vis("done-anim");
    await until(() => vis("view-fixes") && !vis("done-anim"), 80);
    out.tasks = { boxes: document.querySelectorAll(".tbox, .trow").length, from: [...document.querySelectorAll(".tbox .sechd")].map((x) => x.textContent), items: document.querySelectorAll(".tbox .it").length, text: document.querySelector(".tbox").textContent };
    window.fetch = real;
  }

  if (hash === "#free-google") {
    await until(() => !byId("workspace").hidden);
    if (!window.__back) {
      runSample();
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
    out.fixesLabel = byId("fixes-btn").querySelector(".lbl").textContent; out.fixesMenuHidden = byId("acct-fixes").offsetParent === null;  // a wide enough header shows the word; the menu item is for phones
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
    showApp();
    localStorage.setItem("bedahreview-free-used", "1");  // the free analysis was already used in this browser
    show("input"); runSample();
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
                                     supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x", free_per_visitor=50, fetcher=LinkFetch())
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
        self.assertTrue(out["histHidden"])  # signed out: no Riwayat in the menu or on Tugas
        self.assertEqual(out["start"], {"gate": False, "app": True, "signinBtn": True, "acct": True})  # a visitor gets the avatar menu too (theme, access code)  # the first analysis is free, so the app comes first
        self.assertEqual((out["gate"], out["workspaceHidden"]), (True, True))
        self.assertEqual(out["title"], "Sign In")  # owner, 2026-11-04: the title is just "Sign In", the line under it says why
        self.assertEqual(out["google"], "Masuk dengan Google")
        self.assertTrue(out["consent"])
        self.assertTrue(out["twoChoices"])  # Google, email and "Nanti saja" only
        self.assertIn("acct-theme", out["menuVisitor"])  # the visitor's menu holds the theme switch
        self.assertFalse(out["noContactAsk"])  # no "may we contact you" question anywhere
        self.assertTrue(out["keepInMenu"])
        self.assertEqual(out["otp"], ["one-time-code", "numeric", "off", 6])
        self.assertEqual(out["titleEn"], "Sign in")
        self.assertEqual(out["oauth"], ["google", "http://127.0.0.1:%d/" % self.port])
        self.assertEqual((out["badEmail"], out["otpCalls"]), ("Alamat email tidak valid.", 0))  # a bad address never reaches Supabase

    def test_email_code_flow_then_skippable_intro_then_the_app(self):
        out = self.probe("#otp")
        self.assertEqual(out["sub"], "Kode dikirim ke ani@example.com. Belum masuk? Cek folder spam.")  # one short line: the long how-to paragraph overwhelmed a tester
        self.assertRegex(out["resend"], r"Kirim ulang kode \(\d+\)")  # the 60-second wait is shown
        self.assertTrue(out["resendDisabled"])
        self.assertEqual(out["wrong"], "Kodenya salah atau sudah habis waktunya. Minta kode baru.")
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

    def test_an_expired_sign_in_asks_again_instead_of_reloading_the_page(self):
        out = self.probe("#expired")
        self.assertTrue(out["appOpened"])
        self.assertTrue(out["gate"] and out["workspaceHidden"])
        self.assertTrue(out["runEnabled"])
        self.assertTrue(out["signedOutSession"])

    def test_history_saves_a_summary_then_selesai_makes_tasks_and_riwayat_says_so(self):
        out = self.probe("#history")
        self.assertTrue(out["menuShown"])
        self.assertEqual(sorted(out["row"]), sorted(["user_id", "product", "reviews", "fix_n", "fight_n", "ignore_n", "keep_n", "top_theme"]))  # exactly the columns, nothing else leaves the page
        self.assertEqual((out["row"]["user_id"], out["row"]["product"]), ("user-1111-2222", "Kaos Boxy Retro"))
        for word in ("koyak", "ijo", "hitem", "bagus", "tulisannya", "Halo Kak"):  # no review text, quote or draft in the saved row
            self.assertNotIn(word, out["rowText"])
        self.assertEqual(out["inserts"], 1)
        self.assertTrue(out["heldBefore"])  # the result is kept in this browser so it can be continued
        self.assertTrue(out["opened"] and out["view"] and out["noFooter"])  # no footer notes on Riwayat
        self.assertEqual([c[0] for c in out["cards"]], ["Kaos Boxy Retro", "Kaos Tengah"])  # newest first; the 70-day-old row was cleaned up
        mine, other = out["cards"]
        self.assertTrue(mine[1].startswith(out["expectDay"]) and "ulasan" in mine[1])
        self.assertEqual((mine[2], mine[3]), ("Belum selesai", ["Lanjutkan"]))
        self.assertEqual((other[2], other[3]), ("", ["Bedah lagi"]))  # a row this device did not make has no status
        self.assertTrue(out["oldGone"])
        self.assertGreaterEqual(out["prunes"], 2)  # once after saving, once on opening Riwayat
        self.assertEqual(set(out["cutoffDays"]), {60})
        self.assertEqual(out["resumed"], [True, "Hasil Bedah", 1])  # Lanjutkan is not a new analysis: nothing is saved again
        self.assertTrue(out["anim"])
        self.assertEqual(out["landedOn"], [True, False])  # Selesai goes to Tugas, and the box is already there
        self.assertEqual(out["landedBox"]["boxes"], 1)
        done = out["afterDone"]
        self.assertEqual(done["cards"][0][2], "Selesai")
        self.assertIn("tugas dibuat", done["cards"][0][1])
        self.assertEqual(done["cards"][0][3], ["Analisa Kompetitor", "Bedah lagi"])
        self.assertIn("tugas masuk", done["toast"])
        self.assertEqual(done["badge"], "2")  # the red count on Tugas: unticked items
        self.assertFalse(done["held"])  # finished: the held result is gone
        self.assertEqual((out["box"]["boxes"], out["box"]["items"], out["box"]["meta"], out["box"]["bar"]), (1, 2, "0 dari 2 selesai", "0"))
        self.assertRegex(out["box"]["chip"], r"^Sisa (29|30) hari$")
        self.assertTrue(out["box"]["cal"])
        for word in ("Tambah ke Kalender", "Analisa Kompetitor", "Ubah"):
            self.assertIn(word, out["boxText"])
        self.assertEqual((out["ticked"][0], out["ticked"][1], out["ticked"][2]), ("1 dari 2 selesai", "1", "1"))  # the checkbox is the only way to tick
        self.assertEqual(out["allDone"], [True, "Bedah ulang produk ini", True])  # the box turns green and offers the one useful next step
        for word in ("koyak", "ijo", "hitem", "bagus", "tulisannya", "Halo Kak", "Mohon maaf"):  # the box holds task sentences, never review text or drafts
            self.assertNotIn(word, out["stored"])
        self.assertEqual(out["again"], [True, ""])  # Bedah lagi on a row this device does not know: the link screen, empty

    def test_tugas_boxes_follow_the_account_to_another_phone(self):
        out = self.probe("#boxsync")
        self.assertEqual(out["a"]["rows"], 1)
        self.assertEqual(out["a"]["items"], [["fix", False], ["reply", False]])
        self.assertEqual(out["a"]["cols"], sorted(["user_id", "sig", "updated_at", "created_at", "done_at", "product", "label", "url", "items"]))  # exactly the columns
        for word in ("koyak", "ijo", "hitem", "bagus", "Halo Kak", "Mohon maaf", "rahasia"):  # task sentences only: no review text, quote or draft
            self.assertNotIn(word, out["a"]["text"])
        self.assertEqual(out["bBefore"], 0)
        self.assertEqual(out["bAfter"], [1, 2])  # an empty browser gets the box from the account
        self.assertEqual(out["bTick"], [True, False])  # a tick there is pushed
        self.assertEqual(out["aSees"], [2, True])  # the newer copy wins here
        self.assertEqual(out["removed"], 0)  # a box removed on another phone goes here too
        self.assertEqual(out["pushedAgain"], 1)
        self.assertEqual(out["leave"], [0, 1, None])  # sign-out clears this browser, never the account

    def test_tugas_still_works_when_the_account_table_cannot_be_reached(self):
        out = self.probe("#boxdown")
        self.assertEqual(out["local"], [1, 1, "down"])

    def test_events_wait_in_a_queue_and_go_out_together(self):
        out = self.probe("#events")
        self.assertEqual(out["beforeHide"], 0)
        self.assertEqual(out["sent"], [{"events": ["opened", "tab_viewed", "tab_viewed", "draft_copied"], "keepalive": True, "auth": True}])  # one request, kept alive past the page, with the sign-in
        self.assertEqual(out["queueLeft"], 0)
        self.assertEqual((out["explicit"], out["total"]), (True, 2))  # the server accepted it, and it did not wait

    EXPECTED_COLUMNS = sorted(["user_id", "id", "updated_at", "theme", "variant", "product", "product_label", "note", "fixed_on", "before_count", "before_total", "before_sig", "after_count", "after_total", "after_sig", "after_on", "check_on", "remind"])

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

    def test_analysing_after_the_free_analysis_is_used_asks_for_a_sign_in(self):
        out = self.probe("#example")
        self.assertTrue(out["gateAfterRun"])  # the free analysis was used: analysing asks for a sign-in
        self.assertEqual(out["info"], "Analisis gratismu sudah terpakai. Masuk untuk lanjut, tetap gratis.")
        self.assertTrue(out["closeShown"] and out["backInApp"])

    def test_one_free_analysis_shows_the_counts_and_a_sign_in_shows_the_rest(self):
        out = self.probe("#free")
        self.assertEqual(out["start"], {"gate": False, "signinBtn": True, "acct": True})
        self.assertEqual(out["locked"], {"unlock": True, "panels": False, "counts": 4, "flag": "1"})  # the four numbers show, the details wait
        self.assertTrue(out["tabPointsToSignIn"])
        self.assertEqual(out["second"], {"gate": True, "info": "Analisis gratismu sudah terpakai. Masuk untuk lanjut, tetap gratis.", "close": True})
        after = out["after"]
        self.assertEqual((after["unlock"], after["panels"], after["acct"], after["signinBtn"]), (False, True, True, False))
        self.assertEqual(after["email"], "ani@example.com")  # shown in this browser's menu only
        self.assertIn("free_unlocked", after["queued"])
        self.assertIn("analysed", after["queued"])  # the free analysis now counts for the account
        self.assertEqual(after["queued"].count("analysed"), 1)  # drawn again in full, but counted once
        self.assertTrue(after["plan"])  # Selesai and the competitor button show once the result is unlocked

    def test_a_visitor_can_use_a_product_link_for_the_free_analysis(self):
        out = self.probe("#free-link")
        self.assertTrue(out["emptyFirst"] and out["linkFirst"])  # one box: a link is detected by itself
        self.assertEqual(out["text"], {"off": True, "chip": True, "err": "Itu tidak terlihat seperti tautan."})  # review text is not read: the box asks for a link
        self.assertEqual(out["shopee"], {"err": "Link Shopee belum didukung. Pakai link produk dari Tokopedia atau TikTok Shop.", "chip": True, "off": True})
        self.assertTrue(out["shopeeCleared"])  # a good link takes the notice away
        self.assertTrue(out["noDemo"])  # no "demo" or cost talk in front of sellers
        self.assertEqual((out["locked"]["unlock"], out["locked"]["panels"]), (True, False))
        self.assertEqual(out["locked"]["counts"], ["1", "1", "1", "0"])  # the four numbers show (Perbaiki, Balas, Pertahankan, Abaikan); the quotes and drafts wait for a sign-in
        self.assertIn("Kaos Boxy Retro", out["locked"]["summary"])

    def test_a_visitor_is_asked_to_sign_in_for_competitors(self):
        out = self.probe("#rival-visitor")
        self.assertEqual(out["noEntry"], [True, 2])
        self.assertEqual((out["gate"], out["info"]), (True, "Masuk dulu untuk bandingkan dengan pesaing. Gratis, tanpa password."))
        self.assertEqual(out["after"], {"gate": False, "app": True})

    def test_competitor_flow_searches_picks_one_or_two_and_adds_items_to_the_same_box(self):
        out = self.probe("#rival-flow")
        self.assertEqual(out["form"], [True, True, "Kaos Boxy Retro"])
        self.assertTrue(out["searching"])
        self.assertEqual(out["pick"]["title"], "Pilih 1 atau 2 pesaing")
        self.assertEqual(out["pick"]["rows"], [True, False, False])  # the best match is already ticked
        self.assertEqual((out["pick"]["go"], out["pick"]["quota"], out["pick"]["back"]), ("Bandingkan (1 dipilih)", False, "Hasil Bedah"))  # no small "Pakai 1 jatah" line under the button (owner, 2026-11-04)
        self.assertEqual((out["two"][0], out["two"][1]), ([True, True, False], "Bandingkan (2 dipilih)"))
        self.assertEqual(out["three"], [True, True, False])  # a third shop is refused
        self.assertEqual(out["result"]["head"], "Hasil Bedah pesaing")
        self.assertEqual(out["result"]["tabs"], ["Perlu dikejar3", "Kamu unggul1"])
        self.assertEqual(out["result"]["paths"], ["suggest", "compare:2"])
        self.assertTrue(out["result"]["selesai"])
        self.assertTrue(out["anim"])
        self.assertEqual(out["tasks"]["boxes"], 2)  # the paste analysis had its own box (folded into one line); this one is for the product named in the form
        self.assertEqual(out["tasks"]["from"], ["DARI PESAING"])
        self.assertEqual(out["tasks"]["items"], 3)

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
        self.assertIn(out["items"][1], ("Mode gelap", "Mode terang"))  # the label follows the current theme
        self.assertEqual(out["items"][:1] + out["items"][2:], ["Riwayat bedah", "Kebijakan privasi", "Syarat penggunaan", "Keluar", "Hapus akun dan data saya"])  # no "Bedah produk lain" or "Apa arti 4 kelompok?" any more (owner, 2026-11-04); Cetak waits for a result; delete last and apart
        self.assertIn("Kebijakan privasi", out["items"])
        self.assertEqual(out["items"][-1], "Hapus akun dan data saya")  # last, apart from the rest
        self.assertTrue(out["deleteRed"])
        self.assertEqual((out["fixesLabel"], out["fixesMenuHidden"]), ("Tugas", True))
        self.assertTrue(out["escClosed"] and out["outsideClosed"])
        self.assertFalse(out["oldFooter"])  # no longer at the bottom of the results


if __name__ == "__main__":
    unittest.main()
