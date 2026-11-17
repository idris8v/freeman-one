/* Cross-domain adoption. Actual UI/CSS, isolated synthetic HTTP; no provider E2E. */
const assert=require('node:assert/strict'),fs=require('fs');const {chromium,webkit}=require('playwright');
const {fixture,navigate,capture}=require('./helpers/ui-b00-fixture.cjs');
const results=[];
async function check(p,root){
 const issues=await p.locator(root).evaluate(n=>{
  const errors=[];
  for(const c of n.querySelectorAll('button,input,select,textarea')){
   if(!c.getClientRects().length||c.closest('[hidden],.ql-toolbar')||c.type==='file'||c.closest('.unified-code'))continue;
   if(!c.matches('.fo-button,.fo-tab,.fo-row-action,.fo-input,.fo-select,.fo-check,.fo-radio,.fo-switch-input,.fo-nav-item,.fo-disclosure,.fo-choice,.fo-conversation,.fo-backdrop'))errors.push(c.outerHTML.slice(0,130));
   if(c.matches('input:not([type=checkbox],[type=radio]),select,textarea')&&parseFloat(getComputedStyle(c).fontSize)<16)errors.push('Zoom-prone '+c.outerHTML.slice(0,80));
  }
  if(document.documentElement.scrollWidth>innerWidth+1)errors.push('Window overflow');
  return errors;
 });assert.deepEqual(issues,[],root);
}
(async()=>{for(const [engine,type]of Object.entries({chromium,webkit})){const browser=await type.launch();for(const width of [390,1440])for(const theme of ['light','dark']){
 for(const admin of [false,true]){
  const f=await fixture(browser,{width,theme,admin}),{page:p,model}=f;
  for(const [view,selector]of [['profile','.profile-page'],[admin?'admin-catalog':'plans',admin?'.fo-catalog':'.fo-plans'],['support','.native-inbox']]){
   await navigate(p,view,selector);await check(p,'.screen:visible');
   if(view==='profile'&&admin){const value=await p.locator('.ma-balance strong:visible').allTextContents();if(value.length)assert.ok(value[0].includes('₽'));}
  }
  if(admin){
   const tabs=p.locator('.screen:visible .fo-tabs');await navigate(p,'admin-catalog','.fo-catalog');const buttons=p.locator('.catalog-tabs .fo-tab');await buttons.first().focus();await p.keyboard.press('End');assert.equal(await buttons.last().evaluate(n=>n===document.activeElement),true);await p.keyboard.press('Home');assert.equal(await buttons.first().evaluate(n=>n===document.activeElement),true);
   const style=await buttons.first().evaluate(n=>({shadow:getComputedStyle(n).boxShadow,border:getComputedStyle(n).borderColor}));assert.equal(style.shadow,'none');
   await capture(p,'artifacts/ui-global',`${engine}-${width}-${theme}-catalog`);
  }
  assert.deepEqual(model.errors,[]);await f.context.close();
 }
 for(const [entry,root]of [['/admin/assistant','#assistant-app'],['/admin/ai','#ai-app'],['/buy','#content'],['/account','#management']]){
  const f=await fixture(browser,{width,theme,admin:true,entry});const {page:p,model}=f;
  if(entry==='/admin/assistant')await p.locator('.fa-welcome').waitFor();
  else if(entry==='/admin/ai'){const nav=width>1100?'.ai-sidebar':'.ai-tabs';await p.locator(nav).waitFor();assert.equal(await p.locator('.ai-tabs').isVisible(),width<=1100);await p.locator(nav+' [data-tab=knowledge]').click();await p.locator('.ai-knowledge').waitFor();const sizes=await p.locator('.ai-knowledge [type=checkbox]').evaluateAll(nodes=>nodes.map(n=>n.getBoundingClientRect().width));assert.ok(sizes.length&&sizes.every(size=>size===16),JSON.stringify(sizes));}
  else if(entry==='/buy'){await p.locator('#purchase').waitFor();await check(p,root);await p.locator('[data-device="3"]').click();await check(p,root);
   await f.context.route('https://example.test/api/buy/draft',r=>r.fulfill({json:{order_id:7,amount:19000}}));
   await f.context.route('https://example.test/api/buy/7/email',r=>r.fulfill({json:{token:'fixture-only',destination:'fixture@example.test'}}));
   await p.evaluate(()=>document.addEventListener('focusin',e=>{if(e.target.id==='code')window.__otpFocusFont=parseFloat(getComputedStyle(e.target).fontSize)}));await p.locator('#email').fill('fixture@example.test');await p.locator('#purchase').click();await p.locator('#verify').waitFor();assert.ok(await p.evaluate(()=>window.__otpFocusFont>=16),'OTP must be styled before focus');await check(p,root);await p.evaluate(()=>document.querySelector('#content').innerHTML=PostPayment.render({status:'order_paid',key:'https://sub.example.test/fixture',login:'FO-47',months:1,devices:3,expires_at:'2026-11-15T09:00:00Z'}));await p.locator('.pp-success').waitFor();assert.equal(await p.locator('.pp-qr').count(),0,'Absent QR URL must not request /undefined');}
  else await p.locator('#management [data-master]').waitFor();
  await check(p,root);assert.deepEqual(model.errors,[]);await capture(p,'artifacts/ui-global',`${engine}-${width}-${theme}-${entry.replace(/\//g,'-')}`);await f.context.close();
 }
 // Real OTP controller: no SMTP, fixture request only.
 const auth=await fixture(browser,{width,theme});await auth.page.evaluate(()=>{state.data=null;FreemanAuth.show()});await check(auth.page,'#app');await auth.page.locator('[name=identity]').fill('fixture@example.test');await auth.page.locator('[data-unified-auth] [type=submit]').click();await auth.page.locator('[name=code]').waitFor();await check(auth.page,'#app');assert.deepEqual(auth.model.errors,[]);await auth.context.close();
 results.push({engine,width,theme});console.log('PASS global controls',engine,width,theme);
 }await browser.close()}fs.writeFileSync('artifacts/ui-global/results.json',JSON.stringify({coverage:'Real frontend, synthetic HTTP. Not production/provider/physical-device E2E.',cases:results},null,2));})().catch(e=>{console.error(e);process.exit(1)});
