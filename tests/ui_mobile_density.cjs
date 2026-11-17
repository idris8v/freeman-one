/* Density across all entry points; real CSS and controls, synthetic API only. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,navigate,capture,clean}=require('./helpers/ui-b00-fixture.cjs');
const out='artifacts/mobile-density',observations=[];
async function inspect(f,name){
 const data=await f.page.evaluate(()=>({body:parseFloat(getComputedStyle(document.body).fontSize),
  overflow:document.documentElement.scrollWidth>innerWidth+1,
  inputFonts:[...document.querySelectorAll('input:not([type=checkbox],[type=radio],[type=file]),select,textarea')].filter(n=>n.getClientRects().length).map(n=>parseFloat(getComputedStyle(n).fontSize)),
  buttons:[...document.querySelectorAll('.fo-button:not(.fo-nav-item):not(.fo-tab)')].filter(n=>n.getClientRects().length).map(n=>{const s=getComputedStyle(n),hit=getComputedStyle(n,'::after');return{height:n.getBoundingClientRect().height,hit:hit.content==='""'?Math.abs(parseFloat(hit.top)||0)+Math.abs(parseFloat(hit.bottom)||0):0,font:parseFloat(s.fontSize)}})}));
 assert.equal(data.overflow,false,name);assert.ok(data.body>=14&&data.body<=15,name+': body');assert.ok(data.inputFonts.every(x=>x>=16),name+': iOS input size');
 assert.ok(data.buttons.every(x=>x.height+x.hit>=43.9),name+': touch target');clean(f.model);
 observations.push({name,...data});await capture(f.page,out,name);
}
(async()=>{for(const engine of [chromium,webkit]){const browser=await engine.launch();try{for(const width of [320,390])for(const theme of ['light','dark']){
 const prefix=engine.name()+'-'+width+'-'+theme;
 const customer=await fixture(browser,{width,theme});try{
  await navigate(customer.page,'keys','.my-keys-page');await customer.page.locator('[data-key-toggle="1"]').click();await inspect(customer,prefix+'-keys');
  const hit=await customer.page.locator('[data-key-rename="1"]').boundingBox();
  await customer.page.mouse.click(hit.x+hit.width/2,hit.y-2);await customer.page.locator('#rename-key-input').waitFor();await customer.page.keyboard.press('Escape');
  await navigate(customer.page,'plans','.device-options');await inspect(customer,prefix+'-tariffs');
  await customer.page.evaluate(()=>FreemanAuth.show());await inspect(customer,prefix+'-login');
 }finally{await customer.context.close()}
 const admin=await fixture(browser,{width,theme,admin:true});try{
  await navigate(admin.page,'admin-clients','.crm-page');await inspect(admin,prefix+'-clients');
  await navigate(admin.page,'admin-catalog','.catalog-page');await admin.page.locator('[data-catalog-edit="vpn:11"]').click();await inspect(admin,prefix+'-tariff-editor');
 }finally{await admin.context.close()}
 for(const [entry,selector,label]of [['/buy','#purchase','guest'],['/account','[data-master]','account'],['/admin/assistant','.fa-welcome','assistant'],['/admin/ai','.ai-stats','ai']]){
  const f=await fixture(browser,{width,theme,admin:true,entry});try{await f.page.locator(selector).waitFor();await inspect(f,prefix+'-'+label)}finally{await f.context.close()}
 }
 }}finally{await browser.close()}}
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({scope:'Browser engines, not physical iPhone; real UI, mocked APIs',cases:observations},null,2));console.log('PASS '+observations.length+' mobile density states across five bundles, both themes/engines; 16px input and touch-area guards');
})().catch(e=>{console.error(e);process.exitCode=1});
