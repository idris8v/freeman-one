/* Real website scripts in Chromium/WebKit; fixture APIs, no external providers. */
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require('playwright');
const ROOT=path.resolve(__dirname,'../app/web/static');
async function fixture(engine,kind='email',delayNotifications=false){
 const browser=await engine.launch(),context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
 const model={name:'Original',failure:null,requests:[],profile:{is_admin:false,name:kind,freeman_id:'FO-47',telegram_id:kind==='email'?null:123,email:kind==='telegram'||kind==='legacy'?null:'fixture@example.test',email_verified:kind==='email'||kind==='linked',registration_source:kind==='legacy'||kind==='telegram'?'telegram':'website_email'}};
 if(delayNotifications)model.notificationGate=new Promise(resolve=>model.releaseNotifications=resolve);
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());
  if(u.origin!=='https://example.test')return route.fulfill({status:404,body:''});
  if(u.pathname==='/api/web/keys/1/name'){
   const body=req.postDataJSON();model.requests.push(body);
   const failure=model.failure;model.failure=null;if(model.renameGate)await model.renameGate;
   if(failure==='network')return route.abort('failed');
   if(failure==='lost response'){model.name=body.name.trim();return route.abort('failed')}
   if(failure)return route.fulfill({status:Number(failure),json:{detail:failure==='422'?[{loc:['body','name'],msg:'String should have at most 60 characters'}]:failure==='404'?'Ключ не найден':failure==='401'?'Сессия истекла':'Не удалось сохранить название'}});
   model.name=body.name.trim();return route.fulfill({json:{id:1,name:model.name}});
  }
  if(u.pathname==='/api/web/me')return route.fulfill({json:{profile:model.profile,balance:0,summary:{keys:1,keys_active:1},keys:[{id:1,subscription_id:11,name:model.name,status:'active',duration_months:1,expires_at:'2028-11-10T12:00:00Z',device_limit:1,devices:[]}],tariffs:[],payments:{},trial:{available:false},useful_links:[],news:[]}});
  if(u.pathname==='/api/web/keys/1/connection')return route.fulfill({json:{value:'https://sub.example.test/fixture'}});
  if(u.pathname==='/api/account/notifications'&&model.notificationGate)await model.notificationGate;
  if(u.pathname.startsWith('/api/'))return route.fulfill({json:u.pathname==='/api/account/notifications'?{items:[]}: {}});
  const file=path.join(ROOT,u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');
  if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
  return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'});
 });
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.dismiss());
 await page.goto('https://example.test');await page.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);
 async function keys(){await page.locator('[data-view="keys"]:visible').first().click();await page.locator('[data-key-toggle="1"]').click()}
 async function open(){await page.locator('[data-key-rename="1"]').click();await page.locator('[data-rename-key="1"]').waitFor()}
 async function save(name){await page.locator('#rename-key-input').fill(name);await page.locator('[data-rename-key="1"] [type=submit]').click()}
 await keys();return {browser,page,model,errors,keys,open,save};
}
for(const [engineName,engine] of Object.entries({chromium,webkit})){
 for(const kind of ['email','linked','telegram','legacy'])test(`${engineName}: ${kind} key name persists after reload`,{timeout:30000},async()=>{
  const f=await fixture(engine,kind);try{
   await f.open();assert.equal(await f.page.locator('#rename-key-input').getAttribute('maxlength'),'60');
   await f.save('  Мой телефон 📱  ');await f.page.waitForFunction(()=>!state.modal);
   assert.equal(f.model.name,'Мой телефон 📱');assert.equal(await f.page.evaluate(()=>state.data.keys[0].name),f.model.name);
   await f.page.reload();await f.page.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);await f.keys();
   assert.equal(await f.page.locator('.my-key-card strong').first().innerText(),f.model.name);assert.deepEqual(f.errors,[]);
  }finally{await f.browser.close()}
 });
 for(const failure of ['422','404','401','500','network','lost response'])test(`${engineName}: key rename recovers from ${failure}`,{timeout:30000},async()=>{
  const f=await fixture(engine);try{
   await f.open();f.model.failure=failure;await f.save('Recovered name');
   const error=f.page.locator('.rename-key-error');await error.waitFor({state:'visible'});
   const message=await error.innerText();assert.ok(message.length>0);assert.doesNotMatch(message,/\[object Object\]/);
   if(failure==='422')assert.match(message,/1 до 60/);
   assert.equal(await f.page.evaluate(()=>state.data.keys[0].name),'Original');
   assert.equal(await f.page.locator('[data-rename-key="1"] [type=submit]').isEnabled(),true);
   assert.equal(f.model.name,failure==='lost response'?'Recovered name':'Original');
   await f.save('Recovered name');await f.page.waitForFunction(()=>!state.modal);assert.equal(f.model.requests.length,2);
   await f.page.reload();await f.page.waitForFunction(()=>typeof state!=='undefined'&&state.data?.keys[0]?.name==='Recovered name');assert.deepEqual(f.errors,[]);
  }finally{await f.browser.close()}
 });
 test(`${engineName}: notification arrival preserves in-flight rename error`,{timeout:30000},async()=>{
  const f=await fixture(engine,'email',true);try{
   await f.open();f.model.failure='network';let release;f.model.renameGate=new Promise(resolve=>release=resolve);
   await f.save('Recovery');await f.page.waitForFunction(()=>document.querySelector('[data-rename-key] [type=submit]')?.disabled);
   f.model.releaseNotifications();await f.page.waitForFunction(()=>state.extra.expiryNotifications!==undefined);release();
   await f.page.locator('.rename-key-error').waitFor({state:'visible'});assert.equal(await f.page.locator('#rename-key-input').inputValue(),'Recovery');
   assert.equal(await f.page.locator('[data-rename-key] [type=submit]').isEnabled(),true);
   await f.save('Recovery');await f.page.waitForFunction(()=>!state.modal);assert.equal(f.model.name,'Recovery');assert.deepEqual(f.errors,[]);
  }finally{f.model.releaseNotifications?.();await f.browser.close()}
 });
 test(`${engineName}: key rename validation and HTML escaping`,{timeout:30000},async()=>{
  const f=await fixture(engine);try{
   await f.open();await f.save('   ');await f.page.locator('.rename-key-error').waitFor({state:'visible'});assert.equal(f.model.requests.length,0);
   const name='<img src=x onerror="alert(1)">';await f.save(name);await f.page.waitForFunction(()=>!state.modal);
   assert.equal(await f.page.locator('.my-key-card strong').first().innerText(),name);assert.equal(await f.page.locator('.my-key-card img[src="x"]').count(),0);assert.deepEqual(f.errors,[]);
  }finally{await f.browser.close()}
 });
}
