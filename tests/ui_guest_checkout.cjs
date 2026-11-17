/* Real guest entry point and shared UI, isolated HTTP/provider fixtures only. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const ROOT=path.resolve(__dirname,'../app/web/static'),OUT=path.resolve(__dirname,'../artifacts/guest-checkout-20261016');
const tariffs=[{id:55,months:12,devices:5,amount:120000},{id:13,months:3,devices:1,amount:25000},{id:31,months:1,devices:3,amount:19000},{id:11,months:1,devices:1,amount:10000},{id:51,months:1,devices:5,amount:29000},{id:12,months:12,devices:1,amount:90000}];
async function fixture(browser,{width=390,theme='light',before=false,available=true}={}){
 const context=await browser.newContext({viewport:{width,height:844},locale:'ru-RU',isMobile:width<768,hasTouch:width<768,reducedMotion:'reduce'});
 await context.addInitScript(theme=>localStorage.setItem('freeman-theme',theme),theme);
 const model={drafts:new Map(),pay:[],invoices:0,status:'order_pending',confirmed:false,email:[],errors:[],dropPay:false,dropDraft:false,failReceipt:false,hold:null,started:null};
 const json=(r,data,status=200)=>r.fulfill({json:data,status});
 await context.route('**/*',async r=>{
  const u=new URL(r.request().url()),req=r.request();
  if(u.origin==='https://payments.example.test')return r.fulfill({body:'Simulated YooKassa. No real payment.',contentType:'text/html'});
  if(u.origin!=='https://example.test')return r.abort();
  if(u.pathname==='/api/buy/identity')return json(r,{recognized:false});
  if(u.pathname==='/api/buy/catalog')return json(r,{available,bot_url:'https://t.me/FixtureBot',promos_enabled:true,tariffs});
  if(u.pathname==='/api/buy/draft'){
   const p=req.postDataJSON();if(!model.drafts.has(p.nonce))model.drafts.set(p.nonce,{order_id:41,amount:tariffs.find(t=>t.id===p.tariff_id).amount});
   if(model.dropDraft){model.dropDraft=false;return r.abort('failed')}
   return json(r,model.drafts.get(p.nonce));
  }
  if(u.pathname==='/api/buy/41/email'){model.email.push(req.postDataJSON());return json(r,{token:'fixture-email-token'})}
  if(u.pathname==='/api/buy/41/email/verify')return json(r,{verified:true});
  if(u.pathname==='/api/buy/41/pay'){
   model.pay.push(req.postDataJSON());model.invoices=1;
   if(model.hold){model.started?.();await model.hold}
   if(model.dropPay){model.dropPay=false;return r.abort('failed')}
   return json(r,{url:'https://payments.example.test/41',status:model.status});
  }
  if(u.pathname==='/api/buy/41/receipt'){
   if(model.failReceipt)return r.abort('failed');
   return json(r,{status:model.status,confirmed:model.confirmed,payment_url:'https://payments.example.test/41',months:1,devices:1,
    ...(model.status==='order_paid'?{key:'https://sub.example.test/fixture',freeman_id:'FO-47',expires_at:'2027-12-01T00:00:00Z',password:'Fixture-Only-123',authenticated:true}:{})});
  }
  if(u.pathname.startsWith('/api/'))return json(r,{});
  let file=path.join(ROOT,u.pathname.startsWith('/assets/')?path.basename(u.pathname):'guest-purchase.html');
  if(before){const old={'guest-purchase.html':'before.html','guest-purchase.css':'before.css','guest-purchase.js':'before.js'}[path.basename(file)];if(old)file=path.join(OUT,old)}
  if(!fs.existsSync(file))return r.fulfill({status:404,body:'Missing fixture asset'});
  return r.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':file.endsWith('.webp')?'image/webp':'text/html'});
 });
 const page=await context.newPage();page.on('pageerror',e=>model.errors.push(e.message));
 await page.goto('https://example.test/buy');await page.locator('#purchase').waitFor();
 return {context,page,model};
}
async function receipt(p){await p.locator('.pp-flow').waitFor()}
const observations=[];
(async()=>{fs.mkdirSync(OUT,{recursive:true});for(const engine of [chromium,webkit]){const browser=await engine.launch();try{
 for(const width of [320,390,1024])for(const theme of ['light','dark']){
  const name=engine.name()+'-'+width+'-'+theme;
  // Optional local evidence from the exact preserved previous assets.
  let baseline=null;
  if(fs.existsSync(path.join(OUT,'before.js'))){const f=await fixture(browser,{width,theme,before:true});try{baseline=await f.page.locator('#content').evaluate(n=>n.getBoundingClientRect().height);await f.page.screenshot({path:path.join(OUT,name+'-before.png'),fullPage:true})}finally{await f.context.close()}}
  const f=await fixture(browser,{width,theme});try{const p=f.page,m=f.model;
   assert.deepEqual(await p.locator('[data-device]').evaluateAll(ns=>ns.map(n=>Number(n.dataset.device))),[1,3,5]);
   assert.deepEqual(await p.locator('[data-tariff]').evaluateAll(ns=>ns.map(n=>Number(n.dataset.tariff))),[11,13,12]);
   const grid=await p.locator('.device-options').evaluate(n=>getComputedStyle(n).gridTemplateColumns.split(' ').length);assert.equal(grid,2);
   assert.equal(await p.locator('#promo-code,footer').count(),0);
   assert.equal(await p.locator('#email').evaluate(n=>parseFloat(getComputedStyle(n).fontSize))>=16,true);
   assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   assert.equal(await p.locator('#purchase').isEnabled(),true);
   const cta=await p.locator('#purchase').evaluate(n=>{const r=n.getBoundingClientRect(),card=n.closest('.checkout'),s=getComputedStyle(n),box=getComputedStyle(card),total=card.querySelector('.total').getBoundingClientRect();return {color:s.color,bg:s.backgroundColor,width:r.width,available:card.clientWidth-parseFloat(box.paddingLeft)-parseFloat(box.paddingRight),below:r.top>=total.bottom,height:r.height}});
   assert.equal(cta.color,'rgb(255, 255, 255)');assert.equal(cta.bg,'rgb(8, 120, 62)');assert.ok(Math.abs(cta.width-cta.available)<1);assert.ok(cta.below&&cta.height>=44);

   const height=await p.locator('#content').evaluate(n=>n.getBoundingClientRect().height);
   if(baseline&&width<768)assert.ok(height<baseline*.85,`${name}: ${height}/${baseline}`);
   await p.screenshot({path:path.join(OUT,name+'-after.png'),fullPage:true});
   await p.locator('[data-device="3"]').click();assert.equal(await p.locator('[data-device="3"]').getAttribute('aria-checked'),'true');
   await p.locator('[data-device="1"]').click();
   await p.locator('#email').fill('bad');await p.locator('#purchase').click();await p.locator('#error:not([hidden])').waitFor();assert.equal(m.pay.length,0);assert.equal(m.drafts.size,0);
   await p.locator('#email').fill('fixture@example.test');await p.locator('#purchase').click();await p.locator('#code').fill('123456');await p.locator('#verify').click();await p.waitForFunction(()=>document.querySelector('#verification').textContent.includes('подтверждена'));
   const popupPromise=p.waitForEvent('popup');await p.locator('#purchase').click();const popup=await popupPromise;await popup.waitForURL('https://payments.example.test/41');await receipt(p);
   assert.match(p.url(),/receipt=41/);assert.equal(m.invoices,1);assert.equal(m.email.length,1);assert.equal(m.pay.length,1);
   if(width===390)await p.screenshot({path:path.join(OUT,name+'-pending.png'),fullPage:true});
   assert.equal(await p.locator('footer').count(),0);await p.reload();await receipt(p);assert.equal(m.pay.length,1);
   // Confirmed payment still provisioning; must not display a key or success.
   m.confirmed=true;m.status='order_processing';await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await p.waitForFunction(()=>document.querySelector('.pp-wait h1')?.textContent==='Платёж принят');assert.equal(await p.locator('#pp-key').count(),0);assert.equal(await p.locator('.pp-wait .pp-actions a').count(),0,'Confirmed payment must not offer payment again');
   if(width===390)await p.screenshot({path:path.join(OUT,name+'-processing.png'),fullPage:true});
   m.status='order_paid';await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await p.locator('#pp-key').waitFor();assert.equal(await p.locator('#pp-key').inputValue(),'https://sub.example.test/fixture');assert.equal(m.pay.length,1);assert.deepEqual(m.errors,[]);
   observations.push({name,height,baseline,grid,flow:'email → external fixture tab → pending → reload → processing → paid'});
   await popup.close();
  }finally{await f.context.close()}
 }
 for(const scenario of ['lost pay response','lost draft response','unavailable','canceled','failed','network recovery','storage denied','delayed response','other tab retains pending','concurrent tabs']){
  const f=await fixture(browser,{available:scenario!=='unavailable'});try{const p=f.page,m=f.model;
   if(scenario==='unavailable'){assert.equal(await p.locator('#purchase').isDisabled(),true);assert.equal(m.pay.length,0);continue}
   if(scenario==='storage denied'){await p.evaluate(()=>{Storage.prototype.setItem=()=>{throw Error('denied')}});await p.locator('#purchase').click();await p.locator('#error:not([hidden])').waitFor();assert.equal(m.pay.length,0);assert.equal(m.drafts.size,0);continue}
   if(scenario==='lost pay response')m.dropPay=true;
   if(scenario==='lost draft response')m.dropDraft=true;
   if(scenario==='network recovery')m.failReceipt=true;
   let release,started;
   if(scenario==='delayed response'){m.hold=new Promise(r=>release=r);started=new Promise(r=>m.started=r)}
   let other;if(['other tab retains pending','concurrent tabs'].includes(scenario)){other=await f.context.newPage();await other.goto('https://example.test/buy');await other.locator('#purchase').waitFor()}
   if(scenario==='concurrent tabs')await Promise.all([p.locator('#purchase').click(),other.locator('#purchase').click()]);else await p.locator('#purchase').click();
   if(other){await receipt(p);if(scenario!=='concurrent tabs'){await other.locator('[data-device="3"]').click();await other.locator('#purchase').click()}await receipt(other);assert.equal(m.pay.length,1);await other.close()}
   if(scenario==='delayed response'){await started;await p.evaluate(()=>window.dispatchEvent(new Event('focus')));assert.equal(m.pay.length,1);release()}
   if(scenario==='lost draft response'){await p.locator('#error:not([hidden])').waitFor();await p.reload();await p.locator('#purchase').click();await receipt(p);assert.equal(m.drafts.size,1)}
   else {await receipt(p);await p.reload();await receipt(p)}
   m.failReceipt=false;
   if(scenario==='canceled'||scenario==='failed'){
    m.status=scenario==='canceled'?'order_canceled':'order_failed';await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await p.waitForFunction(()=>document.querySelector('.pp-wait h1')?.textContent==='Оплата не завершена');
   }else{m.status='order_paid';await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await p.locator('#pp-key').waitFor()}
   assert.equal(m.invoices,1);assert.equal(m.drafts.size,1);assert.ok(m.pay.length<=2);assert.deepEqual(m.errors,[]);
  }finally{await f.context.close()}
 }
 }finally{await browser.close()}}
 fs.writeFileSync(path.join(OUT,'browser-results.json'),JSON.stringify({scope:'Real Chromium/WebKit rendering; mocked provider/API, no physical iPhone or live transaction',observations},null,2));console.log('PASS 12 viewport/theme purchase flows + 20 recovery/negative cases, no real charges');
})().catch(e=>{console.error(e);process.exitCode=1});
