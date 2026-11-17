const assert=require('node:assert/strict');const {chromium,webkit}=require('playwright');
const {fixture,capture}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{for(const [name,type]of Object.entries({chromium,webkit})){const browser=await type.launch();for(const width of [390,1440])for(const theme of ['light','dark']){
 const {page:p,context,model}=await fixture(browser,{admin:true,width,theme});let attempts=[],operation=null,lost=true;
 await context.route('**/api/web/admin/manual-sales/**',async r=>{const u=new URL(r.request().url());if(u.pathname.endsWith('/options'))return r.fulfill({json:{available:true,tariffs:model.account.tariffs.map(t=>({...t,days:t.months*30}))}});if(u.pathname.endsWith('/quote'))return r.fulfill({json:{tariff_id:11,amount:10000,days:30,devices:1,months:1}});if(u.pathname.includes('/operations/'))return r.fulfill(operation?{json:operation}:{status:404,json:{detail:'not registered'}});return r.abort()});
 await context.route('**/api/web/admin/keys/grant',async r=>{const b=r.request().postDataJSON();attempts.push(b);operation={request:{...b,user_id:17},snapshot:{total:10000},state:'prepared',result:null};if(lost){lost=false;return r.abort()}return r.fulfill({json:{user_id:17,key_id:30,issuance_kind:b.issuance_kind,payment_received:b.payment_received}})});
 await p.goto('https://example.test/');await p.waitForFunction(()=>typeof manualSalesOpen==='function'&&typeof state!=='undefined'&&state.data?.profile);
 await p.evaluate(()=>manualSalesOpen({identity:'FO-17'}));const form=p.locator('[data-manual-sale]');await form.waitFor();await p.waitForFunction(()=>ManualSales.quote?.amount===10000);
 assert.equal(await form.locator('[name=issuance_kind]').inputValue(),'manual_sale');assert.equal(await form.locator('[name=payment_received]').isChecked(),false);
 await form.locator('[name=payment_received]').check();await form.locator('[type=submit]').click();await form.locator('.login-form-error:not([hidden])').waitFor();assert.equal(attempts.length,1);
 assert.ok(await p.evaluate(()=>localStorage.getItem(manualStorage())));
 await p.reload();await p.waitForFunction(()=>typeof manualSalesOpen==='function'&&typeof state!=='undefined'&&state.data?.profile);await p.evaluate(()=>manualSalesOpen());
 const retry=p.locator('[data-manual-sale]');await retry.waitFor();assert.equal(await retry.locator('[name=issuance_kind]').isDisabled(),true);await capture(p,'artifacts/ui-consistency',`${name}-${width}-${theme}-manual-recovery`);
 await retry.locator('[type=submit]').click();await p.waitForFunction(()=>!localStorage.getItem(manualStorage()));assert.equal(attempts.length,2);assert.equal(attempts[0].request_id,attempts[1].request_id);assert.equal(attempts[1].expected_amount,10000);assert.equal(attempts[1].payment_received,true);
 await p.evaluate(()=>manualSalesOpen({identity:'FO-17'}));await p.waitForFunction(()=>ManualSales.quote);await p.locator('[name=issuance_kind]').selectOption('test');assert.equal(await p.locator('[data-manual-received]').isVisible(),false);assert.equal(await p.locator('[data-manual-custom]').isVisible(),true);
 assert.deepEqual(model.errors,[]);await context.close();console.log('PASS manual accounting/reload UI',name,width,theme);
 }await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
