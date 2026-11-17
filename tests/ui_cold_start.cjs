/* Actual entry scripts, isolated providers: critical path and lazy editor. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,clean}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{for(const engine of [chromium,webkit]){const browser=await engine.launch();try{
 const page=await browser.newPage(),reads=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const u=new URL(route.request().url());assert.equal(u.origin,'https://example.test');reads.push(u.pathname);
  if(u.pathname==='/api/web/me')return route.fulfill({status:401,json:{detail:'Требуется вход'}});
  if(u.pathname==='/api/web/auth/otp/request')return route.fulfill({json:{token:'isolated',destination:'test@example.test',resend_after:60}});
  const file=path.join(process.cwd(),'app/web/static',u.pathname==='/'?'index.html':u.pathname.slice(8));
  assert.ok(fs.existsSync(file),u.pathname);const mime={'.js':'text/javascript','.html':'text/html','.css':'text/css','.svg':'image/svg+xml','.webp':'image/webp','.png':'image/png'};
  return route.fulfill({body:fs.readFileSync(file),contentType:mime[path.extname(file)]});
 });
 await page.goto('https://example.test');await page.locator('[name=identity]').fill('test@example.test');
 assert.equal(reads.filter(x=>x==='/api/web/me').length,1);assert.ok(!reads.includes('/assets/app.js'));assert.ok(!reads.includes('/assets/quill.js'));assert.ok(!reads.includes('/assets/clients.js'));assert.ok(!reads.includes('/api/web/config'));
 await page.locator('[data-unified-auth] [type=submit]').click();await page.locator('[name=code]').waitFor();assert.deepEqual(errors,[]);await page.close();
 const f=await fixture(browser,{admin:true});
 try{await f.page.route('**/quill.js?*',route=>route.abort('failed'));await f.page.evaluate(()=>pubNew());
  assert.equal(await f.page.evaluate(()=>Pub.draft),null);assert.equal(await f.page.evaluate(()=>typeof Quill),'undefined');
  await f.page.unroute('**/quill.js?*');
  assert.equal(f.model.reads.filter(x=>x.key==='GET /api/web/me').length,1);assert.equal(await f.page.evaluate(()=>typeof Quill),'undefined');
  // Two concurrent callers share one editor download and preserve button embeds.
  await f.page.evaluate(async()=>{await Promise.all([FreemanCabinetBoot.loadEditor(),FreemanCabinetBoot.loadEditor()]);pubRegisterButton()});
  assert.equal(await f.page.locator('script[src*="/quill.js"]').count(),1);
  assert.equal(await f.page.evaluate(()=>Quill.import('formats/button').blotName),'button');
  await f.page.evaluate(async()=>{state.view='admin-posts';await pubNew()});await f.page.locator('.ql-editor').waitFor();
  await f.page.locator('.ql-editor').fill('Редактор загружен по запросу');assert.equal(await f.page.locator('script[src*="/quill.js"]').count(),1);clean(f.model);
 }finally{await f.context.close();}
 console.log('PASS '+engine.name()+': anonymous OTP without cabinet, one profile request, single lazy editor with embeds');
 }finally{await browser.close()}}})().catch(e=>{console.error(e);process.exitCode=1});
