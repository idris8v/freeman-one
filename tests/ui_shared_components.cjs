/* B02 real renderers + shared CSS. Synthetic APIs only; no backend/provider E2E. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require('playwright');
const {fixture,navigate,capture,clean}=require('./helpers/ui-b00-fixture.cjs');
const OUT=path.resolve(process.env.FREEMAN_B02_OUTPUT||'artifacts/ui-b02/components');
const results=[];
async function boundary(page,selector){
 const issues=await page.locator(selector).evaluate(root=>{
  const problems=[];
  for(const n of root.querySelectorAll('button,input,select')){
   if(!n.getClientRects().length)continue;
   if(!n.matches('.fo-button,.fo-input,.fo-check,.fo-radio,.fo-select,.fo-choice,.fo-disclosure,.fo-tab,.fo-switch-input,.fo-row-action'))problems.push('Unmigrated '+n.outerHTML.slice(0,150));
  }
  function scan(rules){for(const rule of rules){
   if(rule.media&&!matchMedia(rule.media.mediaText).matches)continue;
   if(rule.cssRules){scan(rule.cssRules);continue;}
   if(!rule.selectorText||!rule.style)continue;
   const priority=[...rule.style].filter(p=>rule.style.getPropertyPriority(p)&&!p.startsWith('animation')&&!p.startsWith('transition')&&p!=='scroll-behavior');
   if(!priority.length)continue;
   let matches;try{matches=[...root.querySelectorAll(rule.selectorText)];if(root.matches(rule.selectorText))matches.push(root);}catch{continue;}
   if(matches.length)problems.push('Legacy priority: '+rule.selectorText+' '+priority.join(','));
  }}
  for(const sheet of document.styleSheets)scan(sheet.cssRules);
  return problems;
 });assert.deepEqual(issues,[],selector);
 const box=await page.locator(selector).evaluate(n=>({width:n.clientWidth,scroll:n.scrollWidth}));
 assert.ok(box.scroll<=box.width+1,selector+' horizontal overflow '+JSON.stringify(box));
}
async function style(page,selector,property,token){
 const result=await page.locator(selector).first().evaluate((n,{property,token})=>{const cs=getComputedStyle(n),probe=document.createElement('span');probe.style[property]=`var(${token})`;probe.style.position='absolute';document.body.append(probe);const expected=getComputedStyle(probe)[property];probe.remove();return {actual:cs[property],expected};},{property,token});
 assert.equal(result.actual,result.expected,selector+' '+property);
}
async function run(browser,options){
 const prefix=`${browser.browserType().name()}-${options.width}-${options.theme}`;
 const f=await fixture(browser,options),{page,model}=f;
 try{
  await navigate(page,'keys','.my-keys-page');await page.locator('[data-key-toggle="1"]').click();
  await style(page,'[data-key-install="1"]','backgroundColor','--fo-control-primary');
  await style(page,'[data-key-renew="1"]','backgroundColor','--fo-control-success');
  await style(page,'.my-keys-buy','backgroundColor','--fo-control-success');
  await boundary(page,'.fo-keys');
  // Loading/error/retry are the existing device lifecycle, now with shared leaves.
  const request='GET /api/web/keys/1/devices';let release;
  model.gates[request]=new Promise(r=>release=r);model.failure[request]=503;
  await page.locator('[data-key-devices="1"]').click();
  await page.getByRole('status').filter({hasText:'Загружаем устройства'}).waitFor();
  await boundary(page,'.fo-key-dialog');release();delete model.gates[request];
  await page.getByRole('heading',{name:'Устройства недоступны'}).waitFor();
  await boundary(page,'.fo-key-dialog');await page.locator('[data-keys-devices="1"]').click();
  await page.getByRole('dialog',{name:'Устройства подписки'}).waitFor();
  await boundary(page,'.fo-key-dialog');assert.equal(await page.locator('.key-device-detail').count(),2);
  let deletes=0,remaining=structuredClone(model.account.keys[0].devices);
  await f.context.route('https://example.test/api/web/keys/1/devices/1',route=>{
   assert.equal(route.request().method(),'DELETE');deletes++;remaining=remaining.filter(d=>d.id!==1);return route.fulfill({json:{ok:true}});
  });
  await f.context.route('https://example.test/api/web/keys/1/devices',route=>route.fulfill({json:{key:model.account.keys[0],devices:remaining,device_limit:3,device_sync:{status:'confirmed',count:remaining.length}}}));
  await page.locator('[data-keys-unlink="1"]').click();await page.locator('[data-fo-confirm="yes"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.key-device-detail').length===1);assert.equal(deletes,1);
  await boundary(page,'.fo-key-dialog');
  await page.locator('.fo-key-dialog [data-close-modal]').first().click();
  await f.context.route('https://example.test/api/web/keys/1/connection',route=>route.fulfill({json:{kind:'vless',value:'https://sub.example.test/fixture'}}));
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__copiedKey=value}}}));
  await page.locator('[data-copy-key="1"]').click();await page.waitForFunction(()=>window.__copiedKey==='https://sub.example.test/fixture');
  let autoWrites=0;
  await f.context.route('https://example.test/api/web/keys/1/autorenew',route=>{
   const body=route.request().postDataJSON();assert.equal(route.request().method(),'POST');assert.equal(body.tariff_id,13);autoWrites++;model.account.keys[0].auto_renew=body.enabled;return route.fulfill({json:{ok:true}});
  });
  await page.locator('[data-autorenew="1"]').click();await page.locator('[data-fo-confirm="yes"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-autorenew="1"]')?.getAttribute('aria-checked')==='true');assert.equal(autoWrites,1);
  await f.context.route('https://example.test/api/web/keys/1/qr',route=>route.fulfill({contentType:'image/svg+xml',body:fs.readFileSync(path.resolve(__dirname,'../app/web/static/demo-key-qr.svg'))}));
  await page.locator('[data-key-qr="1"]').click();await page.locator('.qr-modal').waitFor();await boundary(page,'.qr-modal');
  await page.waitForFunction(()=>document.querySelector('.qr-modal img')?.naturalWidth>0);await page.locator('.qr-modal [data-close-modal]').click();
  await page.locator('[data-key-install="1"]').click();await page.locator('.install-modal').waitFor();assert.match(await page.locator('.install-modal').innerText(),/Happ/);
  await boundary(page,'.install-modal');await page.locator('.install-done').click();
  await navigate(page,'plans','.fo-plans');
  await page.locator('[data-device-limit="3"]').click();
  assert.equal(await page.locator('[data-plan="13"]').getAttribute('aria-pressed'),'true');
  await page.locator('[data-payment-method="balance"]').click();
  await style(page,'[data-checkout]','backgroundColor','--fo-control-success');
  await boundary(page,'.fo-plans');
  if(options.width<768){
   await page.locator('[data-checkout]').evaluate(n=>{for(let p=n.parentElement;p;p=p.parentElement){if(/auto|scroll/.test(getComputedStyle(p).overflowY))p.scrollTop=p.scrollHeight;}window.scrollTo(0,document.documentElement.scrollHeight);});
   const bounds=await page.evaluate(()=>({button:document.querySelector('[data-checkout]').getBoundingClientRect().toJSON(),nav:document.querySelector('.mobile-nav').getBoundingClientRect().toJSON()}));
   assert.ok(bounds.button.top>=0&&bounds.button.bottom<=bounds.nav.top-8,JSON.stringify(bounds));
   if(options.width===390)await capture(page,OUT,prefix+'-checkout-reachable');
  }
  await page.locator('[data-tariff-view="grid"]').click();await boundary(page,'.fo-plans');
  await page.locator('[data-checkout]').click();await boundary(page,'.fo-checkout-confirmation');
  assert.equal(model.writes.filter(x=>/checkout|\/pay/.test(x.key)).length,0,'Viewing cannot charge');
  await page.keyboard.press('Escape');assert.equal(await page.locator('[data-checkout]').evaluate(n=>n===document.activeElement),true);
  clean(model);
 }finally{await f.context.close();}
 const admin=await fixture(browser,{...options,admin:true});
 try{
  const {page,model}=admin;await navigate(page,'admin-clients','.crm-page');
  await page.waitForFunction(()=>!document.querySelector('[data-client-row="18"] .admin-list-avatar'));
  await boundary(page,'.fo-clients');
  const rows=await page.locator('.crm-row').evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return {height:r.height,right:r.right,children:[...n.children].filter(c=>c.getBoundingClientRect().width).map(c=>c.getBoundingClientRect().toJSON())};}));
  assert.ok(rows.every(r=>r.height<=60&&r.children.every(c=>c.right<=r.right+1)),JSON.stringify(rows));
  await page.locator('[data-crm-select="17"]').check();assert.equal(await page.evaluate(()=>crm.selected.has(17)),true);
  await style(page,'[data-crm-select="17"]','width','--fo-check-size');
  await page.locator('[data-crm-menu="17"]').click();await boundary(page,'.fo-clients');
  await page.locator('[data-crm-menu="17"]').click();await page.locator('[data-crm-filter-toggle]').click();
  if(options.width<1100){await page.getByRole('dialog',{name:'Фильтр',exact:true}).waitFor();await page.locator('.crm-filter-group summary').first().click();await boundary(page,'.fo-scope.crm-sheet');await page.keyboard.press('Escape');}
  else await boundary(page,'.crm-filters');
  await navigate(page,'admin-catalog','.catalog-page');await page.locator('[data-catalog-edit="vpn:11"]').click();
  await boundary(page,'.fo-tariff-dialog');
  await style(page,'.fo-tariff-dialog [name=price]','fontSize','--fo-control-input-font');
  assert.equal(await page.locator('[name=months]').getAttribute('readonly'),'');
  // The native form and existing request identifier survive a server failure.
  await page.locator('[name=confirmed]').check();await page.locator('[name=price]').fill('125.50');
  model.failure['POST /api/web/admin/catalog']=503;
  await page.locator('[data-catalog-form] [type=submit]').click();await page.locator('[data-catalog-error]').filter({hasText:'Fixture'}).waitFor();
  await boundary(page,'.fo-tariff-dialog');await page.locator('[data-catalog-form] [type=submit]').click();
  await page.waitForFunction(()=>!state.modal);
  const writes=model.writes.filter(x=>x.key==='POST /api/web/admin/catalog');assert.equal(writes.length,2);assert.equal(writes[0].body.request_id,writes[1].body.request_id);
  await page.reload();await page.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);await navigate(page,'admin-catalog','.catalog-page');
  await page.waitForFunction(()=>Catalog.data?.vpn[0].price===12550);clean(model);
  model.failure['GET /api/web/admin/catalog']=503;
  await page.evaluate(()=>loadCatalog());await page.getByRole('alert').filter({hasText:'Fixture'}).waitFor();
  await boundary(page,'.fo-catalog');await page.locator('[data-catalog-retry]').click();
  await page.waitForFunction(()=>!Catalog.loading&&!Catalog.error);
  await page.locator('[data-catalog-edit="vpn:11"]').waitFor();
  // A focused fixture exercises shared states absent from some real pilot data.
  await page.evaluate(()=>{document.querySelector('.screen:not([hidden])').innerHTML=`<section class="fo-scope fo-card" id="fo-state-fixture"><h2>Shared component states</h2><div class="fo-row"><button class="fo-button" id="normal">Secondary</button><button class="fo-button fo-icon-button" aria-label="Add">+</button><button class="fo-button fo-button--primary">Install</button><button class="fo-button fo-button--success">Renew</button><button class="fo-button fo-button--danger">Delete</button><button class="fo-button" disabled>Disabled</button><button class="fo-button" disabled aria-busy="true">Saving</button></div><label class="fo-field">Name<input class="fo-input" aria-invalid="true" aria-describedby="field-error" value="Invalid"></label><p id="field-error" class="fo-feedback" data-tone="danger" role="alert">Please check the field</p><label class="fo-check-label"><input class="fo-check" type="checkbox" checked>Checkbox</label><label class="fo-check-label"><input class="fo-radio" type="radio" name="fixture" checked>Radio</label><label class="fo-field">Select<select class="fo-select"><option>Selected</option></select></label><div class="fo-group"><div class="fo-row">Grouped row</div><div class="fo-row">Grouped row</div></div><p class="fo-status" data-tone="success">Active</p><p class="fo-status" data-tone="warning">Expiring</p><p class="fo-feedback" data-tone="success">Saved</p><div class="fo-loading" role="status"><i class="fo-spinner" aria-hidden="true"></i>Loading</div><div class="fo-empty">Nothing here yet</div></section>`;});
  await page.locator('#normal').press('ArrowRight');await boundary(page,'#fo-state-fixture');
  assert.equal(await page.locator('#normal').evaluate(n=>getComputedStyle(n).outlineStyle),'solid',JSON.stringify(await page.locator('#normal').evaluate(n=>({focused:document.activeElement===n,visible:n.matches(':focus-visible'),html:n.outerHTML}))));
  const field=page.locator('#fo-state-fixture .fo-input');await field.focus();
  const viewport=await page.evaluate(()=>({scale:visualViewport.scale,width:innerWidth}));assert.equal(viewport.width,options.width);assert.equal(viewport.scale,1);
  const contrast=await page.locator('#fo-state-fixture .fo-button:not(:disabled),#fo-state-fixture .fo-status,#fo-state-fixture .fo-feedback').evaluateAll(nodes=>{
   const rgb=value=>(value.match(/[\d.]+/g)||[]).slice(0,3).map(Number);
   const luminance=value=>(Array.isArray(value)?value:rgb(value)).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
   return nodes.map(n=>{const s=getComputedStyle(n),a=luminance(s.color),base=rgb(s.backgroundColor),alphas=[...s.backgroundImage.matchAll(/rgba\(255,\s*255,\s*255,\s*([\d.]+)\)/g)].map(m=>Number(m[1])),alpha=Math.max(0,...alphas),b=luminance(base.map(v=>v*(1-alpha)+255*alpha));return {text:n.textContent,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};});
  });assert.ok(contrast.every(x=>x.ratio>=4.5),JSON.stringify(contrast));
  if(options.width===1440){
   const normal=page.locator('#normal');await normal.hover();await page.mouse.down();
   assert.notEqual(await normal.evaluate(n=>getComputedStyle(n).transform),'none','Pressed feedback');await page.mouse.up();
  }
  if(options.width===390||options.width===1440){fs.mkdirSync(OUT,{recursive:true});await capture(page,OUT,prefix+'-states');}
  results.push(prefix);clean(model);
 }finally{await admin.context.close();}
 console.log('PASS B02',prefix);
}
(async()=>{for(const engine of [chromium,webkit]){const browser=await engine.launch();try{for(const width of [320,390,1440])for(const theme of ['light','dark'])await run(browser,{width,theme});}finally{await browser.close()}}
 fs.mkdirSync(OUT,{recursive:true});fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify({coverage:'Real UI/CSS, synthetic APIs; no provider E2E or physical iPhone keyboard',cases:results},null,2)+'\n');
})().catch(e=>{console.error(e);process.exit(1)});
