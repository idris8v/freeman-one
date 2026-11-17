/* Running renderers, real assets, isolated HTTP data. Never reaches providers. */
const fs=require('fs'),assert=require('node:assert/strict');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,navigate,capture}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{for(const [engine,type]of Object.entries({chromium,webkit})){const browser=await type.launch();for(const width of [390,1440])for(const theme of ['light','dark']){
 const {page:p,context,model}=await fixture(browser,{admin:true,width,theme,motion:'no-preference'});let listCalls=[];
 await context.route('**/api/web/admin/users/search',r=>{const body=r.request().postDataJSON();listCalls.push(body);const total=55,page=Math.min(body.page,Math.ceil(total/body.limit));return r.fulfill({json:{items:Array.from({length:Math.min(body.limit,total-(page-1)*body.limit)},(_,i)=>({id:(page-1)*body.limit+i+1,name:'Клиент '+i,freeman_id:'FO-'+(i+1),active_keys:1,active_proxy:0,status:'active',contacts:[],created_at:'2026-10-01T00:00:00Z'})),total,page,limit:body.limit}})});
 await navigate(p,'admin-clients','.crm-toolbar');await p.locator('.screen:visible [data-admin-list-limit]').first().selectOption('25');await p.waitForFunction(()=>state.extra['admin-clientsList']?.limit===25);
 assert.equal(await p.locator('.screen:visible .crm-row').count(),25);assert.equal(listCalls.at(-1).limit,25);
 await p.locator('.screen:visible [aria-label="Следующая страница"]').first().click();await p.waitForFunction(()=>state.extra['admin-clientsList']?.page===2);
 await p.locator('.screen:visible [data-admin-list-limit]').first().selectOption('100');await p.waitForFunction(()=>state.extra['admin-clientsList']?.limit===100);assert.equal(await p.locator('.screen:visible .crm-row').count(),55);assert.equal(await p.evaluate(()=>state.extra['admin-clientsList'].page),1);
 assert.equal(await p.locator('body>footer,.fo-shell-footer').count(),0);
 await capture(p,'artifacts/ui-consistency',`${engine}-${width}-${theme}-clients`);
 const key={id:30,user_id:17,name:'Личный телефон',owner:'Клиент с длинным именем',freeman_id:'FO-17',device_count:1,device_limit:3,status:'active',mode:'vless',expires_at:'2026-11-15T00:00:00Z',duration_months:1,connection_url:'https://sub.example.test/'+('very-long-subscription-'.repeat(12)),traffic:'1 ГБ'};
 await context.route('**/api/web/admin/keys/search',r=>{const body=r.request().postDataJSON();return r.fulfill({json:{items:[key],total:1,page:1,limit:body.limit,device_limits:[1,3,5],tariffs:[],servers:[]}})});
 await context.route('**/api/web/admin/keys/30/export',r=>r.fulfill({body:key.connection_url,contentType:'text/plain'}));
 await p.evaluate(()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>window.lastCopied=text},configurable:true})});
 await navigate(p,'admin-keys','.fo-key-row');await p.locator('.screen:visible .fo-key-url').click();await p.waitForFunction(()=>!!window.lastCopied);assert.equal(await p.evaluate(()=>window.lastCopied),key.connection_url);
 await p.locator('.screen:visible [data-keys-filter]').click();const select=p.locator('[data-keys-filter-field=devices]');await select.selectOption('partial');await p.waitForFunction(()=>state.extra['admin-keysFilter']?.devices==='partial');
 await p.locator('.screen:visible .fo-key-actions>summary').click();const menu=p.getByRole('menu');await menu.waitFor();const rect=await menu.boundingBox();assert.ok(rect.x>=0&&rect.x+rect.width<=width+1);await p.keyboard.press('Escape');
 await capture(p,'artifacts/ui-consistency',`${engine}-${width}-${theme}-keys`);
 await navigate(p,'support','.fo-conversation');const row=p.locator('.screen:visible .fo-conversation');assert.equal(await row.locator('svg[data-icon=chevron-right]').count(),0);const preview=row.locator('.fo-conversation-preview');assert.equal(await preview.evaluate(n=>getComputedStyle(n).textAlign),'start');
 await capture(p,'artifacts/ui-consistency',`${engine}-${width}-${theme}-support`);
 assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 assert.deepEqual(model.errors,[]);await context.close();console.log('PASS consistency',engine,width,theme);
 }await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
