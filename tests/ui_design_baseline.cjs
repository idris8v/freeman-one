/* B00 migration guard. Real UI/scripts, synthetic APIs; not provider E2E. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {chromium, webkit} = require(process.env.FREEMAN_PLAYWRIGHT_MODULE || 'playwright');
const {fixture, navigate, capture, clean, NOW} = require('./helpers/ui-b00-fixture.cjs');
const OUT = path.resolve(process.env.FREEMAN_B00_OUTPUT || 'artifacts/ui-b00');
const results = [], cases = [];
async function shot(f, prefix, state) {
  fs.mkdirSync(OUT, {recursive: true});
  fs.writeFileSync(path.join(OUT, prefix + '.' + f.model.runtime.entry + '.runtime.json'), JSON.stringify(f.model.runtime, null, 2) + '\n');
  results.push(await capture(f.page, OUT, prefix + '-' + state));
}
async function keys(browser, options, prefix) {
  const f = await fixture(browser, options), {page, model} = f;
  try {
    await navigate(page, 'keys', '.my-keys-page');
    assert.equal(await page.locator('[data-ai-open]:visible,a[href^="/admin/assistant"]:visible').count(), 0);
    const toggle = page.locator('[data-key-toggle="1"]');
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    await shot(f, prefix, 'collapsed');
    await toggle.click(); assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    await page.locator('[data-key-rename="1"]').focus();
    await shot(f, prefix, 'expanded-focus');
    await page.locator('[data-key-devices="1"]').click();
    const dialog = page.getByRole('dialog', {name: 'Устройства подписки'});
    await dialog.waitFor(); assert.equal(await dialog.locator('.key-device-detail').count(), 2);
    await shot(f, prefix, 'devices');
    await dialog.locator('button[data-close-modal]').first().click();
    await page.locator('[data-key-rename="1"]').click();
    await page.locator('#rename-key-input').fill('   ');
    await page.locator('[data-rename-key] [type=submit]').click();
    await page.locator('.rename-key-error').waitFor({state: 'visible'});
    assert.equal(model.writes.length, 0, 'Invalid rename must not write');
    await shot(f, prefix, 'rename-invalid');
    await page.locator('#rename-key-input').fill('Телефон для поездок');
    await page.locator('[data-rename-key] [type=submit]').click();
    await page.waitForFunction(() => !state.modal);
    assert.equal(model.account.keys[0].name, 'Телефон для поездок');
    assert.equal(model.writes.filter(x => x.key === 'PUT /api/web/keys/1/name').length, 1);
    await page.reload(); await page.waitForFunction(() => typeof state !== 'undefined' && !!state.data);
    await navigate(page, 'keys', '.my-keys-page');
    assert.match(await page.locator('.my-key-card').filter({has: page.locator('[data-key-toggle="1"]')}).innerText(), /Телефон для поездок/);
    clean(model); cases.push(prefix);
  } finally {await f.context.close();}
}
async function plans(browser, options, prefix) {
  const f = await fixture(browser, options), {page, model} = f;
  try {
    await navigate(page, 'plans', '.device-options'); await shot(f, prefix, 'initial');
    await page.locator('[data-device-limit="3"]').click();
    assert.equal(await page.locator('[data-plan="13"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-payment-method="balance"]').click();
    await shot(f, prefix, 'selected');
    await page.locator('[data-checkout]').click();
    const dialog = page.getByRole('dialog', {name: 'Подтвердить оплату?'}); await dialog.waitFor();
    assert.equal(await page.locator('[data-confirm-checkout]').evaluate(n => n === document.activeElement), true);
    await page.locator('[data-confirm-checkout]').press('Tab');
    assert.equal(await dialog.locator('button[data-checkout-edit]').evaluate(n => n === document.activeElement), true);
    await shot(f, prefix, 'confirmation');
    assert.equal(model.writes.length, 0, 'Opening confirmation cannot charge');
    await page.keyboard.press('Escape'); await dialog.waitFor({state: 'hidden'});
    assert.equal(await page.locator('[data-checkout]').evaluate(n => n === document.activeElement), true);
    await page.locator('[data-view="keys"]:visible').first().click();
    await page.locator('.my-keys-page').waitFor();
    assert.equal(await page.evaluate(() => state.view), 'keys');
    // Preserve the native unavailable-card control, without changing UI code.
    model.account.payments.yookassa = false;
    await page.reload(); await page.waitForFunction(() => typeof state !== 'undefined' && !!state.data);
    await navigate(page, 'plans', '.device-options');
    assert.equal(await page.locator('[data-payment-method="yookassa"]').isDisabled(), true);
    await shot(f, prefix, 'disabled-card');
    clean(model); cases.push(prefix);
  } finally {await f.context.close();}
}
async function clients(browser, options, prefix) {
  const f = await fixture(browser, {...options, admin: true}), {page, model} = f;
  try {
    await navigate(page, 'admin-clients', '.crm-compact');
    assert.equal(await page.locator('.screen:not([hidden]) .crm-row').count(), 2);
    await page.waitForFunction(() => !document.querySelector('[data-client-row="18"] .admin-list-avatar'));
    await shot(f, prefix, 'list');
    await page.locator('[data-crm-select="17"]').check();
    assert.equal(await page.locator('[data-crm-select="17"]').isChecked(), true);
    await page.locator('[data-crm-menu="17"]').click();
    await page.locator('#' + await page.locator('[data-crm-menu="17"]').getAttribute('aria-controls')).waitFor();
    await shot(f, prefix, 'selected-menu');
    await page.locator('[data-crm-menu="17"]').click();
    await page.locator('[data-crm-filter-toggle]').click();
    if (options.width < 1100) {
      await page.getByRole('dialog', {name: 'Фильтр', exact: true}).waitFor();
      await page.locator('.crm-filter-group').filter({has: page.locator('input[name=status]')}).locator('summary').click();
    } else await page.locator('[data-crm-filter="status"]').waitFor();
    await shot(f, prefix, 'filters');
    const response = page.waitForResponse(r => r.url().includes('/admin/users') && r.status() === 200);
    if (options.width < 1100) {
      await page.locator('input[name=status][value=active]').check();
      await page.locator('[data-crm-sheet-apply]').click();
    } else await page.locator('[data-crm-filter="status"]').selectOption('active');
    await response;
    await page.waitForFunction(() => crmFilters().status === 'active');
    assert.ok(model.reads.concat(model.writes).some(r => r.key.includes('/admin/users')));
    assert.equal(await page.evaluate(() => crm.selected.size), 0, 'Filter change clears selection');
    clean(model); cases.push(prefix);
  } finally {await f.context.close();}
}
async function catalog(browser, options, prefix) {
  const f = await fixture(browser, {...options, admin: true}), {page, model} = f;
  try {
    await navigate(page, 'admin-catalog', '.catalog-page');
    await page.locator('[data-catalog-edit="vpn:11"]').waitFor();
    await shot(f, prefix, 'list');
    await page.locator('[data-catalog-edit="vpn:11"]').click();
    const form = page.locator('[data-catalog-form]'); await form.waitFor();
    assert.equal(await form.locator('[name=months]').getAttribute('readonly'), '');
    await form.locator('[name=price]').fill('0');
    await form.locator('[name=confirmed]').check(); await form.locator('[type=submit]').click();
    assert.equal(model.writes.length, 0, 'Native validation rejects invalid price');
    await shot(f, prefix, 'editor-invalid');
    await form.locator('[name=price]').fill('123.45');
    const key = 'POST /api/web/admin/catalog';
    let release; model.gates[key] = new Promise(resolve => {release = resolve;}); model.failure[key] = 503;
    await form.locator('[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('[data-catalog-form] [type=submit]')?.disabled);
    await shot(f, prefix, 'editor-busy'); release(); delete model.gates[key];
    await page.locator('[data-catalog-error]').filter({hasText: 'Fixture'}).waitFor();
    assert.equal(await form.locator('[name=price]').inputValue(), '123.45');
    await shot(f, prefix, 'editor-error');
    await form.locator('[type=submit]').click();
    await page.waitForFunction(() => !state.modal && Catalog.data?.vpn?.[0]?.price === 12345);
    const writes = model.writes.filter(x => x.key === key);
    assert.equal(writes.length, 2); assert.equal(writes[0].body.request_id, writes[1].body.request_id);
    assert.equal(writes[1].body.id, 11); assert.equal(writes[1].body.devices, 1);
    await page.reload(); await page.waitForFunction(() => typeof state !== 'undefined' && !!state.data);
    await navigate(page, 'admin-catalog', '.catalog-page');
    await page.waitForFunction(() => Catalog.data?.vpn?.[0]?.price === 12345);
    model.catalog.can_edit = false;
    await page.evaluate(() => loadCatalog());
    assert.equal(await page.locator('[data-catalog-new],[data-catalog-edit]').count(), 0);
    clean(model); cases.push(prefix);
  } finally {await f.context.close();}
}
async function smokes(browser, options, prefix) {
  // Entry controllers/renderers remain real; API delivery/authentication is simulated.
  const f = await fixture(browser, options), {page, model} = f;
  try {
    await page.evaluate(() => FreemanAuth.show());
    await page.locator('[name=identity]').fill('fixture@example.test');
    await page.locator('[data-unified-auth] [type=submit]').click();
    await page.locator('[name=code]').waitFor();
    assert.equal(await page.locator('[name=code]').getAttribute('autocomplete'), 'one-time-code');
    assert.equal(await page.locator('[data-unified-resend]').isDisabled(), true);
    await shot(f, prefix, 'otp-wait');
    await page.clock.setFixedTime(new Date(new Date(NOW).getTime() + 2000));
    await page.waitForFunction(() => !document.querySelector('[data-unified-resend]').disabled);
    await shot(f, prefix, 'otp-resend');
    const resendResponse=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/web/auth/otp/request'&&r.request().method()==='POST');
    await page.locator('[data-unified-resend]').click();await resendResponse;
    assert.equal(model.otp, 2);
    await page.locator('[data-unified-back]').click();
    assert.equal(await page.locator('[name=identity]').inputValue(), 'fixture@example.test');
    await page.locator('[data-unified-auth] [type=submit]').click();
    await page.locator('[name=code]').fill('123456');
    await page.locator('[data-unified-auth] [type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('[data-unified-auth]') && !!document.querySelector('.app-shell'));
    assert.equal(model.writes.filter(x => x.key === 'POST /api/web/auth/otp/verify').length, 1);
    await navigate(page, 'support', '.native-inbox');
    await page.locator('[data-support-ticket="7"]').click();
    const supportInput = page.locator('[data-support-message] textarea'); await supportInput.waitFor();
    await supportInput.fill('Помогите подключить'); await supportInput.press('Shift+Enter');
    await supportInput.dispatchEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true});
    assert.equal(model.writes.filter(x => x.key === 'POST /api/web/support/messages').length, 0);
    assert.match(await supportInput.inputValue(), /\n/);
    await supportInput.press('Enter');
    await page.waitForFunction(() => !state.busy && document.querySelector('[data-support-message] textarea')?.value === '');
    assert.equal(model.writes.filter(x => x.key === 'POST /api/web/support/messages').length, 1);
    await shot(f, prefix, 'support-sent');
    clean(model);
  } finally {await f.context.close();}
  const receipt = await fixture(browser, {...options, entry: '/buy'});
  try {
    await receipt.page.locator('#purchase').waitFor();
    const states = [
      ['pending', {status: 'order_pending'}], ['uncertain', {status: 'unknown'}],
      ['failed', {status: 'order_failed'}], ['provisioning', {status: 'order_paid', confirmed: true}],
      ['success', {status: 'order_paid', key: 'https://sub.example.test/fixture', login: 'FO-47', months: 1, devices: 3, expires_at: '2026-11-15T09:00:00Z'}],
    ];
    for (const [name, data] of states) {
      await receipt.page.evaluate(data => {document.querySelector('#content').innerHTML = PostPayment.render(data);}, {...data, started: new Date(NOW).getTime()});
      assert.equal(await receipt.page.locator('.pp-success').count(), name === 'success' ? 1 : 0);
      if (name !== 'success') assert.equal(await receipt.page.getByRole('link',{name:'Открыть оплату',exact:true}).count(),0,'Missing provider URL cannot become an /undefined payment link');
      if (name === 'success') assert.equal(await receipt.page.locator('.pp-qr').count(),0);
      if (name === 'provisioning') assert.match(await receipt.page.locator('.pp-wait').innerText(), /Покупку повторять не нужно/);
      await shot(receipt, prefix, 'receipt-' + name);
    }
    assert.equal(receipt.model.writes.length, 0);
    clean(receipt.model);
  } finally {await receipt.context.close();}
  const chat = await fixture(browser, {...options, admin: true, entry: '/admin/assistant'});
  try {
    // The form exists before the initial chat/history request completes. Wait
    // for the existing readiness signal so startup cannot clear the test draft.
    await chat.page.locator('#assistant-app:not([aria-busy])').waitFor();
    const input = chat.page.locator('textarea'); await input.waitFor();
    assert.equal(await chat.page.locator('[data-mic],[data-voice-send]').count(), 0);
    await input.fill('Первая строка'); await input.press('Shift+Enter');
    await input.dispatchEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true});
    assert.equal(chat.model.writes.filter(x => x.key.endsWith('/messages')).length, 0);
    assert.match(await input.inputValue(), /\n/);
    await input.press('Enter'); await chat.page.locator('.fa-message.assistant').waitFor();
    const writes = chat.model.writes.filter(x => x.key.endsWith('/messages'));
    assert.equal(writes.length, 1); assert.ok(writes[0].body.request_id);
    await shot(chat, prefix, 'chat-sent'); clean(chat.model);
  } finally {await chat.context.close();}
  cases.push(prefix + '-otp-receipt-chat');
}
(async () => {
  fs.mkdirSync(OUT, {recursive: true});
  for (const engine of [chromium, webkit]) {
    const browser = await engine.launch();
    try {
      for (const width of [390, 1440]) for (const theme of ['light', 'dark']) {
        const options = {width, theme}, prefix = `${engine.name()}-${width}-${theme}`;
        for (const [name, run] of Object.entries({keys, plans, clients, catalog, smokes})) {
          console.log('B00', prefix, name); await run(browser, options, prefix + '-' + name);
        }
      }
    } finally {await browser.close();}
  }
  const files = fs.readdirSync(OUT).filter(n => /\.(png|json)$/.test(n) && n !== 'manifest.json').sort();
  const manifest = {baseline: 'd5a803588695d66a8d7b6a85d8dd90f188125ae4', fixtureTime: NOW,
    coverage: 'Real HTML/CSS/JS with synthetic API responses; no backend/provider E2E or physical devices', cases, captures: results,
    files: files.map(name => ({name, sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(OUT, name))).digest('hex')}))};
  fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  const esc = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  fs.writeFileSync(path.join(OUT, 'index.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><title>B00 RC baseline</title><style>body{font:16px system-ui;margin:24px;background:#eee}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}img{width:100%;height:360px;object-fit:contain;background:white}a{color:#1456b8}article{padding:12px;background:white;border-radius:12px}</style><h1>B00 · RC baseline · synthetic APIs</h1><p>390 / 1440 · Chromium / WebKit · light / dark. Computed styles are linked beside each capture.</p><main>' + results.map(r => `<article><p>${esc(r.name)}${r.overflow ? ' · PAGE OVERFLOW' : ''}</p><a href="${r.name}.png"><img loading="lazy" src="${r.name}.png" alt="${esc(r.name)}"></a><p><a href="${r.name}.json">Computed styles</a></p></article>`).join('') + '</main></html>');
  console.log(`PASS B00: ${cases.length} browser scenarios; ${results.length} screenshot/style pairs; ${results.filter(r => r.overflow).length} overflow observations. ${OUT}`);
})().catch(e => {console.error(e); process.exit(1);});
