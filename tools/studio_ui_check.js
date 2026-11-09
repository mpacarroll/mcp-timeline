// Browser check for the history studio. Drives the real page in Chromium,
// fails on console errors, horizontal scroll at phone width, or a redacted
// name showing up in the share preview, and saves screenshots.
//
//   python3 tools/demo_studio.py /tmp/demo.db
//   TIMELINE_DB=/tmp/demo.db python3 studio_web.py 8011 &
//   node tools/studio_ui_check.js http://127.0.0.1:8011 screenshots/
//
// Uses the Playwright that ships with the environment; nothing to install
// in this repo, and no network access beyond the local studio.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const base = process.argv[2] || "http://127.0.0.1:8011";
const out = process.argv[3] || "screenshots";
fs.mkdirSync(out, { recursive: true });

function check(cond, msg) {
  if (!cond) throw new Error("check failed: " + msg);
  console.log("ok  " + msg);
}

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const watch = (page) => {
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(String(e)));
  };

  // Desktop walk-through.
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page);
  await page.goto(base);
  await page.waitForSelector(".item");
  check(await page.locator("#trip-title").textContent() === "Lisbon long weekend", "trip opens on load");
  check(await page.locator(".tag.redacted").count() >= 3, "redacted items are flagged in the editor");
  check(await page.locator("text=(+1d)").count() >= 2, "overnight items show the next-day marker");
  await page.screenshot({ path: path.join(out, "1-timeline.png"), fullPage: false });

  await page.locator(".item", { hasText: "Belem Tower" }).getByRole("button", { name: "Source" }).click();
  await page.waitForSelector("#inspector[open]");
  check((await page.locator("#inspector-body").textContent()).includes("Unknown"), "inspector shows the source's own label");
  await page.screenshot({ path: path.join(out, "2-inspector.png") });
  await page.click("#inspector-close");

  await page.click('button[data-tab="privacy"]');
  await page.waitForSelector("text=Active redactions");
  await page.screenshot({ path: path.join(out, "3-privacy.png") });

  await page.click('button[data-tab="share"]');
  await page.waitForSelector(".preview h4");
  const preview = await page.locator(".preview").textContent();
  check(preview.includes("Appointment"), "share preview shows the masked label");
  check(!/Lumen|Ines|rash/i.test(preview), "share preview leaks no redacted name");
  await page.selectOption('select[name="coordinates"]', "none");
  await page.waitForFunction(() => !document.querySelector(".preview").textContent.includes("38.7"));
  check(true, "turning coordinates off updates the preview");
  await page.selectOption('select[name="preset"]', "family");
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(out, "4-share.png") });

  await page.click('button[data-tab="agents"]');
  await page.fill('form.card input[placeholder="Claude Desktop"]', "UI check agent");
  await page.getByRole("button", { name: "Create grant" }).click();
  await page.waitForSelector(".token-box code");
  check((await page.locator(".token-box code").textContent()).startsWith("tlg_"), "grant token is shown once");
  await page.getByRole("button", { name: "I've stored it" }).click();
  check(await page.locator(".token-box").count() === 0, "token disappears once dismissed");
  await page.screenshot({ path: path.join(out, "5-agents.png") });

  await page.click('button[data-tab="story"]');
  await page.waitForSelector("textarea");
  await page.screenshot({ path: path.join(out, "6-story.png") });

  // Phone width: no horizontal scroll on any tab.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const mp = await phone.newPage();
  watch(mp);
  await mp.goto(base);
  await mp.waitForSelector(".item");
  for (const tab of ["timeline", "story", "privacy", "share", "agents", "danger"]) {
    await mp.click(`button[data-tab="${tab}"]`);
    await mp.waitForTimeout(250);
    const overflow = await mp.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(overflow <= 0, `no horizontal scroll at 390px on ${tab} (${overflow}px)`);
  }
  await mp.click('button[data-tab="timeline"]');
  await mp.screenshot({ path: path.join(out, "7-phone-timeline.png") });

  // Share page as a recipient sees it.
  const sharePage = await ctx.newPage();
  watch(sharePage);
  const shares = await page.evaluate(async () => {
    const t = document.querySelector('meta[name="studio-token"]').content;
    const trips = await (await fetch("/api/trips", { headers: { "X-Studio-Token": t } })).json();
    const trip = await (await fetch("/api/trips/" + trips.trips[0].trip_id, { headers: { "X-Studio-Token": t } })).json();
    return trip.shares;
  });
  await sharePage.goto(base + "/share/" + shares[0].share_id);
  const shareText = await sharePage.textContent("body");
  check(!/Lumen|Ines|rash/i.test(shareText), "family share page leaks no redacted name");
  await sharePage.screenshot({ path: path.join(out, "8-share-page.png"), fullPage: true });

  check(errors.length === 0, "no console errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
