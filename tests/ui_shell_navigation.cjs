/* B04 real renderers/routes/motion with isolated synthetic APIs only. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require('playwright');
const {fixture,navigate,capture,clean}=require('./helpers/ui-b00-fixture.cjs');
const OUT=path.resolve(process.env.FREEMAN_B04_OUTPUT||'artifacts/ui-b04/navigation'),results=[];
async function settled(page){await page.waitForFunction(()=>PageMotion.diagnostics().transitions===0&&document.querySelectorAll('.screen:not([hidden])').length===1);}
async function check(page,admin,width){
 const items=await page.locator('.mobile-nav>button').evaluateAll(ns=>ns.map(n=>({view:n.dataset.view||n.dataset.aiOpen,label:n.textContent.trim()})));
 assert.deepEqual(items.map(n=>n.view),admin?['admin-clients','admin-keys','support','/admin/assistant','admin-dashboard']:['support','plans','keys','profile']);
 if(!admin)assert.equal(await page.locator('[data-ai-open]').count(),0);
 const geometry=await page.locator('.mobile-nav').evaluate(nav=>{
  const r=nav.getBoundingClientRect();if(!r.width)return [];
  return [...nav.querySelectorAll(':scope>button')].map(button=>{const b=button.getBoundingClientRect(),text=button.querySelector('span');return {inside:b.left>=r.left&&b.right<=r.right&&b.bottom<=r.bottom,hit:b.height>=44,fit:text.scrollWidth<=text.clientWidth,top:b.top};});
 });if(width<=1100){assert.equal(geometry.length,admin?5:4);assert.ok(geometry.every(n=>n.inside&&n.hit&&n.fit&&Math.abs(n.top-geometry[0].top)<1),JSON.stringify(geometry));}
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'Page horizontal overflow');
}
async function matrix(browser,options){
 const name=`${browser.browserType().name()}-${options.width}-${options.theme}`;
 for(const admin of [false,true]){
  const f=await fixture(browser,{...options,admin,motion:'no-preference'}),{page,model}=f;
  try{
   await navigate(page,admin?'admin-clients':'keys',admin?'.crm-page':'.my-keys-page');await settled(page);await check(page,admin,options.width);
   const first=admin?'admin-clients':'keys',second=admin?'admin-catalog':'plans';
   const live=await page.locator('.screen:not([hidden])').evaluate(n=>{window.__live=n;window.__entrance=n.__pageEntrance;return true});assert.ok(live);
   await page.evaluate(()=>render());assert.equal(await page.evaluate(()=>document.querySelector('.screen:not([hidden])')===window.__live&&window.__live.__pageEntrance===window.__entrance),true,'Refresh cannot replay entrance');
   await page.locator(admin?'[data-crm-select="17"]':'[data-key-toggle="1"]').click();
   await capture(page,OUT,name+'-'+(admin?'clients':'keys'));
   await navigate(page,second,admin?'.catalog-page':'.fo-plans');await settled(page);
   assert.equal(await page.locator('.mobile-nav [aria-current=page]').getAttribute('data-view'),admin?'admin-dashboard':'plans');
   await page.goBack();await page.waitForFunction(view=>state.view===view,first);await settled(page);
   if(admin)assert.equal(await page.locator('[data-crm-select="17"]').isChecked(),true,'Selection on Back');
   else assert.equal(await page.locator('[data-key-toggle="1"]').getAttribute('aria-expanded'),'true');
   await page.goForward();await page.waitForFunction(view=>state.view===view,second);await settled(page);
   if(admin){await page.locator('[data-catalog-edit="vpn:11"]').click();await page.locator('[name=price]').fill('111');await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="no"]').click();assert.equal(await page.locator('[name=price]').inputValue(),'111');await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="yes"]').click();}
   else{await page.locator('[data-checkout]').click();await page.keyboard.press('Escape');assert.equal(model.writes.length,0);}
   await capture(page,OUT,name+'-'+(admin?'catalog':'plans'));
   clean(model);
  }finally{await f.context.close()}
 }
 results.push(name);console.log('PASS B04',name);
}
async function recovery(browser){
 const f=await fixture(browser,{admin:true,motion:'no-preference'}),{page,model}=f;let release;
 try{
  await f.context.route('https://example.test/api/web/admin/users/search',route=>route.fulfill({json:{items:Array.from({length:40},(_,i)=>({id:i+17,name:'Fixture '+i,freeman_id:'FO-'+(i+17),active_keys:1,active_proxy:0,created_at:'2026-10-15T09:00:00Z'})),page:1,total:40,limit:100}}));
  await navigate(page,'admin-clients','.crm-page');await settled(page);
  await page.getByLabel('Сортировка клиентов').selectOption('name');await page.waitForFunction(()=>crmFilters().sort==='name');
  await page.locator('[data-crm-select="17"]').check();await page.locator('.screen:not([hidden])').evaluate(n=>n.scrollTop=300);
  // A representative nested customer is served only by this isolated fixture.
  await f.context.route('https://example.test/api/web/admin/customers/17/**',route=>{
   const isProfile=route.request().url().endsWith('/profile');return route.fulfill({json:isProfile?{user:{id:17,name:'Fixture',freeman_id:'FO-17',status:'active'},balance:0,counts:{},summary:{}}:{items:[],total:0,page:1,limit:10}});
  });
  await page.evaluate(()=>openAdminCustomer(17));await page.locator('.cp-heading').waitFor();await settled(page);
  await page.locator('.cp-breadcrumb [data-nav-back]').click();await page.waitForFunction(()=>typeof state!=='undefined'&&state.view==='admin-clients'&&!CabinetNavigation.restoring);await settled(page);
  assert.equal(await page.getByLabel('Сортировка клиентов').inputValue(),'name');assert.equal(await page.locator('[data-crm-select="17"]').isChecked(),true);
  assert.ok(await page.locator('.screen:not([hidden])').evaluate(n=>n.scrollTop)>=290,'Scroll returns to registry');
  // Rapid route requests, including a delayed tariff quote, must not win late.
  const quote='GET /api/web/offers/quote';model.gates[quote]=new Promise(r=>release=r);
  await page.evaluate(()=>{void loadView('plans');void loadView('admin-clients');void loadView('admin-catalog')});
  await page.waitForFunction(()=>typeof state!=='undefined'&&state.view==='admin-catalog');release();delete model.gates[quote];await settled(page);
  assert.equal(await page.evaluate(()=>state.view),'admin-catalog');
  const root=page.locator('.screen:not([hidden])');await root.evaluate(n=>{window.__entrance=n.__pageEntrance;render();render()});
  assert.equal(await root.evaluate(n=>n.__pageEntrance===window.__entrance),true);
  await page.evaluate(()=>{void loadView('admin-clients');void loadView('admin-catalog');void loadView('admin-clients')});await settled(page);
  assert.equal(await page.locator('.screen:not([hidden])').count(),1);assert.equal(await page.locator('.is-leaving,.is-entering').count(),0);
  // Reload restores the current route, filter and selection without a live integration.
  await page.reload();await page.waitForFunction(()=>typeof state!=='undefined'&&state.view==='admin-clients'&&!CabinetNavigation.restoring);await settled(page);
  assert.equal(await page.getByLabel('Сортировка клиентов').inputValue(),'name');assert.equal(await page.locator('[data-crm-select="17"]').isChecked(),true);
  // Return through a real cross-document Assistant navigation.
  await page.locator('.mobile-nav [data-ai-open="/admin/assistant"]').click();await page.waitForURL('**/admin/assistant');await page.locator('#assistant-app:not([aria-busy])').waitFor();
  const input=page.locator('textarea');await input.fill('Keep my draft');
  await page.locator('[data-menu]').click();assert.equal(await page.locator('.fa-main').evaluate(n=>n.inert),true);
  const last=page.locator('.fa-sidebar footer a');await last.focus();await page.keyboard.press('Tab');assert.equal(await page.locator('.fa-brand').evaluate(n=>n===document.activeElement),true);
  await page.keyboard.press('Escape');assert.equal(await input.inputValue(),'Keep my draft');assert.equal(await page.locator('.fa-main').evaluate(n=>n.inert),false);
  await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
  assert.equal(await page.locator('#assistant-app').evaluate(n=>getComputedStyle(n).opacity),'1');
  await page.locator('[data-menu]').click();await page.locator('.fa-sidebar footer a').click();await page.waitForURL('https://example.test/');
  await page.waitForFunction(()=>typeof state!=='undefined'&&state.view==='admin-clients'&&!CabinetNavigation.restoring);assert.equal(await page.getByLabel('Сортировка клиентов').inputValue(),'name');
  assert.equal(await page.locator('[data-crm-select="17"]').isChecked(),true);clean(model);
 }finally{release?.();await f.context.close()}
 // Shared viewport and token contracts, including pinch-zoom coordinates.
 const a=await fixture(browser,{entry:'/admin/assistant'});
 try{
  const {page}=a;await page.locator('#assistant-app:not([aria-busy])').waitFor();
  await page.evaluate(()=>{Object.defineProperty(window,'visualViewport',{configurable:true,value:{height:330,width:195,offsetTop:24,offsetLeft:0,scale:2}});dispatchEvent(new Event('resize'))});
  await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--fa-height')==='330px');
  assert.equal(await page.evaluate(()=>PageMotion.diagnostics().viewportSubscriptions),1);
  assert.doesNotMatch(await page.locator('meta[name=viewport]').getAttribute('content'),/user-scalable\s*=\s*no|maximum-scale\s*=\s*1/);
  await page.evaluate(()=>{document.documentElement.style.setProperty('--fo-motion-page','333ms');window.__testMotion=PageMotion.enter(document.querySelector('#assistant-app'))});
  // This fixture requests reduced motion, so test the public preference contract too.
  assert.equal(await page.evaluate(()=>PageMotion.reduced.matches),true);assert.equal(await page.evaluate(()=>PageMotion.tokens.page),333);
  await page.evaluate(()=>document.documentElement.style.setProperty('--fo-motion-page','not-a-duration'));assert.equal(await page.evaluate(()=>PageMotion.tokens.page),280);
  clean(a.model);
 }finally{await a.context.close()}
 console.log('PASS B04 recovery',browser.browserType().name());
}
async function interruption(browser){
 const f=await fixture(browser,{admin:true,motion:'no-preference'}),{page,model}=f;
 try{
  await navigate(page,'admin-clients','.crm-page');await settled(page);
  const outgoing=await page.evaluate(()=>{const from=document.querySelector('.screen:not([hidden])');void loadView('admin-catalog');const out=from.getAnimations()[0],to=document.querySelector('.screen.is-entering');to.__pageEntrance?.pause();out?.finish();const opacity=getComputedStyle(from).opacity;to.__pageEntrance?.play();return opacity});
  assert.equal(outgoing,'0','Outgoing page stays faded until incoming transition completes');await settled(page);
  await page.locator('[data-catalog-edit="vpn:11"]').click();await page.locator('[name=price]').fill('177');
  await page.goBack();await page.locator('[data-fo-confirm="no"]').click();
  await page.waitForFunction(()=>history.state.foCabinet.route.view==='admin-catalog');
  assert.equal(await page.locator('[name=price]').inputValue(),'177');assert.equal(await page.evaluate(()=>state.view),'admin-catalog');
  await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="yes"]').click();
  await page.evaluate(()=>{void loadView('admin-clients');void loadView('admin-catalog')});
  await page.emulateMedia({reducedMotion:'reduce'});await settled(page);
  assert.equal(await page.locator('.screen:not([hidden])').count(),1);assert.equal(await page.locator('.is-leaving,.is-entering').count(),0);
  await navigate(page,'support','.native-inbox');await page.locator('[data-support-ticket="7"]').click();
  const input=page.locator('[data-support-message] textarea');await input.fill('Retained draft');
  await page.setViewportSize({width:390,height:480});await input.focus();
  await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--chat-viewport-height')==='480px');
  const bounds=await page.evaluate(()=>({top:document.querySelector('.native-header').getBoundingClientRect().top,bottom:document.querySelector('.native-composer').getBoundingClientRect().bottom,height:innerHeight}));
  assert.ok(bounds.top>=0&&bounds.bottom<=bounds.height+1,JSON.stringify(bounds));
  await page.evaluate(()=>{window.__composer=document.querySelector('[data-support-message]');render();render()});
  assert.equal(await page.evaluate(()=>window.__composer===document.querySelector('[data-support-message]')),true);assert.equal(await input.inputValue(),'Retained draft');
  await f.context.route('https://example.test/api/web/posts**',route=>route.fulfill({json:[]}));
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))});
  assert.equal(await page.evaluate(()=>PageMotion.diagnostics().viewportSubscriptions),0);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))});
  assert.equal(await page.evaluate(()=>PageMotion.diagnostics().viewportSubscriptions),1);
  await page.locator('[data-support-back]').click();await page.waitForFunction(()=>!state.supportTicket&&!CabinetNavigation.restoring);await settled(page);
  assert.equal(await page.evaluate(()=>PageMotion.diagnostics().viewportSubscriptions),0);
  await page.goForward();await page.waitForFunction(()=>state.supportTicket===7&&!CabinetNavigation.restoring);
  assert.equal(await page.locator('[data-support-message] textarea').inputValue(),'Retained draft');
  assert.equal(model.writes.length,0,'Navigation/dirty cancellation cannot mutate business records');clean(model);
 }finally{await f.context.close()}
 console.log('PASS B04 interruption/viewport',browser.browserType().name());
}
(async()=>{
 fs.mkdirSync(OUT,{recursive:true});
 for(const engine of [chromium,webkit]){const browser=await engine.launch();try{
  for(const width of [320,390,900,1440])for(const theme of ['light','dark'])await matrix(browser,{width,theme});
  await recovery(browser);
  await interruption(browser);
 }finally{await browser.close()}}
 fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify({coverage:'Real UI/JS/CSS with synthetic APIs. No production/provider E2E or physical-device certification.',cases:results,recovery:['chromium','webkit']},null,2)+'\n');
})().catch(error=>{console.error(error);process.exit(1)});
