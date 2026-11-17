/* Real cabinet scripts in Chromium/WebKit; all URLs and payments are fixtures. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium,webkit}=require('playwright');
const ROOT=path.resolve(__dirname,'../app/web/static');
const params={months:1,method:'balance',use_balance:false,subscription_id:null};
const normalize=p=>({months:p.months,...(p.tariff_id!=null?{tariff_id:p.tariff_id}:{}),method:p.method,use_balance:!!p.use_balance,subscription_id:p.subscription_id||null});
const account=()=>({profile:{is_admin:false,name:'Fixture',freeman_id:'FO-47',email:'fixture@example.test',email_verified:true},
 balance:100000,summary:{keys:1,keys_active:1},keys:[{id:1,subscription_id:11,name:'Existing VPN',duration_months:1,device_limit:1,status:'active',expires_at:'2028-11-10T12:00:00Z',devices:[],connection_loaded:true,display_key:'https://sub.example.test/fixture'}],
 tariffs:[{id:1,months:1,price:10000,device_limit:1},{id:3,months:3,price:30000,device_limit:3}],payments:{owner_free:false,yookassa:true},trial:{available:false},useful_links:[],news:[],bot_url:'https://t.me/FixtureBot'});
async function fixture(engine){
 const browser=await engine.launch(),context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
 const model={account:account(),operations:new Map(),requests:[],charges:0,invoices:0,lost:null,brokenConnection:false,hold:null,paid:false,paymentGate:null,paymentStarted:null,paymentReads:0,paymentStatus:null,confirmed:false,failStatus:false,failLookup:false,snapshotStatus:false,gatePage:null,activeReads:new Map(),maxActiveReads:0,profileGate:null,profileStarted:null,scriptGate:null,scriptStarted:null};
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());
  if(u.origin!=='https://example.test')return route.fulfill({body:'External fixture'});
  const json=(data,status=200)=>route.fulfill({status,json:data});
  if(u.pathname==='/api/web/checkout'&&req.method()==='GET'){
   if(model.failLookup)return route.abort('failed');
   const op=model.operations.get(model.account.profile.freeman_id+':'+u.searchParams.get('idempotency_key'));
   return json(op||{detail:'Not registered'},op?200:404);
  }
  if(u.pathname==='/api/web/checkout/cancel'){
   const body=req.postDataJSON(),id=model.account.profile.freeman_id+':'+body.idempotency_key;
   let op=model.operations.get(id)||{request:normalize(body),result:null};
   if(!op.payment_id&&!op.result)op.result={status:'canceled'};
   model.operations.set(id,op);return json(op);
  }
  if(u.pathname==='/api/web/checkout'){
   const body=req.postDataJSON(),id=model.account.profile.freeman_id+':'+body.idempotency_key;model.requests.push(body);
   if(model.lost==='before'){model.lost=null;return route.abort('failed')}
   let op=model.operations.get(id);
   if(op&&JSON.stringify(op.request)!==JSON.stringify(normalize(body)))return json({detail:'Key conflict'},409);
   if(!op){
    if(body.method==='balance'){
     model.charges++;model.account.balance-=10000;
     model.account.keys.push({...model.account.keys[0],id:2,subscription_id:12,name:'Purchased VPN'});
     op={request:normalize(body),result:{status:'paid',subscription_id:12,key_id:2,amount:10000}};
    }else{
     model.invoices++;op={request:normalize(body),payment_id:model.invoices,result:{status:'pending',payment_id:model.invoices,redirect_url:'https://payments.example.test/'+model.invoices,amount:10000,total:10000,balance_applied:0}};
    }
    model.operations.set(id,op);
   }
   if(model.hold)await model.hold;
   if(model.lost==='after'){model.lost=null;return route.abort('failed')}
   return json(op.result);
  }
  if(u.pathname.startsWith('/api/web/payments/')){
   model.paymentReads++;
   if(model.failStatus)return route.abort('failed');
   const page=req.frame().page(),active=(model.activeReads.get(page)||0)+1;model.activeReads.set(page,active);model.maxActiveReads=Math.max(model.maxActiveReads,active);
   const status=()=>({id:Number(u.pathname.split('/').at(-1)),status:model.paymentStatus||(model.paid?'order_paid':'order_pending'),confirmed:model.confirmed,key_id:model.paid?2:null,subscription_id:model.paid?12:null});
   const snapshot=model.snapshotStatus?status():null;
   try{
    if(model.paymentGate&&(page===model.gatePage||req.frame().url().includes('audit_return=1'))){const gate=model.paymentGate;model.paymentGate=null;model.paymentStarted?.();await gate}
    const data=snapshot||status();
    if(data.status==='order_paid'&&!model.account.keys.some(k=>k.id===2))model.account.keys.push({...model.account.keys[0],id:2,subscription_id:12});
    return json(data);
   }finally{model.activeReads.set(page,active-1)}
  }
  if(u.pathname.endsWith('/connection'))return json(model.brokenConnection?{detail:'Injected connection delay'}:{value:'https://sub.example.test/fixture'},model.brokenConnection?503:200);
  if(u.pathname.startsWith('/api/')){
   if(u.pathname==='/api/web/me'){const snapshot=structuredClone(model.account);if(model.profileGate&&req.frame().url().includes('audit_profile=1')){const gate=model.profileGate;model.profileGate=null;model.profileStarted?.();await gate}return json(snapshot)}
   if(u.pathname==='/api/account/notifications')return json({items:[]});
   if(u.pathname==='/api/web/offers/quote')return json({amount:10000,discount:0});
   return json({});
  }
  const file=path.join(ROOT,u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');
  if(!fs.existsSync(file))return route.fulfill({status:404,body:'Missing fixture asset'});
  if(path.basename(file)==='app.js'&&model.scriptGate&&req.frame().url().includes('audit_script=1')){const gate=model.scriptGate;model.scriptGate=null;model.scriptStarted?.();await gate}
  return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'});
 });
 const errors=[];
 async function page(url='https://example.test'){const p=await context.newPage();p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.dismiss());await p.goto(url);await p.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);return p}
 return {browser,context,model,errors,page};
}
async function send(page,p=params){return page.evaluate(async p=>{try{await CheckoutRecovery.submit(p,null);return null}catch(e){return e.message}},p)}
async function ready(page){await page.waitForFunction(()=>typeof state!=='undefined'&&!state.busy&&state.view==='purchase'&&state.purchase?.stage==='success')}
for(const [name,engine] of Object.entries({chromium,webkit})){
 for(const scenario of ['wallet retry','wallet reload','card reload','request not delivered','connection delayed','change unstarted plan','keep pending invoice','two tabs','account isolation','storage denied','completed purchase then new purchase']){
  test(`${name}: ${scenario}`,{timeout:30000},async()=>{
   const f=await fixture(engine);try{
    const p=await f.page(),m=f.model;
    if(scenario==='storage denied'){
     await p.evaluate(()=>{Storage.prototype.setItem=function(){throw Error('Injected storage denial')}});
     assert.match(await send(p),/storage denial/);assert.equal(m.requests.length,0);
    }else if(scenario==='two tabs'){
     const other=await f.page();let release;m.hold=new Promise(r=>release=r);
     const first=send(p);await p.waitForFunction(()=>!!localStorage.getItem('freeman-checkout:FO-47'));
     const second=send(other);await new Promise(r=>setTimeout(r,100));release();
     assert.deepEqual(await Promise.all([first,second]),[null,null]);
     assert.equal(m.requests.length,1);assert.equal(m.charges,1);
    }else if(scenario==='completed purchase then new purchase'){
     assert.equal(await send(p),null);const first=m.requests[0].idempotency_key;
     await new Promise(r=>setTimeout(r,20));assert.equal(await send(p),null);
     assert.equal(m.charges,2);assert.notEqual(m.requests[1].idempotency_key,first);
    }else if(scenario==='connection delayed'){
     m.brokenConnection=true;assert.equal(await send(p),null);assert.equal(m.charges,1);
     m.brokenConnection=false;assert.equal(await send(p),null);await ready(p);assert.equal(m.requests.length,1);
    }else if(scenario==='keep pending invoice'){
     assert.equal(await send(p,{...params,method:'yookassa'}),null);
     assert.equal(await send(p,{...params,months:3}),null);
     assert.equal(m.invoices,1);assert.equal(m.requests.length,1);assert.equal(m.charges,0);
    }else if(scenario==='change unstarted plan'){
     m.lost='before';assert.ok(await send(p));const original=m.requests[0].idempotency_key;
     assert.equal(await send(p,{...params,months:3}),null);
     assert.notEqual(m.requests[1].idempotency_key,original);
     assert.equal(m.operations.get('FO-47:'+original).result.status,'canceled');assert.equal(m.charges,1);
    }else if(scenario==='account isolation'){
     m.lost='after';assert.ok(await send(p));const old=m.requests[0].idempotency_key;
     m.account.profile.freeman_id='FO-48';await p.reload();await p.waitForFunction(()=>typeof state!=='undefined'&&state.data?.profile.freeman_id==='FO-48');
     assert.equal(m.requests.length,1);assert.equal(await p.evaluate(()=>state.view),'profile');
     m.account.profile.freeman_id='FO-47';await p.reload();await ready(p);
     assert.equal(m.requests.length,1);assert.equal(m.operations.get('FO-47:'+old).result.status,'paid');
    }else{
     const card=scenario==='card reload',before=scenario==='request not delivered';
     m.lost=before?'before':'after';assert.ok(await send(p,card?{...params,method:'yookassa'}:params));
     const original=m.requests[0].idempotency_key;
     if(scenario==='wallet retry')assert.equal(await send(p),null);
     else {await p.reload();await p.waitForFunction(()=>typeof state!=='undefined'&&state.view==='purchase');if(!card)await ready(p)}
     assert.equal(new Set(m.requests.map(r=>r.idempotency_key)).size,1);
     assert.equal(m.requests[0].idempotency_key,original);
     assert.equal(m.charges,card?0:1);assert.equal(m.invoices,card?1:0);
     if(!before)assert.equal(m.requests.length,1);else assert.equal(m.requests.length,2);
     if(card){
      assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).confirmedAt||null),null);
      m.paid=true;await p.evaluate(()=>checkReturnedPayment(1));await ready(p);
      assert.ok(await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).confirmedAt));
     }
    }
    assert.deepEqual(f.errors,[]);
   }finally{await f.browser.close()}
  });
 }
}

for(const [name,engine] of Object.entries({chromium,webkit})){
 for(const method of ['balance','yookassa']){
  test(name+': same-duration tariff ID survives lost response and reload ('+method+')',{timeout:30000},async()=>{
   const f=await fixture(engine);
   try{
    f.model.account.tariffs.push({id:5,months:1,price:22000,device_limit:5});
    const p=await f.page(),desired={...params,tariff_id:5,method};
    f.model.lost='before';
    assert.ok(await send(p,desired));
    const key=f.model.requests[0].idempotency_key;
    await p.reload();
    await p.waitForFunction(()=>typeof state!=='undefined'&&state.view==='purchase');
    assert.equal(await p.evaluate(()=>state.selectedPlan),5);
    assert.equal(f.model.requests.length,2);
    assert.ok(f.model.requests.every(r=>r.tariff_id===5&&r.idempotency_key===key));
    assert.equal(f.model.operations.get('FO-47:'+key).request.tariff_id,5);
    assert.equal(f.model.charges,method==='balance'?1:0);
    assert.equal(f.model.invoices,method==='yookassa'?1:0);
    assert.deepEqual(f.errors,[]);
   }finally{await f.browser.close()}
  });
 }
}

// The return tab completes while the reloaded original waits for its Web Lock.
for(const [name,engine] of Object.entries({chromium,webkit})){
 test(name+': return tab wins checkout lock during original reload',{timeout:30000},async()=>{
  const f=await fixture(engine);let release;
  try{
   const original=await f.page();assert.equal(await send(original,{...params,method:'yookassa'}),null);
   const started=new Promise(resolve=>{f.model.paymentStarted=resolve});
   f.model.paymentGate=new Promise(resolve=>{release=resolve});
   const returned=await f.page('https://example.test/?audit_return=1&payment=return&payment_id=1');await started;
   await original.reload();await original.waitForFunction(()=>typeof state!=='undefined'&&state.data?.keys.length===1);
   f.model.paid=true;release();await ready(returned);await original.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='success',null,{timeout:5000});
   assert.equal(await original.evaluate(()=>state.data.keys.length),2);
   assert.equal(f.model.requests.length,1);assert.equal(f.model.invoices,1);
   assert.deepEqual(f.errors,[]);
  }finally{release?.();await f.browser.close()}
 });
}

async function awaken(page,event='focus'){
 await page.evaluate(()=>{state.view='keys';render();sessionStorage.removeItem('freeman-payment');sessionStorage.removeItem('freeman-purchase-context')});
 await page.evaluate(event=>event==='visibilitychange'?document.dispatchEvent(new Event(event)):window.dispatchEvent(new Event(event)),event);
}
for(const [name,engine] of Object.entries({chromium,webkit})){
 for(const scenario of ['storage-only completion','focus without session marker','pageshow without session marker','online after network failure','unknown receipt','paid but provisioning pending','failed receipt','canceled receipt','uncertain operation without result','event burst with stale pending response','late old response after a new checkout','account change during status request','status timeout then recovery','explicit retry while status is in flight','staged operation without cached result','legacy operation result','topup isolation','proxy payment isolation']){
  test(name+': '+scenario,{timeout:30000},async()=>{
   const f=await fixture(engine);let release;
   try{
    const p=await f.page(),m=f.model,card={...params,method:'yookassa'};
    if(scenario==='storage-only completion'){
     const other=await f.page();assert.equal(await send(other,card),null);
     m.paid=true;await other.evaluate(()=>checkReturnedPayment(1));await ready(other);await ready(p);
    }else if(scenario==='uncertain operation without result'){
     m.lost='before';assert.ok(await send(p,card));await awaken(p);
     await p.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='error');
     const key=await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).key);
     await p.evaluate(()=>window.dispatchEvent(new Event('online')));await p.waitForTimeout(150);
     assert.equal(m.requests.length,1);assert.equal(m.invoices,0);
     assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).key),key);
    }else{
     assert.equal(await send(p,card),null);
     if(scenario==='focus without session marker'||scenario==='pageshow without session marker'){
      m.paid=true;await awaken(p,scenario.startsWith('pageshow')?'pageshow':'focus');await ready(p);
     }else if(scenario==='online after network failure'){
      m.failLookup=true;await awaken(p);await p.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='error');
      assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).confirmedAt||null),null);
      m.failLookup=false;m.failStatus=true;await p.evaluate(()=>window.dispatchEvent(new Event('online')));await p.waitForTimeout(150);
      m.failStatus=false;m.paid=true;await p.evaluate(()=>window.dispatchEvent(new Event('online')));await ready(p);
     }else if(scenario==='topup isolation'||scenario==='proxy payment isolation'){
      m.paid=true;await p.evaluate(()=>checkReturnedPayment(1));await ready(p);m.paid=false;
      await p.evaluate(async kind=>{await startExternalPayment({payment_id:99,redirect_url:'https://payments.example.test/99'},null,{kind})},scenario==='topup isolation'?'topup':'proxy');
      await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await p.waitForTimeout(100);
      assert.equal(Number(await p.evaluate(()=>state.purchase.paymentId)),99);assert.equal(await p.evaluate(()=>sessionStorage.getItem('freeman-payment')),'99');
     }else if(scenario==='staged operation without cached result'||scenario==='legacy operation result'){
      const key=m.requests[0].idempotency_key,op=m.operations.get('FO-47:'+key);
      if(scenario==='staged operation without cached result')op.result=null;else op.request=null;
      await awaken(p);await p.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='pending');
      assert.equal(!!await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).confirmedAt),false);
      m.paid=true;await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await ready(p);
     }else if(scenario==='status timeout then recovery'||scenario==='explicit retry while status is in flight'){
      if(scenario==='status timeout then recovery')await p.clock.install();
      const started=new Promise(resolve=>{m.paymentStarted=resolve});m.paymentGate=new Promise(resolve=>{release=resolve});m.gatePage=p;
      await awaken(p);await started;
      if(scenario==='status timeout then recovery'){
       await p.clock.fastForward(31000);await p.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='error');assert.match(await p.locator('.purchase-demo').innerText(),/повторно оплачивать/);release();
       m.paid=true;await p.evaluate(()=>window.dispatchEvent(new Event('online')));await ready(p);
      }else{
       m.paid=true;const retry=send(p,card);release();assert.equal(await retry,null);await ready(p);
      }
     }else if(['unknown receipt','paid but provisioning pending','failed receipt','canceled receipt'].includes(scenario)){
      m.paymentStatus={'unknown receipt':'unknown','paid but provisioning pending':'order_pending','failed receipt':'order_failed','canceled receipt':'order_canceled'}[scenario];m.confirmed=scenario==='paid but provisioning pending';await awaken(p);
      const expected=scenario==='unknown receipt'?'error':m.confirmed?'processing':'canceled';
      await p.waitForFunction(stage=>state.purchase?.stage===stage,expected);
      assert.equal(await p.evaluate(()=>state.data.keys.length),1);
      const terminal=expected==='canceled';assert.equal(!!await p.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')).confirmedAt),terminal);
      if(!terminal){m.paymentStatus=null;m.confirmed=false;m.paid=true;await p.evaluate(()=>window.dispatchEvent(new Event('focus')));await ready(p)}
     }else{
      const other=await f.page();await other.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='pending');
      const started=new Promise(resolve=>{m.paymentStarted=resolve});m.paymentGate=new Promise(resolve=>{release=resolve});m.gatePage=p;m.snapshotStatus=true;
      await awaken(p);await started;
      if(scenario==='account change during status request'){
       await p.evaluate(()=>{state.data.profile.freeman_id='FO-48';state.view='keys';render()});m.paid=true;release();await p.waitForTimeout(200);
       assert.equal(await p.evaluate(()=>state.data.profile.freeman_id),'FO-48');assert.notEqual(await p.evaluate(()=>state.purchase?.stage),'success');
      }else{
       m.paid=true;await other.evaluate(()=>checkReturnedPayment(1));await ready(other);
       if(scenario==='late old response after a new checkout'){
        m.paid=false;await other.waitForTimeout(20);assert.equal(await send(other,card),null);
       }
       await p.evaluate(()=>{for(let i=0;i<20;i++){window.dispatchEvent(new Event('focus'));window.dispatchEvent(new Event('pageshow'))}});
       release();
       if(scenario==='late old response after a new checkout'){
        await p.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='pending'&&state.purchase?.paymentId===2);
        assert.equal(m.invoices,2);assert.equal(m.requests.length,2);
       }else await ready(p);
       assert.equal(m.maxActiveReads,1);
      }
     }
    }
    if(scenario!=='uncertain operation without result'&&scenario!=='late old response after a new checkout'){assert.equal(m.invoices,1);assert.equal(m.requests.length,1)}
    assert.equal(m.charges,0);assert.deepEqual(f.errors,[]);
   }finally{release?.();await f.browser.close()}
  });
 }
}

for(const [name,engine] of Object.entries({chromium,webkit})){
 for(const delayed of ['profile','script'])test(name+': confirmation while initial '+delayed+' is loading',{timeout:30000},async()=>{
  const f=await fixture(engine);let release;
  try{
   const buyer=await f.page();assert.equal(await send(buyer,{...params,method:'yookassa'}),null);
   const started=new Promise(resolve=>{f.model[delayed+'Started']=resolve});f.model[delayed+'Gate']=new Promise(resolve=>{release=resolve});
   const opening=f.page('https://example.test/?audit_'+delayed+'=1');await started;
   f.model.paid=true;await buyer.evaluate(()=>checkReturnedPayment(1));await ready(buyer);release();
   const returning=await opening;await returning.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='success',null,{timeout:5000});
   assert.equal(await returning.evaluate(()=>state.data.keys.length),2);assert.equal(f.model.requests.length,1);assert.equal(f.model.invoices,1);assert.deepEqual(f.errors,[]);
  }finally{release?.();await f.browser.close()}
 });
}

for(const [name,engine] of Object.entries({chromium,webkit}))test(name+': older payment return keeps its own order',{timeout:30000},async()=>{
 const f=await fixture(engine);
 try{
  const buyer=await f.page();assert.equal(await send(buyer,{...params,method:'yookassa'}),null);f.model.paid=true;await buyer.evaluate(()=>checkReturnedPayment(1));await ready(buyer);
  f.model.paid=false;await buyer.waitForTimeout(20);assert.equal(await send(buyer,{...params,method:'yookassa'}),null);
  const pending=await buyer.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')));
  const returning=await f.context.newPage();returning.on('pageerror',e=>f.errors.push(e.message));
  await returning.route('**/api/web/payments/1',r=>r.fulfill({json:{id:1,status:'order_paid',key_id:2,subscription_id:12}}));
  await returning.goto('https://example.test/?payment=return&payment_id=1');
  await returning.waitForFunction(()=>typeof state!=='undefined'&&state.purchase?.stage==='success',null,{timeout:5000});
  assert.equal(Number(await returning.evaluate(()=>state.purchase.paymentId)),1);
  const after=await returning.evaluate(()=>JSON.parse(localStorage.getItem('freeman-checkout:FO-47')));assert.equal(after.key,pending.key);assert.equal(!!after.confirmedAt,false);
  assert.equal(f.model.invoices,2);assert.equal(f.model.requests.length,2);assert.deepEqual(f.errors,[]);
 }finally{await f.browser.close()}
});

for(const [name,engine] of Object.entries({chromium,webkit}))test(name+': confirmed history does not reopen on focus',{timeout:30000},async()=>{
 const f=await fixture(engine);
 try{
  const buyer=await f.page();f.model.paid=true;assert.equal(await send(buyer,{...params,method:'yookassa'}),null);await ready(buyer);
  const other=await f.page();await other.waitForFunction(()=>typeof state!=='undefined'&&state.view==='profile'&&state.data?.keys.length===2);
  await other.evaluate(()=>window.dispatchEvent(new Event('focus')));await other.waitForTimeout(200);
  assert.equal(await other.evaluate(()=>state.view),'profile');assert.equal(f.model.requests.length,1);assert.equal(f.model.invoices,1);assert.deepEqual(f.errors,[]);
 }finally{await f.browser.close()}
});
