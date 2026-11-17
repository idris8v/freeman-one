/* Guest tariff choices use stable IDs; all requests are local fixtures. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium,webkit}=require('playwright');
const root=path.resolve(__dirname,'../app/web/static');
for(const [name,engine] of Object.entries({chromium,webkit})){
 test(name+': guest selects exact tariff among equal durations and devices',{timeout:30000},async()=>{
  const browser=await engine.launch(),page=await browser.newPage(),drafts=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url()),json=data=>route.fulfill({json:data});
   if(u.pathname==='/api/buy/catalog')return json({available:true,bot_url:'https://t.me/Fixture',tariffs:[{id:1,months:1,devices:1,amount:10000},{id:7,months:1,devices:1,amount:15000},{id:8,months:1,devices:5,amount:22000}]});
   if(u.pathname==='/api/buy/identity')return json({recognized:false});
   if(u.pathname==='/api/buy/draft'){drafts.push(route.request().postDataJSON());return json({order_id:1,amount:15000})}
   if(u.pathname==='/api/buy/1/pay')return route.fulfill({status:503,json:{detail:'Fixture: stop before external payment'}});
   if(u.pathname==='/buy')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'guest-purchase.html'))});
   const file=path.join(root,path.basename(u.pathname));
   if(fs.existsSync(file))return route.fulfill({contentType:u.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(file)});
   return json({});
  });
  try{
   await page.goto('https://example.test/buy');
   await page.locator('[data-tariff="7"]').click();
   assert.equal(await page.locator('[data-tariff="7"]').getAttribute('aria-checked'),'true');
   assert.equal(await page.locator('[data-tariff="1"]').getAttribute('aria-checked'),'false');
   await page.locator('#purchase').click();
   await page.waitForFunction(()=>document.querySelector('#error')?.textContent.includes('Fixture'));
   assert.equal(drafts.length,1);
   assert.equal(drafts[0].tariff_id,7);
   assert.equal(drafts[0].months,1);
   assert.deepEqual(errors,[]);
  }finally{await browser.close()}
 });
}
