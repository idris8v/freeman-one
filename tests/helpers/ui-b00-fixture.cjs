/* B00 only: real RC entry points; isolated synthetic HTTP boundary. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const ROOT = path.resolve(__dirname, '../../app/web/static');
const ORIGIN = 'https://example.test';
const NOW = '2026-10-15T09:00:00Z';
const tariffs = [
  {id: 11, name: '1 месяц', months: 1, device_limit: 1, price: 10000},
  {id: 12, name: '3 месяца', months: 3, device_limit: 1, price: 25000},
  {id: 13, name: '1 месяц · семья', months: 1, device_limit: 3, price: 19000},
];
const devices = [
  {id: 1, name: 'iPhone', type: 'phone', status: 'active', registered_at: NOW, can_unlink: true},
  {id: 2, name: 'MacBook', type: 'laptop', status: 'active', registered_at: NOW, can_unlink: true},
];
function account(admin) {
  return {
    profile: {is_admin: admin, name: admin ? 'Admin' : 'Клиент', freeman_id: admin ? 'FO-1' : 'FO-47',
      email: 'fixture@example.test', email_verified: true, telegram_id: null,
      registration_source: 'website_email', password_login_available: true},
    balance: 90000, summary: {keys: 2, keys_active: 1},
    keys: [{id: 1, subscription_id: 11, name: 'Мой телефон', status: 'active', duration_months: 1,
      expires_at: '2026-11-15T09:00:00Z', device_limit: 3, devices,
      connection_loaded: true, display_key: 'https://sub.example.test/fixture', auto_renew: false},
    {id: 2, subscription_id: 12, name: 'Рабочий ноутбук · длинное название', status: 'expired',
      duration_months: 3, expires_at: '2026-10-10T09:00:00Z', device_limit: 1,
      connection_loaded: true, display_key: 'https://sub.example.test/expired', devices: []}],
    tariffs: structuredClone(tariffs), payments: {owner_free: false, yookassa: true},
    trial: {available: false}, useful_links: [], news: [], notifications: [],
  };
}
function catalogue() {
  return {can_edit: true, vpn: tariffs.map(t => ({...t, devices: t.device_limit, traffic_gb: 0,
    enabled: true, archived: false, version: 1})), proxy: [], promotions: [], codes: [], history: []};
}
const clients = [
  {id: 17, name: 'Ахмед', freeman_id: 'FO-17', created_at: '2026-10-07T12:00:00Z',
    avatar: '/assets/freeman-logo.svg', active_keys: 1, active_proxy: 1, status: 'active',
    contacts: [{channel: 'telegram', label: '@fixture'}]},
  {id: 18, name: 'Очень длинное имя клиента для проверки', freeman_id: 'FO-18',
    created_at: '2026-10-08T12:00:00Z', avatar: '/assets/b00-missing-avatar.png',
    active_keys: 0, active_proxy: 0, status: 'inactive', contacts: []},
];
async function fixture(browser, {width = 390, theme = 'light', admin = false, entry = '/', motion = 'reduce', recordVideo} = {}) {
  const context = await browser.newContext({viewport: {width, height: 900}, locale: 'ru-RU',
    timezoneId: 'Europe/Moscow', reducedMotion: motion, serviceWorkers: 'block', recordVideo,
    isMobile: width < 768, hasTouch: width < 768});
  await context.addInitScript(theme => localStorage.setItem('freeman-theme', theme), theme);
  const model = {account: account(admin), catalog: catalogue(), writes: [], reads: [], unknown: [],
    errors: [], consoleErrors: [], blocked: [], gates: {}, failure: {}, otp: 0,
    conversation: {id: 1, channel: 'owner', mode: 'ai', context_mode: 'service', messages: []}};
  const ticket = {id: 7, status: 'waiting_admin', channel: 'site', subject: 'Подключение',
    client: {name: 'Клиент', freeman_id: 'FO-47'}, updated_at: NOW, created_at: NOW,
    last_message: 'Нужна помощь', messages: [{id: 1, role: 'client', text: 'Нужна помощь', created_at: NOW, attachments: []}]};
  const json = (route, data, status = 200) => route.fulfill({status, json: data});
  await context.route('**/*', async route => {
    const req = route.request(), u = new URL(req.url()), key = req.method() + ' ' + u.pathname;
    if (u.origin !== ORIGIN) {model.blocked.push(u.origin); return route.abort();}
    if (u.pathname.startsWith('/api/')) {
      const write = !['GET', 'HEAD'].includes(req.method());
      let body = null;
      if (write && req.postData()) body = req.postDataJSON();
      const queryOnly = ['/api/web/admin/users/search', '/api/web/admin/proxy/control/list'].includes(u.pathname);
      (write && !queryOnly ? model.writes : model.reads).push({key, body, query: u.search});
      if (model.gates[key]) await model.gates[key];
      if (model.failure[key]) {
        const failure = model.failure[key]; delete model.failure[key];
        return failure === 'network' ? route.abort('failed') : json(route, {detail: 'Fixture: попробуйте снова'}, failure);
      }
      if (u.pathname === '/api/web/me') return model.account ? json(route, model.account) : json(route, {detail: 'Требуется вход'}, 401);
      if (u.pathname === '/api/web/config') return json(route, {});
      if (u.pathname === '/api/account/management') return json(route, {is_admin:admin,master:false,payment_methods:[],subscriptions:[{id:11,name:'Мой телефон',expires_at:'2026-11-15',devices:1,source:'balance',tariff_id:11}],tariffs:tariffs.map(t=>({...t,devices:t.device_limit})),attempts:[],trial_available:false});
      if (u.pathname === '/api/admin/renewal-status') return json(route, {items:[],reminders:[],trial_used:0});
      if (u.pathname === '/api/web/admin/ai') return json(route, {items:[],total:0,page:1,stats:{total:0,ai:0,human:0,closed:0,failures:0,total_tokens:0},configured:false,enabled:false,owner_access:admin});
      if (u.pathname === '/api/web/admin/ai/knowledge') return json(route, {items:[{id:1,title:'Подключение',text:'Откройте приложение.',version:1,enabled:false}]});
      if (u.pathname === '/api/web/admin/ai/events') return json(route, {items:[]});
      if (u.pathname === '/api/account/notifications') return json(route, {items: []});
      if (u.pathname === '/api/web/offers') return json(route, {offers: [], notifications: []});
      if (u.pathname === '/api/web/offers/quote') return json(route, {amount: tariffs.find(t => t.id === Number(u.searchParams.get('tariff_id')))?.price, discount: 0});
      if (key === 'POST /api/web/auth/otp/request') {model.otp++; return json(route, {step: 'otp', token: 'fixture-otp', destination: 'fixture@example.test', resend_after: 1});}
      if (key === 'POST /api/web/auth/otp/verify') {model.account = account(false); return json(route, {ok: true});}
      if (u.pathname === '/api/buy/identity') return json(route, {recognized: false});
      if (u.pathname === '/api/buy/catalog') return json(route, {available: true, bot_url: 'https://t.me/FixtureBot', tariffs: tariffs.map(t => ({id: t.id, months: t.months, devices: t.device_limit, amount: t.price}))});
      if (u.pathname === '/api/web/keys/1/connection') return json(route, {value: 'https://sub.example.test/fixture'});
      if (u.pathname === '/api/web/keys/1/devices') return json(route, {key: model.account.keys[0], devices, device_limit: 3, device_sync: {status: 'confirmed', count: 2}});
      if (key === 'PUT /api/web/keys/1/name') {model.account.keys[0].name = body.name; return json(route, {id: 1, name: body.name});}
      if (key === 'POST /api/web/checkout') return json(route, {detail: 'Fixture: платёж не выполняется'}, 503);
      if (u.pathname === '/api/web/support') return json(route, {is_admin: admin, tickets: [ticket],
        selected: u.searchParams.has('ticket_id') ? ticket : null, total: 1, page: 1, limit: 10,
        counts: {waiting_admin: 1}, quick_replies: [], autoreply: {enabled: false, text: ''}});
      if (key === 'POST /api/web/support/messages') {
        ticket.messages.push({id: 2, role: 'client', text: body.text, created_at: NOW, attachments: []});
        return json(route, {id: 7});
      }
      if (u.pathname === '/api/web/admin/users/search' || u.pathname === '/api/web/admin/users') {
        if (!model.account?.profile.is_admin) return json(route, {detail: 'Нет доступа'}, 403);
        return json(route, {items: clients, total: 2, page: 1, limit: 10});
      }
      if (u.pathname === '/api/web/admin/catalog') {
        if (!model.account?.profile.is_admin) return json(route, {detail: 'Нет доступа'}, 403);
        if (write) {const t = model.catalog.vpn.find(t => t.id === body.id); Object.assign(t, body, {price: Math.round(Number(body.price) * 100)});}
        return json(route, model.catalog);
      }
      if (u.pathname === '/api/web/admin/dashboard') return json(route, {revenue: 90000, sales: 1, active_keys: 1, users: 2, daily: []});
      if (u.pathname === '/api/web/admin/reports') return json(route, {stats: {revenue: 90000}, sales: [], daily: []});
      if (u.pathname === '/api/web/admin/proxy/control/list') return json(route, {items: [], servers: [], total: 0, summary: {}, recent: [], series: []});
      if (u.pathname === '/api/web/admin/proxy/control/settings') return json(route, {});
      if (u.pathname === '/api/web/admin/assistant') return json(route, model.conversation);
      if (u.pathname.endsWith('/chats')) return json(route, {items: [{id: 1, title: 'Проверка интерфейса', updated_at: NOW}]});
      if (u.pathname === '/api/web/admin/assistant/chats/1') return json(route, model.conversation);
      if (key === 'POST /api/web/admin/assistant/chats/1/messages') {
        model.conversation.messages.push({id: 1, role: 'client', text: body.text, created_at: NOW},
          {id: 2, role: 'assistant', text: 'Ответ из изолированного fixture.', created_at: NOW, actions: []});
        return json(route, {ok: true});
      }
      model.unknown.push(key); return json(route, {detail: 'Unmapped B00 fixture: ' + key}, 501);
    }
    const document = u.pathname === '/admin/assistant' ? 'freeman-assistant.html' : u.pathname === '/admin/ai' ? 'freeman-ai.html' : u.pathname === '/account' ? 'account-management.html' : u.pathname.startsWith('/buy') ? 'guest-purchase.html' : u.pathname === '/' ? 'index.html' : null;
    const name = document || (u.pathname.startsWith('/assets/') ? path.basename(u.pathname) : null);
    const file = name && path.join(ROOT, name);
    if (!file || !fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    const mime = {'.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.html': 'text/html'};
    return route.fulfill({body: fs.readFileSync(file), contentType: mime[path.extname(file)] || 'application/octet-stream'});
  });
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date(NOW));
  page.on('pageerror', e => model.errors.push(e.message));
  page.on('console', e => {if (e.type() === 'error') model.consoleErrors.push(e.text());});
  page.setDefaultTimeout(10000);
  await page.goto(ORIGIN + entry);
  if (entry === '/') await page.waitForFunction(() => typeof state !== 'undefined' && !!state.data);
  model.runtime = await page.evaluate(() => ({
    scripts: [...document.scripts].filter(s => s.src).map(s => ({src: new URL(s.src).pathname, defer: s.defer, async: s.async})),
    // Read resolved bindings after all deferred scripts, not an earlier app.js declaration.
    bindings: Object.fromEntries(['viewContent', 'profilePage', 'adminProfilePage', 'adminClientsPage',
      'adminCustomerPage', 'adminKeysPage', 'adminKeyPage', 'keysDevices', 'purchasePage',
      'adminProxyDashboardPage', 'catalogPage', 'plansPage', 'keysPage', 'render', 'loadView']
      .map(name => {try {const fn = (0, eval)(name); return [name, String(fn).slice(0, 600)];} catch {return [name, null];}})),
  }));
  const pathname=new URL(entry,ORIGIN).pathname;
  const entryName = pathname === '/admin/assistant' ? 'freeman-assistant' : pathname === '/admin/ai' ? 'freeman-ai' : pathname === '/account' ? 'account-management' : pathname.startsWith('/buy') ? 'guest-purchase' : 'index';
  model.runtime.entry = entryName;
  const contract = require('../fixtures/ui-b00-contracts.json').entries[entryName];
  assert.deepEqual(model.runtime.scripts, contract.scripts_in_document_order, 'Entry script/attribute contract changed');
  return {page, context, model};
}
async function navigate(page, view, selector) {
  // Prefer existing visible navigation. Some admin entries are nested in Profile.
  const link = page.locator(`[data-view="${view}"]:visible`).first();
  if (await link.count()) await link.click();
  else await page.evaluate(view => loadView(view), view);
  await page.locator(`.screen:not([hidden]) ${selector}`).first().waitFor();
}
async function capture(page, directory, name) {
  fs.mkdirSync(directory, {recursive: true});
  await page.waitForFunction(() => {
    const intro = document.querySelector('.brand-intro');
    return !intro || getComputedStyle(intro).display === 'none' || getComputedStyle(intro).visibility === 'hidden';
  });
  await page.evaluate(() => document.fonts.ready);
  // Finish only finite transitions; keep production CSS and renderer untouched.
  await page.waitForTimeout(350);
  const data = await page.evaluate(() => {
    const properties = ['display', 'position', 'width', 'height', 'padding', 'margin', 'borderRadius',
      'borderWidth', 'borderColor', 'color', 'backgroundColor', 'backgroundImage', 'boxShadow',
      'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'gap', 'alignItems', 'justifyContent',
      'overflowX', 'overflowY', 'appearance', 'outline', 'transitionDuration', 'transitionTimingFunction',
      'animationName', 'animationDuration', 'backdropFilter'];
    const selector = 'button,input,select,textarea,a,summary,[role=dialog],.purchase-card,.my-key-card,.crm-row,.catalog-row,.mobile-nav,.sidebar';
    const items = [...document.querySelectorAll(selector)].filter(n => {
      const b = n.getBoundingClientRect(), s = getComputedStyle(n);
      return b.width && b.height && s.visibility !== 'hidden' && !n.closest('[hidden],[inert]');
    }).map(n => {
      const b = n.getBoundingClientRect(), s = getComputedStyle(n);
      return {tag: n.tagName, class: n.className, text: (n.textContent || '').trim().slice(0, 100),
        attributes: Object.fromEntries([...n.attributes].filter(a => /^(data-|aria-|name$|type$)/.test(a.name)).map(a => [a.name, a.value])),
        bounds: [b.x, b.y, b.width, b.height].map(v => Math.round(v * 100) / 100),
        style: Object.fromEntries(properties.map(p => [p, s[p]]))};
    });
    return {url: location.pathname + location.search, width: innerWidth, height: innerHeight,
      theme: document.documentElement.dataset.theme, role: typeof state !== 'undefined' ? state.data?.profile?.is_admin ? 'admin' : 'customer' : 'standalone',
      pageOverflow: document.documentElement.scrollWidth > innerWidth + 1, items};
  });
  assert.ok(data.items.length, 'Computed styles are empty: ' + name);
  fs.writeFileSync(path.join(directory, name + '.json'), JSON.stringify(data, null, 2) + '\n');
  await page.screenshot({path: path.join(directory, name + '.png'), fullPage: true, animations: 'disabled'});
  return {name, overflow: data.pageOverflow, controls: data.items.length};
}
function clean(model) {
  assert.deepEqual(model.errors, [], 'Uncaught browser errors');
  assert.deepEqual(model.unknown, [], 'Unmapped fixture requests');
}
module.exports = {fixture, navigate, capture, clean, NOW};
