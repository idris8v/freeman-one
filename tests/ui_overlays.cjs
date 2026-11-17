/* B03: actual pilot renderers and overlay manager; isolated synthetic APIs.
   No provider E2E, physical keyboard or screen-reader certification claim. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require('playwright');
const {fixture,navigate,capture,clean}=require('./helpers/ui-b00-fixture.cjs');
const OUT=path.resolve(process.env.FREEMAN_B03_OUTPUT||'artifacts/ui-b03/overlays');
const results=[];
async function idle(page){
 await page.waitForFunction(()=>FreemanUI.diagnostics().overlays===0);
 assert.deepEqual(await page.evaluate(()=>FreemanUI.diagnostics()),{overlays:0,locked:false,observing:false});
 assert.equal(await page.locator('.fo-popover-layer').count(),0);
}
async function focused(page,selector){assert.equal(await page.locator(selector).evaluate(n=>n===document.activeElement),true,selector+' focus');}
async function geometry(page){
 const problems=await page.locator('.fo-dialog,.fo-popover').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&!n.closest('[inert]')).flatMap(n=>{
  const r=n.getBoundingClientRect(),v=visualViewport;
  return r.left<v.offsetLeft-1||r.right>v.offsetLeft+v.width+1||r.top<v.offsetTop-1||r.bottom>v.offsetTop+v.height+1||n.scrollWidth>n.clientWidth+1?[{class:n.className,rect:r.toJSON(),scroll:n.scrollWidth,client:n.clientWidth}]:[];
 }));assert.deepEqual(problems,[],'Overlay viewport/overflow');
}
async function shot(page,name){await geometry(page);await capture(page,OUT,name);}
async function run(browser,options){
 const name=`${browser.browserType().name()}-${options.width}-${options.theme}`;
 const f=await fixture(browser,options),{page,model}=f;
 try{
  await navigate(page,'keys','.my-keys-page');await page.locator('[data-key-toggle="1"]').click();
  for(let i=0;i<3;i++){
   await page.locator('[data-key-rename="1"]').click();await focused(page,'#rename-key-input');
   assert.equal(await page.locator('.fo-rename').getAttribute('aria-modal'),'true');
   const last=page.locator('.fo-rename [type=submit]');await last.focus();await page.keyboard.press('Tab');await focused(page,'#rename-key-input');
   await page.keyboard.press('Shift+Tab');await focused(page,'.fo-rename [type=submit]');
   await page.keyboard.press('Escape');await idle(page);await focused(page,'[data-key-rename="1"]');
  }
  await page.locator('[data-key-rename="1"]').click();await page.locator('#rename-key-input').fill('Сохранить черновик');
  await page.evaluate(()=>{window.__draft=document.querySelector('#rename-key-input');render()});
  assert.equal(await page.evaluate(()=>window.__draft===document.querySelector('#rename-key-input')),true,'Render retains live draft node');
  await page.keyboard.press('Escape');await page.getByRole('dialog',{name:'Закрыть без сохранения?'}).waitFor();
  await focused(page,'[data-fo-confirm="no"]');
  assert.match(await page.getByRole('dialog',{name:'Закрыть без сохранения?'}).getAttribute('aria-describedby'),/^fo-confirm-description-/);
  await shot(page,name+'-dirty-confirm');await page.locator('[data-fo-confirm="no"]').click();
  await focused(page,'#rename-key-input');assert.equal(await page.locator('#rename-key-input').inputValue(),'Сохранить черновик');
  await page.locator('.fo-rename [data-close-modal]').click();await page.locator('[data-fo-confirm="yes"]').click();
  await idle(page);assert.equal(model.writes.length,0,'Discard never writes');
  await page.locator('[data-key-devices="1"]').click();await page.getByRole('dialog',{name:'Устройства подписки'}).waitFor();
  await shot(page,name+'-devices');
  await page.locator('[data-keys-unlink="1"]').click();
  const confirm=page.locator('[data-fo-overlay="confirm"]');await confirm.waitFor();
  assert.equal(await page.locator('.keys-device-modal').evaluate(n=>!!n.closest('[inert]')),true,'Underlying dialog inert');
  await page.keyboard.press('Escape');await focused(page,'[data-keys-unlink="1"]');
  await page.locator('.keys-device-modal [data-close-modal]').first().click();await idle(page);
  await navigate(page,'plans','.fo-plans');await page.locator('[data-payment-method="balance"]').click();
  for(const dismiss of ['escape','backdrop']){
   await page.locator('[data-checkout]').click();await page.getByRole('dialog',{name:'Подтвердить оплату?'}).waitFor();
   await shot(page,name+'-checkout-'+dismiss);
   if(dismiss==='escape')await page.keyboard.press('Escape');else await page.locator('[data-fo-overlay]').click({position:{x:2,y:2}});
   await idle(page);await focused(page,'[data-checkout]');
  }
  assert.equal(model.writes.length,0,'Canceled checkout/device confirmations never mutate');clean(model);
 }finally{await f.context.close()}
 const a=await fixture(browser,{...options,admin:true});
 try{
  const {page,model}=a;await navigate(page,'admin-clients','.crm-page');
  await page.locator('[data-crm-menu="17"]').focus();await page.keyboard.press('ArrowDown');
  const menu=page.getByRole('menu');await menu.waitFor();
  assert.equal(await page.locator('[data-crm-menu="17"]').getAttribute('aria-expanded'),'true');
  const items=menu.getByRole('menuitem');assert.ok(await items.count()>1);
  await items.first().evaluate(n=>{window.__firstMenu=n});await focused(page,'[role=menuitem]:first-child');
  await page.keyboard.press('End');assert.equal(await items.last().evaluate(n=>n===document.activeElement),true);
  await page.keyboard.press('Home');assert.equal(await items.first().evaluate(n=>n===document.activeElement),true);
  await page.keyboard.press('ArrowDown');assert.equal(await items.nth(1).evaluate(n=>n===document.activeElement),true);
  await shot(page,name+'-client-menu');await page.keyboard.press('Escape');await idle(page);await focused(page,'[data-crm-menu="17"]');
  await page.locator('[data-crm-menu="17"]').click();await page.getByRole('menu').waitFor();
  await page.keyboard.press('Tab');await idle(page);assert.equal(await page.evaluate(()=>document.activeElement?.closest('.fo-popover-layer')!=null),false);
  await page.evaluate(()=>{window.__opened=[];openAdminCustomer=async id=>{window.__opened.push(id)}});
  await page.locator('[data-crm-menu="17"]').click();await page.getByRole('menuitem',{name:'Открыть клиента',exact:true}).press('Enter');
  await idle(page);assert.deepEqual(await page.evaluate(()=>window.__opened),[17],'Menu action executes once');
  await page.locator('[data-crm-filter-toggle]').click();
  if(options.width<1100){
   const sheet=page.getByRole('dialog',{name:'Фильтр',exact:true});await sheet.waitFor();
   await sheet.locator('.crm-filter-group').filter({has:page.locator('input[name=status]')}).locator('summary').click();
   await sheet.locator('input[name=status][value=active]').check();
   await shot(page,name+'-client-filters');await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="no"]').click();
   assert.equal(await sheet.locator('input[name=status][value=active]').isChecked(),true);
   await sheet.locator('[data-crm-sheet-apply]').click();await idle(page);
   await page.waitForFunction(()=>crmFilters().status==='active');
   await page.locator('.crm-list-actions>summary').click();
   await page.locator('.fo-popover [data-crm-select-page]').waitFor();
   assert.equal(await page.locator('.fo-popover').getAttribute('role'),'region','Native controls are not menuitems');
   await shot(page,name+'-list-popover');await page.keyboard.press('Escape');await idle(page);
  }else{
   await page.locator('.crm-filters.fo-popover').waitFor();await shot(page,name+'-client-filters');
   await page.locator('[data-crm-filter="status"]').selectOption('active');
   await page.waitForFunction(()=>crmFilters().status==='active');await page.keyboard.press('Escape');await idle(page);
  }
  await navigate(page,'admin-catalog','.catalog-page');await page.locator('[data-catalog-edit="vpn:11"]').click();
  await focused(page,'.fo-tariff-dialog [name=name]');await page.locator('[name=price]').fill('140.50');
  await shot(page,name+'-tariff-editor');await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="no"]').click();
  assert.equal(await page.locator('[name=price]').inputValue(),'140.50');
  await page.locator('.fo-tariff-dialog [data-close-modal]').first().click();await page.locator('[data-fo-confirm="yes"]').click();await idle(page);
  await focused(page,'[data-catalog-edit="vpn:11"]');assert.equal(model.writes.length,0);clean(model);
 }finally{await a.context.close()}
 results.push(name);console.log('PASS B03',name);
}
async function resilience(browser){
 const f=await fixture(browser),{page,model}=f;let release;
 try{
  await navigate(page,'keys','.my-keys-page');await page.locator('[data-key-toggle="1"]').click();
  const read='GET /api/web/keys/1/devices';model.gates[read]=new Promise(r=>release=r);
  await page.locator('[data-key-devices="1"]').click();await page.getByRole('status').filter({hasText:'Загружаем устройства'}).waitFor();
  await page.keyboard.press('Escape');await idle(page);release();delete model.gates[read];
  await page.waitForTimeout(100);assert.equal(await page.locator('[data-fo-overlay]').count(),0,'Late device read cannot reopen');
  // Newer read wins even if the earlier read returns last.
  const deliveries=[];let registered;
  await f.context.route('https://example.test/api/web/keys/1/devices',route=>new Promise(done=>{
   deliveries.push(async name=>{await route.fulfill({json:{key:model.account.keys[0],devices:[{id:5,name}],device_limit:3,device_sync:{status:'confirmed',count:1},actions:{unlink_all:true}}});done()});registered?.();registered=null;
  }));
  for(let i=0;i<2;i++){const received=new Promise(resolve=>registered=resolve);await page.evaluate(()=>{void keysDevices(1)});await received;}
  assert.equal(deliveries.length,2);await deliveries[1]('Newest');await page.getByText('Newest',{exact:true}).waitFor();await deliveries[0]('Stale');
  await page.waitForTimeout(100);assert.equal(await page.getByText('Stale',{exact:true}).count(),0);
  // Explicit reset remains a separate, confirmed operation; failed retries reuse its ID.
  await page.locator('[data-client-reset-devices]').click();
  let resetCalls=[];
  await f.context.route('https://example.test/api/web/keys/1/devices/reset',route=>{
   resetCalls.push(route.request().postDataJSON());return route.fulfill({status:503,json:{detail:'Fixture reset unavailable'}});
  });
  await page.locator('[data-client-reset-form] [type=submit]').click();await page.locator('.login-form-error:not([hidden])').waitFor();
  await page.locator('[data-client-reset-form] [type=submit]').click();await page.waitForFunction(()=>!document.querySelector('[data-client-reset-form] [type=submit]').disabled);
  assert.equal(resetCalls.length,2);assert.equal(resetCalls[0].request_id,resetCalls[1].request_id);assert.equal(resetCalls[0].confirmed,true);
  await page.keyboard.press('Escape');await idle(page);
  await f.context.unroute('https://example.test/api/web/keys/1/devices');
  await page.locator('[data-key-rename="1"]').click();await page.locator('#rename-key-input').fill('После ошибки');
  const write='PUT /api/web/keys/1/name';model.failure[write]=503;model.gates[write]=new Promise(r=>release=r);
  await page.locator('[data-rename-key] [type=submit]').click();await page.waitForFunction(()=>FreemanUI.busy(document.querySelector('[data-rename-key]')));
  await page.keyboard.press('Escape');await page.locator('.fo-rename [data-close-modal]').click();
  await page.locator('[data-rename-key]').dispatchEvent('submit');
  assert.equal(model.writes.filter(x=>x.key===write).length,1,'Busy duplicate submit suppressed');
  assert.equal(await page.locator('[data-fo-confirm]').count(),0,'Busy cannot dismiss');
  release();delete model.gates[write];await page.locator('.rename-key-error:not([hidden])').waitFor();
  assert.equal(await page.locator('#rename-key-input').inputValue(),'После ошибки');
  await page.locator('[data-rename-key] [type=submit]').click();await idle(page);
  assert.equal(model.account.keys[0].name,'После ошибки');
  // Long content, visual viewport resize/offset, focus containment and unmount cleanup.
  const inertBefore=await page.locator('[inert]').count();
  await page.evaluate(()=>{
   const root=document.createElement('div');root.id='b03-long';root.className='fo-overlay fo-overlay--sheet fo-scope';root.dataset.foOverlay='sheet';
   root.innerHTML='<section class="fo-dialog" data-fo-panel aria-label="Long content"><h2>Long content</h2>'+Array.from({length:30},(_,i)=>'<label class="fo-field">Field '+i+'<input class="fo-input" value="Text"></label>').join('')+'<button class="fo-button" data-fo-dismiss>Close</button></section>';
   document.body.append(root);FreemanUI.mount(root,{onClose:()=>root.remove()});
   window.__vv={width:390,height:350,offsetTop:35,offsetLeft:0,addEventListener(){},removeEventListener(){}};
   Object.defineProperty(window,'visualViewport',{configurable:true,value:window.__vv});window.dispatchEvent(new Event('resize'));
  });
  await page.waitForFunction(()=>document.querySelector('#b03-long').getBoundingClientRect().height===350);
  await geometry(page);await page.locator('#b03-long input').last().focus();
  assert.equal(await page.locator('#b03-long input').first().evaluate(n=>parseFloat(getComputedStyle(n).fontSize)>=16),true);
  assert.doesNotMatch(await page.locator('meta[name=viewport]').getAttribute('content'),/user-scalable\s*=\s*no|maximum-scale\s*=\s*1/);
  await page.evaluate(()=>{document.querySelector('#b03-long').remove()});await idle(page);
  assert.equal(await page.locator('[inert]').count(),inertBefore);clean(model);
  // Manager is explicit and independent of optional motion; duplicate inclusion
  // does not add listeners or execute menu actions twice. A nested popup belongs
  // to its modal, while native dialog top-layer semantics remain usable.
  await page.addScriptTag({path:path.resolve(__dirname,'../app/web/static/ui-system.js')});
  await page.evaluate(()=>{
   window.PageMotion=undefined;window.__actionCount=0;
   const root=document.createElement('dialog');root.id='b03-native';root.className='fo-dialog fo-scope';root.dataset.foPanel='';root.setAttribute('aria-label','Native fixture');
   root.innerHTML='<button class="fo-button" id="b03-anchor">Menu</button><button class="fo-button" id="b03-next">Next</button><div class="fo-menu" data-fo-popup="menu" data-fo-anchor="#b03-anchor"><button class="fo-button">Alpha</button><button class="fo-button">Beta</button></div>';
   document.body.append(root);root.showModal();FreemanUI.mount(root,{onClose:()=>{root.close();root.remove()}});FreemanUI.sync(root);
   document.querySelector('[role=menuitem]').addEventListener('click',()=>window.__actionCount++);
  });
  assert.equal(await page.locator('.fo-popover-layer').evaluate(n=>!!n.closest('dialog[open]')),true);
  await page.keyboard.press('b');await focused(page,'[role=menuitem]:last-child');
  await page.keyboard.press('Tab');await focused(page,'#b03-next');
  assert.equal(await page.locator('#b03-native [data-fo-popup]').isVisible(),false);
  await page.evaluate(()=>{document.querySelector('#b03-native [data-fo-popup]').hidden=false;FreemanUI.sync(document.querySelector('#b03-native'))});
  await page.keyboard.press('Enter');await page.waitForFunction(()=>FreemanUI.diagnostics().overlays===1);
  assert.equal(await page.evaluate(()=>window.__actionCount),1);
  await page.keyboard.press('Escape');await idle(page);
  assert.equal(await page.locator('#b03-native').count(),0);clean(model);
 }finally{release?.();await f.context.close()}
 const a=await fixture(browser,{admin:true});
 try{
  const {page,model}=a;await navigate(page,'admin-catalog','.catalog-page');await page.locator('[data-catalog-edit="vpn:11"]').click();
  await page.locator('[name=price]').fill('130');await page.locator('[name=confirmed]').check();
  const key='POST /api/web/admin/catalog';model.gates[key]=new Promise(r=>release=r);model.failure[key]=503;
  await page.locator('[data-catalog-form] [type=submit]').click();
  await page.keyboard.press('Escape');await page.locator('.fo-tariff-dialog [data-close-modal]').first().click();
  await page.locator('[data-catalog-form]').dispatchEvent('submit');assert.equal(model.writes.length,1);
  release();delete model.gates[key];await page.locator('[data-catalog-error]').filter({hasText:'Fixture'}).waitFor();
  await page.locator('[data-catalog-form] [type=submit]').click();await idle(page);
  assert.equal(model.writes.length,2);assert.equal(model.writes[0].body.request_id,model.writes[1].body.request_id);
  // Resize the background while a mobile draft is open: no lifecycle reset.
  await navigate(page,'admin-clients','.crm-page');await page.locator('[data-crm-filter-toggle]').click();
  await page.locator('.crm-filter-group').first().locator('summary').click();
  const field=page.locator('#crm-mobile-sheet input[name=status][value=active]');await field.check();
  await page.setViewportSize({width:1440,height:900});assert.equal(await field.isChecked(),true);
  await page.keyboard.press('Escape');await page.locator('[data-fo-confirm="yes"]').click();await idle(page);clean(model);
  await page.locator('[data-crm-menu="17"]').click();await page.getByRole('menu').waitFor();
  await page.evaluate(()=>loadView('admin-catalog'));await idle(page);
  assert.equal(await page.evaluate(()=>crm.menu),null,'Navigation clears stale menu ownership');
 }finally{release?.();await a.context.close()}
 console.log('PASS B03 resilience',browser.browserType().name());
}
(async()=>{
 fs.mkdirSync(OUT,{recursive:true});
 for(const engine of [chromium,webkit]){
  const browser=await engine.launch();try{
   for(const width of [320,390,1440])for(const theme of ['light','dark'])await run(browser,{width,theme});
   await resilience(browser);
  }finally{await browser.close()}
 }
 fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify({coverage:'Real UI/scripts; mocked APIs and viewport only. No live integrations or physical iPhone keyboard.',cases:results,resilience:['chromium','webkit']},null,2)+'\n');
})().catch(error=>{console.error(error);process.exit(1)});
