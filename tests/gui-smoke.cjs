const {chromium} = require('playwright');
const {pathToFileURL} = require('node:url');
const {resolve} = require('node:path');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1150, height: 1000}});
    const errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
    await page.goto(pathToFileURL(resolve('asana_export_kit/web/index.html')).href);
    assert.match(await page.locator('#command').inputValue(), /export/);
    await page.locator('#schedule').check();
    assert.match(await page.locator('#command').inputValue(), /watch/);
    await page.locator('#stories').uncheck();
    assert.match(await page.locator('#command').inputValue(), /--omit/);
    await page.locator('#auth').selectOption('oauth');
    assert.match(await page.locator('#error').innerText(), /PAT/);
    assert.equal(await page.locator('#copy').isDisabled(), true);
    await page.locator('#auth').selectOption('prompt');
    await page.locator('#workspaces').fill('123,456');
    await page.locator('#remote').fill("drive:folder with 'quotes'");
    assert.equal(await page.locator('#error').textContent(), '');
    // The argument preview is inside a collapsed <details>; read its DOM text.
    const args = JSON.parse(await page.locator('#argv').textContent());
    const parsed = spawnSync('python', ['-c', 'import json,sys; from asana_export_kit.cli import parser; parser().parse_args(json.loads(sys.argv[1]))', JSON.stringify(args.slice(3))], {encoding:'utf8'});
    assert.equal(parsed.status, 0, parsed.stderr);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    assert.deepEqual(requests, []);
    console.log('GUI browser smoke passed: form controls, CLI round-trip, mobile width, no external requests.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
