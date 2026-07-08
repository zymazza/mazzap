// Screenshot harness for the VEIL viewer (needs playwright: npm i -D playwright).
// Usage: node scripts/screenshot.js <outfile.png> [layerToggleId] [waitMs]
// Target: SHOT_URL env, else http://127.0.0.1:$PORT (default 4173).
const { chromium } = require('playwright');

const GL = ['--use-gl=angle','--use-angle=gl-egl','--no-sandbox','--disable-gpu-sandbox',
  '--in-process-gpu','--disable-dev-shm-usage','--ignore-gpu-blocklist'];

(async () => {
  const out = process.argv[2] || '/tmp/twin.png';
  const toggleOnly = process.argv[3] || null;   // layer id to leave on (others off)
  const extraWait = Number(process.argv[4] || 0);
  const url = process.env.SHOT_URL
    || `http://127.0.0.1:${process.env.PORT || 4173}/`;
  const browser = await chromium.launch({ args: GL });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errs = [];
  page.on('pageerror', e => errs.push('PAGEERR ' + e.message));
  page.on('console', m => { if (m.type()==='error') errs.push('CONSOLE ' + m.text()); });
  await page.goto(url, { waitUntil: 'load' });
  try { await page.waitForSelector('#loading.hidden', { state: 'attached', timeout: 60000 }); }
  catch { console.log('WARN: scene did not finish loading'); }
  await page.waitForTimeout(1500);
  await page.evaluate((astroTime) => {
    const api = window.__twin?.astronomy;
    if (api?.clock?.setManual) {
      api.clock.setManual(Date.parse(astroTime), { rate: 0, playing: false });
    }
  }, process.env.ASTRO_TIME || '2026-06-21T16:00:00Z');
  await page.waitForTimeout(100);

  if (toggleOnly) {
    // comma list of labels to keep on; everything else (scene + atlas + survey) goes off
    await page.evaluate((keepCsv) => {
      const keeps = keepCsv.toLowerCase().split(',').map((s) => s.trim()).filter(Boolean);
      document.querySelectorAll('#layer-toggles .toggle-row, #atlas-toggles .toggle-row, #survey-toggles .toggle-row, #astro-toggles .toggle-row')
        .forEach((row) => {
          const cb = row.querySelector('input');
          const label = row.textContent.trim().toLowerCase();
          const want = keeps.some((k) => label.includes(k));
          if (cb.checked !== want) { cb.checked = want; cb.dispatchEvent(new Event('change')); }
        });
    }, toggleOnly);
    await page.waitForTimeout(1200 + extraWait);
  } else if (extraWait) {
    await page.waitForTimeout(extraWait);
  }

  if (process.argv[5] === 'click') {
    const box = await page.locator('#viewer-root canvas').boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForTimeout(600);
    const info = await page.evaluate(() => ({
      coords: document.getElementById('r-latlon')?.textContent,
      identify: document.getElementById('identify-results')?.innerText?.slice(0, 600),
    }));
    console.log('CLICK INFO:', JSON.stringify(info, null, 2));
  }

  await page.screenshot({ path: out });
  console.log('shot ->', out);
  if (errs.length) console.log('ERRORS:', errs.slice(0,8));
  await browser.close();
})();
// (click point override via CLICK_X/CLICK_Y env: fractions of canvas size)
