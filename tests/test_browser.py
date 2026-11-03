"""Headless Chrome checks of the installed-app shell: manifest, service worker, Share-menu pre-fill, results board.

Skipped when Chrome is not installed. The page is served by the real server with a stub model; a small script is
appended to a temp copy of index.html and leaves what the page did in window.__probe. Chrome is driven over its
DevTools socket in real time (a service worker needs real time to install), with a tiny stdlib WebSocket client.
"""
import base64
import contextlib
import io
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
from urllib.parse import quote, unquote, urlparse

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
            kit = {"why": "Likely transit damage.", "steps": ["Ask for photos."], "why_id": "Kemungkinan rusak di pengiriman.", "steps_id": ["Minta foto."], "chat_to_buyer": "Halo Kak, mohon maaf 🙏", "public_reply": "Mohon maaf, Kak.", "report_text": None}
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
  out.product = document.getElementById("product").value;
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
    out.aiNotes = [...document.querySelectorAll(".ai-note")].map((n) => n.closest("[role=tabpanel]").id);  // one note above the drafts of each tab
    out.footHidden = document.getElementById("foot").hidden; out.noDev = !/analytics\.md|Model:|Demo:/.test(document.body.innerText);
    const cache = await caches.open("bedahreview-shell-v2");
    out.cachedPaths = (await cache.keys()).map((r) => new URL(r.url).pathname);
    out.copyButtons = document.querySelectorAll(".copy").length;
    out.fixQuote = (document.querySelector("#panel-fix blockquote") || {}).textContent;
    document.getElementById("lang").click();
    out.enTabs = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    out.enLang = document.documentElement.lang;
    out.draftStillBahasa = (document.querySelector("#panel-fix .todo li") || {}).textContent;
    out.whyEn = (document.querySelector("#panel-fight .why") || {}).textContent;
    out.stepsEn = [...document.querySelectorAll("#panel-fight .todo li")].map((li) => li.textContent);
    document.getElementById("lang").click();
    out.whyId = (document.querySelector("#panel-fight .why") || {}).textContent;
    out.stepsId = [...document.querySelectorAll("#panel-fight .todo li")].map((li) => li.textContent);
    out.draftAfterBack = (document.querySelector("#panel-fight .draft .bahasa") || {}).textContent;
  }
  if (location.hash === "#desktop") {
    out.noAside = document.getElementById("feature2") === null && document.getElementById("ptabs") === null;
    document.getElementById("sample").click();
    document.getElementById("run").click();
    for (let i = 0; i < 40 && document.getElementById("view-results").hidden; i++) await wait(100);
    const res = document.getElementById("view-results"), sum = document.getElementById("sumbox");
    out.cols = getComputedStyle(res).gridTemplateColumns.split(" ").map(parseFloat);
    out.sticky = getComputedStyle(sum).position;
    out.sumSide = sum.getBoundingClientRect().left > document.getElementById("tabs").getBoundingClientRect().right - 1;
    out.sumLabel = (document.querySelector("#sumbox .eb") || {}).textContent || "";
    out.board = [...document.querySelectorAll("#tabs button")].map((t) => t.textContent);
    out.pills = [...document.querySelectorAll("#check-btn, #fixes-btn")].map((b) => getComputedStyle(b).display !== "none");
    out.bottom = getComputedStyle(document.querySelector(".bottombar")).display;
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
    document.getElementById("new-mob").click();
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
    const listBtn = (t) => [...document.querySelectorAll("#fixes-list button")].find((b) => b.textContent === t);
    out.fl.hiddenAtFirst = !listBtn("Ubah") && !listBtn("Hapus");  // Ubah and Hapus wait behind one small Lainnya link
    listBtn("Lainnya").click();
    const del = () => listBtn("Hapus") || listBtn("Yakin hapus?");
    del().click(); del().click();
    out.fl.afterDelete = [JSON.parse(localStorage.getItem("bedahreview-fixes-v1")).length, document.getElementById("fixes-count").hidden];
  }

  if (location.hash === "#planflow") {
    localStorage.removeItem("bedahreview-fixes-v1"); localStorage.removeItem("bedahreview-plans-v1");
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    const btn = (sel, text) => [...document.querySelectorAll(sel)].find((b) => b.textContent === text);
    document.getElementById("sample").click();
    document.getElementById("run").click();
    await until(() => !document.getElementById("view-results").hidden);
    out.pl = { rows: [...document.querySelectorAll("#start .prow b")].map((b) => b.textContent), headline: document.getElementById("results-h").textContent, badge: document.getElementById("tasks-count").textContent };
    document.getElementById("tab-fight").click();  // a tile opens the sheet with that group
    out.pl.sheet = [!document.getElementById("sheet").hidden, document.getElementById("sheet-h").textContent, !document.getElementById("panel-fight").hidden];
    document.getElementById("sheet-x").click(); await wait(450);
    out.pl.sheetClosed = document.getElementById("sheet").hidden;
    document.getElementById("plan-go").click();
    out.pl.step1 = [!document.getElementById("view-plan").hidden, document.getElementById("plan-h").textContent];
    document.querySelector("input[name=fixopt][value=own]").click();
    const own = document.querySelector("#view-plan .own input"); own.value = "ganti foto katalog"; own.dispatchEvent(new Event("input"));
    out.pl.step1Ui = [document.querySelectorAll("#view-plan .seg").length, document.querySelector("#view-plan .checkline").textContent === "Cek lagi tanggal " + fmtDayShort(addDays(7)) + ".", document.body.innerText.indexOf("Ingatkan") < 0];
    btn("#view-plan .stickyfoot button", "Simpan").click();
    const fix = JSON.parse(localStorage.getItem("bedahreview-fixes-v1"))[0];
    out.pl.fix = [fix.note, fix.checkOn === addDays(7), "remind" in fix, fix.theme];
    out.pl.toast = document.getElementById("toast2").textContent.indexOf("Tersimpan di Tugas") === 0;
    out.pl.step2 = document.getElementById("plan-h").textContent;
    [...document.querySelectorAll("#view-plan .reply button")].find((b) => b.textContent.indexOf("Salin") === 0).click();
    await until(() => document.querySelector("#view-plan .ask"));
    out.pl.asked = document.querySelector("#view-plan .ask p").textContent;
    btn("#view-plan .ask button", "Sudah").click();
    out.pl.sent = [document.querySelectorAll("#view-plan .reply.sent").length, JSON.parse(localStorage.getItem("bedahreview-plans-v1"))[0].replies];
    out.pl.foot2 = document.querySelector("#view-plan .stickyfoot .of").textContent;
    btn("#view-plan .stickyfoot button", "Lanjut").click();
    out.pl.sum = [document.getElementById("plan-h").textContent, [...document.querySelectorAll("#view-plan .nextlist li")].map((li) => li.className + ":" + li.textContent),
      [...document.querySelectorAll("#view-plan .stepper li .full")].map((s) => s.textContent), [...document.querySelectorAll("#view-plan .sumfoot button")].map((b) => b.textContent)];
    out.pl.planText = localStorage.getItem("bedahreview-plans-v1");
    const waA = document.querySelector("#view-plan .wabtn");
    out.pl.waSummary = waA ? [waA.href, waA.target, waA.rel] : null;
    document.getElementById("nav-tasks").click();
    out.pl.tasks = [!document.getElementById("view-fixes").hidden, [...document.querySelectorAll("#tasks-body .task b")].map((b) => b.textContent), document.getElementById("tasks-count").textContent];
    out.pl.watched = document.getElementById("fixes-list").textContent.indexOf("ganti foto katalog") >= 0;
    const card = document.querySelector("#fixes-list .trk");
    out.pl.card = { act: card.querySelector(".act").textContent, buttons: [...card.querySelectorAll("button")].map((b) => b.textContent), small: card.querySelector(".small").textContent, big: card.querySelectorAll(".btn.big").length };
    out.pl.waTasks = !!document.querySelector("#tasks-body .wabtn");
    out.pl.waNote = (document.querySelector("#tasks-body .wablock .small") || {}).textContent;
    out.pl.more = [!document.getElementById("fixes-more").hidden, document.getElementById("fixes-export").textContent, document.getElementById("fixes-import").textContent, document.querySelector("#fixes-more summary").textContent];
    [...card.querySelectorAll("button")].find((b) => b.textContent === "Sudah dikerjakan").click();
    const done = document.querySelector("#fixes-list .trk");
    out.pl.afterDone = { ticked: !!done.querySelector(".sentrow .ok"), stored: JSON.parse(localStorage.getItem("bedahreview-fixes-v1"))[0].done, nothingToSend: !document.querySelector("#tasks-body .wabtn") };
  }

  if (location.hash === "#checkup") {  // found by a click-path audit on 2026-11-02
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    const runSample = async () => { document.getElementById("sample").click(); document.getElementById("run").click(); await until(() => !document.getElementById("view-results").hidden); };
    // a check-up that has come: analysing new reviews of the product moves the next one a week on, so the card and the count do not stay "due" forever
    saveFixes([{ id: "old1", theme: "Colour vs photo", variant: "", product: "kaos boxy retro", productLabel: "Kaos Boxy Retro", note: "ganti foto", date: addDays(-8), updated: 1,
      before: { count: 3, total: 6, sig: "zz.6", date: "" }, after: null, checkOn: addDays(-1), done: false }]);
    await runSample();
    const f = loadFixes()[0];
    out.cu = { after: !!f.after, checkOn: f.checkOn === addDays(7), due: isDue(f) };
    // a fix marked in the Fix sheet gets the same week-away check date as step 1 of the plan
    saveFixes([]); render(lastData, false);
    document.getElementById("tab-fix").click();
    [...document.querySelectorAll("#panel-fix .fixform > .btn")].find((b) => b.textContent === tr("fixMark")).click();
    document.querySelector("#panel-fix .fixform .btn.primary").click();
    out.cu.sheetCheckOn = loadFixes()[0].checkOn === addDays(7);
    closeSheet();
    // "Bedah lagi" for another product starts from an empty box, never the last product's reviews
    reanalyse("", "Sepatu Lari");
    out.cu.again = [document.getElementById("paste").value, document.getElementById("product").value, currentView];
    reanalyse("", "");
    out.cu.noName = document.getElementById("product").value;
  }

  if (location.hash === "#tasksinplace") {  // a task is finished on the Tugas screen, never back in the analysis
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    const tbtn = (t) => [...document.querySelectorAll("#tasks-body button")].find((b) => b.textContent.trim() === t);
    const titles = () => [...document.querySelectorAll("#tasks-body .task b")].map((b) => b.textContent);
    const task = (word) => [...document.querySelectorAll("#tasks-body .task")].find((b) => b.textContent.indexOf(word) >= 0);
    document.getElementById("product").value = "Baju Oblong Wanita POLOS Lengan Panjang Bahan Spandek Distro Printing Premiun Atasan Baju Kasual";
    const gap = String.fromCharCode(10, 10);
    document.getElementById("paste").value = ["warna tulisannya ijo, pas dateng warna tulisannya hitem", "Pas sampe koyak bajunya", "bahannya tebel, bagus"].join(gap);
    document.getElementById("run").click();
    await until(() => !document.getElementById("view-results").hidden);
    openFixes();
    out.tp = { titles: titles(), sub: document.querySelector("#tasks-body .task .t span").textContent };
    task("Balas").click();
    out.tp.fight = { view: currentView, draft: !!document.querySelector(".taskpanel .pdraft"), buttons: [...document.querySelectorAll(".taskpanel button")].map((b) => b.textContent.trim()) };
    tbtn("Sudah dibalas").click();
    task("Catat").click();
    out.tp.prefilled = document.querySelector(".taskpanel input").value;  // the AI's step, while the analysis is still here
    document.querySelector(".taskpanel input").value = "ganti foto varian";
    tbtn("Simpan & pantau").click();
    const fx = loadFixes()[0];
    out.tp.fix = [fx.note, fx.checkOn === addDays(7), fx.before.total > 0, currentView];
    // a reply step from an analysis no longer on the page: no drafts, just "done" or take it off the list
    savePlans(loadPlans().concat(cleanPlan({ sig: "zz9.3", created: Date.now(), product: "produk x", label: "Produk X", fight: ["1"], replies: {} })));
    renderFixes();
    task("Balas").click();
    out.tp.noData = [!!document.querySelector(".taskpanel .pdraft"), !!tbtn("Sudah kubalas semua")];
    tbtn("Hapus dari Tugas").click();
    out.tp.left = [titles(), currentView, document.getElementById("tasks-count").hidden];
    // "Bedah produk lain" at the end: empty fields and an empty competitor panel
    document.getElementById("url").value = "https://www.tokopedia.com/toko/x";
    startFresh();
    out.tp.fresh = [currentView, document.getElementById("url").value, document.getElementById("paste").value, document.getElementById("back-results").hidden, lastData];
    out.tp.short = [shortLabel("Kaos"), shortLabel("Baju Oblong Wanita POLOS Lengan Panjang Bahan Spandek Distro Printing")];
    // after a comparison, "Mulai ulang" gives an empty form, not the last product again
    rivalForm = { url: "https://www.tokopedia.com/toko/lama", name: "lama" };
    [...document.querySelectorAll("#rival-body button")].find((b) => b.textContent.indexOf("contoh") >= 0).click();
    [...document.querySelectorAll("#rival-body button")].find((b) => b.textContent.trim() === "Mulai ulang").click();
    out.tp.again = [document.getElementById("rival-url").value, document.getElementById("rival-name").value];
  }

  if (location.hash === "#history") {
    const items = [
      { id: "1", bucket: "fix", theme: "Colour vs photo", text: "rahasia satu ijo", proof_quote: "rahasia satu ijo", fix_suggestion_id: "saran khusus", likely_fake: { flag: false } },
      { id: "2", bucket: "fight", theme: "Packaging / damaged on arrival", text: "rahasia dua koyak", proof_quote: "rahasia dua koyak", fight: { public_reply: "balasan khusus" }, likely_fake: { flag: false } },
      { id: "3", bucket: "keep", theme: "Praise: quality / material", text: "rahasia tiga", proof_quote: "rahasia tiga", likely_fake: { flag: false } }];
    const data = { items, reviews: 3, groups: [{ theme: "Colour vs photo", variant: "Putih", ids: ["1"], n: 1 }], source: { kind: "paste", title: "Kaos  Boxy " + "x".repeat(200) } };
    const row = historyRow(data, { fix: 1, fight: 1, ignore: 0, keep: 1 }, "u-1");
    out.hist = { keys: Object.keys(row), cols: HISTORY_COLS, text: JSON.stringify(row), theme: row.top_theme, product: row.product.length, counts: [row.reviews, row.fix_n, row.fight_n, row.ignore_n, row.keep_n],
      oddTheme: historyRow(Object.assign({}, data, { groups: [{ theme: "made up" }] }), {}, "u").top_theme, noGroup: historyRow(Object.assign({}, data, { groups: [] }), {}, "u").top_theme,
      hidden: [document.getElementById("acct-history").hidden, document.getElementById("tasks-history").hidden], days: HISTORY_DAYS };
    openFixes(); out.hist.tasksLink = document.getElementById("tasks-history").hidden;
    await openHistory(); out.hist.noScreen = document.getElementById("view-history").hidden;
  }

  if (location.hash === "#events") {
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    document.getElementById("sample").click();
    document.getElementById("run").click();
    await until(() => !document.getElementById("view-results").hidden);
    document.getElementById("tab-fight").click();
    document.querySelector("#panel-fight .copy").click();
    const fb = document.getElementById("feedback");
    out.ev = { askShown: !fb.hidden, textareaBeforeAnswer: !!fb.querySelector("textarea") };
    [...fb.querySelectorAll("button")].find((b) => b.dataset.a === "partly").click();
    let area = fb.querySelector("textarea");
    area.value = "warna tulisannya ijo, pas dateng warna tulisannya hitem";  // copied from a review: must be refused
    area.dispatchEvent(new Event("input"));
    [...fb.querySelectorAll("button")].find((b) => b.textContent === "Kirim").click();
    await wait(200);
    out.ev.refused = fb.textContent.indexOf("teks ulasan") >= 0;
    area = fb.querySelector("textarea");
    area.value = "cukup berguna";
    area.dispatchEvent(new Event("input"));
    [...fb.querySelectorAll("button")].find((b) => b.textContent === "Kirim").click();
    await until(() => fb.textContent.indexOf("Terima kasih") >= 0);
    out.ev.thanks = fb.textContent.indexOf("Terima kasih") >= 0;
    window.dispatchEvent(new Event("pagehide"));  // the page being hidden sends everything that was queued, in one batch
    await wait(500);
  }

  if (location.hash === "#sections") { try {
    const until = async (fn) => { for (let i = 0; i < 60 && !fn(); i++) await wait(100); };
    await until(() => linkBoxOn === false);
    document.getElementById("sample").click();
    document.getElementById("run").click();
    await until(() => !document.getElementById("view-results").hidden);
    out.sec = { openVisible: getComputedStyle(document.getElementById("rivals-open")).display !== "none", gone: [document.getElementById("nav-rivals"), document.getElementById("ptabs"), document.getElementById("feature2")].map((e) => e === null),
      bottom: [...document.querySelectorAll(".bottombar .nav")].map((n) => n.id) };
    document.getElementById("rivals-open").click();  // pasted reviews have no link: a short form asks for it
    out.sec.view = [currentView, !document.getElementById("view-rival").hidden];
    out.sec.nameFilled = document.getElementById("rival-name").value;
    out.sec.back = document.querySelector("#view-rival .back").textContent;
    [...document.querySelectorAll("#rival-body button")].find((b) => b.textContent.indexOf("contoh") >= 0).click();
    const body = document.getElementById("rival-body");
    out.sec.example = { head: body.querySelector("h2").textContent, tabs: [...body.querySelectorAll(".gtab")].map((x) => x.textContent), sum: !!body.querySelector(".sumbox"), notice: body.textContent.indexOf("Contoh, bukan data nyata") >= 0,
      doneHidden: [...body.querySelectorAll(".res-end .btn.primary")].every((b) => b.hidden) };
    out.sec.cards = body.querySelectorAll(".rcard").length;
    body.querySelector(".back").click();
    out.sec.returned = [currentView, !document.getElementById("view-results").hidden];
    const g = groupInsights(EXAMPLE_COMPARE.insights, EXAMPLE_COMPARE.rows);
    out.sec.groups = [g.learn.length, g.avoid.length, g.ignore];
    const st = { checked: [0, 1, 2], candidates: [0, 1, 2].map((i) => ({ url: "https://www.tokopedia.com/s/p" + i, title: "t" + i })), extra: "https://www.tokopedia.com/x/y" };
    out.sec.chosen = [rivalChosen(Object.assign({ want: 1 }, st)).length, rivalChosen(Object.assign({ want: 2 }, st)).length, rivalChosen(st).length];
  } catch (e) { out.err = String(e && e.stack || e); } }
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

    def test_share_with_title_and_text_puts_the_title_in_the_product_name(self):
        out = self.probe("/?title=" + quote("Kaos Boxy") + "&text=" + quote("bagus banget"))
        self.assertEqual((out["prefill"], out["product"]), ("bagus banget", "Kaos Boxy"))  # the title is not counted as a review

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
        self.assertEqual([out["tabs"][0], out["tabs"][1], out["tabs"][3]], ["Perbaiki2", "Tanggapi1", "Abaikan0"])  # four group tabs: Perbaiki, Tanggapi, Pertahankan, Abaikan
        self.assertTrue(out["tabs"][2].startswith("Pertahankan"), out["tabs"])
        self.assertEqual(out["cards"], 1)  # the two colour complaints become one card
        self.assertIn("Kurir? Cek dulu", out["chips"])
        self.assertGreaterEqual(out["keep"], 1)  # praise sits in its own Pertahankan tab
        self.assertEqual(out["aiNotes"], ["panel-fix", "panel-fight"])
        self.assertTrue(out["footHidden"] and out["noDev"])  # pasted reviews: no footer note, and no developer text anywhere
        self.assertIn("/", out["cachedPaths"])  # the app shell is cached...
        self.assertFalse([p for p in out["cachedPaths"] if p.startswith("/api/")], out["cachedPaths"])  # ...reviews never are
        self.assertGreaterEqual(out["copyButtons"], 2)  # chat and public reply (a fix has nothing to copy)
        self.assertEqual([out["enTabs"][0], out["enTabs"][1], out["enTabs"][3]], ["Fix2", "Respond1", "Ignore0"])
        self.assertTrue(out["enTabs"][2].startswith("Keep"), out["enTabs"])
        self.assertEqual(out["enLang"], "en")
        self.assertEqual(out["draftStillBahasa"], "Ganti foto varian.")  # drafts stay Bahasa in English mode
        self.assertIn("Likely transit damage.", out["whyEn"])  # the explanation and steps follow the page language...
        self.assertEqual(out["stepsEn"], ["Ask for photos."])
        self.assertIn("Kemungkinan rusak di pengiriman.", out["whyId"])
        self.assertEqual(out["stepsId"], ["Minta foto."])
        self.assertTrue(out["draftAfterBack"].startswith("Halo Kak"))  # ...while the reply to the buyer is always Bahasa

    def test_wide_screen_has_header_pills_and_two_columns_with_a_sticky_summary(self):
        out = self.probe("/#desktop", width=1300)
        self.assertTrue(out["noAside"])  # the competitor column and the two-tab switch are gone
        self.assertEqual(len(out["cols"]), 2)
        self.assertEqual(round(out["cols"][1]), 380)  # content left, the summary 380 px on the right
        self.assertEqual(out["sticky"], "sticky")
        self.assertTrue(out["sumSide"])
        self.assertEqual(out["sumLabel"], "RINGKASAN · 2 HAL PERLU DIPERBAIKI")  # the summary box names how many things to fix
        self.assertEqual(out["board"][:2], ["Perbaiki2", "Tanggapi1"])
        self.assertEqual(out["pills"], [True, True])  # Bedah ulasan and Tugas pills in the header...
        self.assertEqual(out["bottom"], "none")  # ...instead of the bottom bar
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

    @unittest.skip("the plan card and fix tracker are gone from Hasil Bedah; the Selesai and Tugas steps of the R11 revision rebuild this")
    def test_marking_a_fix_then_analysing_new_reviews_shows_before_and_after(self):
        fl = self.probe("/#fixflow")["fl"]
        self.assertEqual((fl["n"], fl["theme"], fl["variant"], fl["note"], fl["before"]), (1, "Colour vs photo", "Putih", "foto varian diganti", [2, 5]))
        self.assertTrue(fl["noNull"])
        self.assertTrue(fl["noText"])  # only counts and the seller's own note are stored, no review text
        self.assertTrue(fl["cardAfterSave"])
        self.assertEqual(fl["badge"], "1")  # Tugas counts open steps: the fix is logged, a reply is left
        self.assertTrue(fl["comparedNote"])
        self.assertEqual(fl["after"], [1, 2])
        self.assertEqual(fl["listShows"], [True, True, True, True])
        self.assertTrue(fl["hiddenAtFirst"])
        self.assertEqual(fl["afterDelete"], [0, False])  # the fix is gone; the open steps keep the Tugas count

    @unittest.skip("the plan card and fix tracker are gone from Hasil Bedah; the Selesai and Tugas steps of the R11 revision rebuild this")
    def test_results_become_a_plan_of_two_steps_and_a_summary(self):
        pl = self.probe("/#planflow")["pl"]
        self.assertEqual(pl["rows"], ["Catat perbaikan warna beda dari foto", "Balas 1 ulasan buruk"])  # the praise is in the summary, not a step
        self.assertEqual(pl["headline"], "Keluhan terbanyak cuma satu: warna beda dari foto.")
        self.assertEqual(pl["badge"], "2")  # two open steps on the Tugas tab
        self.assertEqual(pl["sheet"], [True, "Tanggapi · 1 ulasan", True])  # a tile opens the sheet with that group's quotes and drafts
        self.assertTrue(pl["sheetClosed"])
        self.assertEqual(pl["step1"], [True, "Catat perbaikan untuk warna beda dari foto"])
        self.assertEqual(pl["step1Ui"], [0, True, True])  # no +7/14/30 choice and no reminder question: one line says when we check again
        self.assertEqual(pl["fix"], ["ganti foto katalog", True, False, "Colour vs photo"])  # the seller's own step, a check date 7 days out, no reminder field at all
        self.assertTrue(pl["toast"])
        self.assertEqual(pl["step2"], "Balas 1 ulasan buruk")
        self.assertEqual(pl["asked"], "Sudah kamu kirim?")  # a reply only counts once the seller says it went out
        self.assertEqual(pl["sent"], [1, {"4": "sent"}])
        self.assertEqual(pl["foot2"], "Langkah 2 dari 3")  # Catat, Balas, then the summary as step 3
        self.assertEqual(pl["sum"][0], pl["headline"])  # step 3 is a short summary of this analysis, in the results' own words
        self.assertEqual(pl["sum"][1][1], "done:Semua balasan terkirim")
        self.assertTrue(pl["sum"][1][0].startswith("done:Perbaikan warna beda dari foto dicatat. Cek lagi "), pl["sum"][1])
        self.assertTrue(pl["sum"][1][2].startswith("keep:Pertahankan yang dipuji pembeli: "), pl["sum"][1])
        self.assertEqual(len(pl["sum"][1]), 3)  # short: no more than the things to do and the one thing to keep
        self.assertEqual(pl["sum"][2], ["Catat", "Balas", "Ringkasan"])
        self.assertEqual(pl["sum"][3], ["Kembali ke hasil", "Bedah produk lain"])
        for word in ("koyak", "ijo", "bagus", "tulisannya"):  # the stored plan holds ids and counts, never review text
            self.assertNotIn(word, pl["planText"])
        self.assertEqual(pl["tasks"][:2], [True, []])  # both steps done: nothing left for today
        self.assertTrue(pl["watched"])  # the logged fix is tracked on the Tugas screen
        self.assertEqual(pl["card"]["act"], "ganti foto katalog")  # one sentence of action, one big button, one small line
        self.assertEqual(pl["card"]["buttons"], ["Sudah dikerjakan", "Lainnya"])
        self.assertTrue(pl["card"]["small"].startswith("Cek lagi "))
        self.assertEqual(pl["card"]["big"], 1)
        self.assertEqual(pl["more"], [True, "Simpan ke file", "Buka dari file", "Lainnya"])  # the file backup sits under one Lainnya link for people without an account
        self.assertEqual(pl["afterDone"], {"ticked": True, "stored": True, "nothingToSend": True})
        for where in ("waSummary", ):
            href, target, rel = pl[where]
            self.assertEqual((target, rel), ("_blank", "noopener noreferrer"))
            message = unquote(href[len("https://wa.me/?text="):])
            self.assertEqual(message.split("\n")[0], "Perbaikan BedahReview")
            self.assertRegex(message.split("\n")[-1], r"^- ganti foto katalog \(cek lagi \d")
            for word in ("koyak", "ijo", "bagus", "tulisannya", "\u201c"):  # no review quote can get into the message
                self.assertNotIn(word, message)
        self.assertTrue(pl["waTasks"])
        self.assertEqual(pl["waNote"], "WhatsApp terbuka dengan pesan siap kirim. Kami tidak mengirim apa pun sendiri.")

    @unittest.skip("the plan card and fix tracker are gone from Hasil Bedah; the Selesai and Tugas steps of the R11 revision rebuild this")
    def test_a_check_up_moves_on_and_every_new_fix_gets_a_check_date(self):
        cu = self.probe("/#checkup")["cu"]
        self.assertEqual(cu["after"], True)
        self.assertEqual(cu["checkOn"], True)  # the next check-up is a week after this one
        self.assertEqual(cu["due"], False)
        self.assertEqual(cu["sheetCheckOn"], True)
        self.assertEqual(cu["again"], ["", "Sepatu Lari", "input"])  # the old reviews are gone, the new name is in
        self.assertEqual(cu["noName"], "")

    @unittest.skip("the plan card and fix tracker are gone from Hasil Bedah; the Selesai and Tugas steps of the R11 revision rebuild this")
    def test_tasks_are_finished_in_place_and_the_end_starts_fresh(self):
        tp = self.probe("/#tasksinplace", width=1200)["tp"]
        self.assertEqual(sorted(tp["titles"]), sorted(["Catat perbaikan warna beda dari foto", "Balas 1 ulasan buruk"]))
        self.assertTrue(tp["sub"].startswith("Baju Oblong Wanita POLOS Lengan Panjang…"))  # a long marketplace title is shortened
        self.assertEqual(tp["fight"], {"view": "fixes", "draft": True, "buttons": ["Salin balasan", "Sudah dibalas", "Hapus dari Tugas"]})
        self.assertEqual(tp["prefilled"], "Ganti foto varian.")
        self.assertEqual(tp["fix"], ["ganti foto varian", True, True, "fixes"])
        self.assertEqual(tp["noData"], [False, True])
        self.assertEqual(tp["left"], [[], "fixes", True])
        self.assertEqual(tp["fresh"], ["input", "", "", True, None])
        self.assertEqual(tp["short"], ["Kaos", "Baju Oblong Wanita POLOS Lengan Panjang…"])
        self.assertEqual(tp["again"], ["", ""])

    def test_a_history_row_holds_counts_and_names_only(self):
        hist = self.probe("/#history")["hist"]
        self.assertEqual(hist["keys"], hist["cols"])
        for word in ("rahasia", "ijo", "koyak", "saran khusus", "balasan khusus"):  # a result full of quotes, a suggestion and a draft: none of it is in the row
            self.assertNotIn(word, hist["text"])
        self.assertEqual(hist["theme"], "Colour vs photo")
        self.assertEqual(hist["product"], 150)
        self.assertEqual(hist["counts"], [3, 1, 1, 0, 1])
        self.assertIsNone(hist["oddTheme"])  # only the fixed theme names
        self.assertIsNone(hist["noGroup"])
        self.assertEqual(hist["days"], 60)
        self.assertEqual(hist["hidden"], [True, True])  # no account here: no Riwayat anywhere
        self.assertTrue(hist["tasksLink"])
        self.assertTrue(hist["noScreen"])


    def test_usage_events_and_the_feedback_question(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = self.probe("/#events")
        ev = out["ev"]
        self.assertEqual((ev["askShown"], ev["textareaBeforeAnswer"], ev["refused"], ev["thanks"]), (True, False, True, True))
        logged = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
        names = [e["event"] for e in logged]
        for name in ("opened", "analysed", "tab_viewed", "draft_copied", "feedback"):
            self.assertIn(name, names)
        self.assertEqual(len({e["device"] for e in logged}), 1)  # one browser, one random id
        by = {e["event"]: e for e in logged}
        self.assertEqual((by["analysed"]["source"], by["analysed"]["lang"], by["tab_viewed"]["tab"], by["draft_copied"]["kind"]), ("paste", "id", "fight", "chat"))
        self.assertEqual([e["comment"] for e in logged if e["event"] == "feedback"], ["cukup berguna"])  # the refused comment never left the page
        everything = buf.getvalue()
        for word in ("tulisannya", "Kaos Boxy Retro", "bahannya"):  # no review text and no product name anywhere in the log
            self.assertNotIn(word, everything)

    def test_competitors_are_not_in_the_bottom_bar_and_the_free_example_is_labelled(self):
        pr = self.probe("/#sections")
        self.assertNotIn("err", pr, pr.get("err"))
        sec = pr["sec"]
        self.assertTrue(sec["openVisible"])  # the second button at the end of Hasil Bedah
        self.assertEqual(sec["gone"], [True, True, True])  # no Pesaing tab, no section switch, no side column
        self.assertEqual(sec["bottom"], ["nav-check", "nav-tasks"])
        self.assertEqual(sec["view"], ["rival", True])
        self.assertEqual(sec["nameFilled"], "Kaos Boxy Retro")  # pasted reviews have no link: the form asks for it, the name is filled in
        self.assertEqual(sec["back"], "Hasil Bedah")  # the back link names where it returns to
        self.assertEqual(sec["example"]["head"], "Hasil Bedah pesaing")
        self.assertEqual(sec["example"]["tabs"], ["Perlu dikejar3", "Keunggulanmu1"])
        self.assertTrue(sec["example"]["sum"] and sec["example"]["notice"] and sec["example"]["doneHidden"])  # the made-up example never adds tasks
        self.assertEqual(sec["cards"], 3)
        self.assertEqual(sec["returned"], ["results", True])
        self.assertEqual(sec["groups"], [3, 1, ["Praise: quality / material"]])
        self.assertEqual(sec["chosen"], [1, 2, 3])  # pasted links count towards the same number


if __name__ == "__main__":
    unittest.main()
