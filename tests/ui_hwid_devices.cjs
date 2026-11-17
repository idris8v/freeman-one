/* I06: real cabinet device modal; API and account are isolated fixtures. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium,webkit}=require('playwright');
const ROOT=path.resolve(__dirname,'../app/web/static');
for(const [name,engine] of Object.entries({chromium,webkit})){
 for(const scenario of ['cached failure','unknown failure','confirmed empty']){
  test(`${name}: HWID ${scenario}`,{timeout:30000},async()=>{
   const browser=await engine.launch();
   try{
    const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
    const devices=scenario==='cached failure'?[{id:7,name:'iPhone',status:'active',registered_at:'2026-10-01T00:00:00Z',can_unlink:true}]:[];
    const count=scenario==='unknown failure'?null:devices.length;
    await context.route('**/*',async route=>{
     const u=new URL(route.request().url());
     if(u.origin!=='https://example.test')return route.fulfill({status:404,body:''});
     if(u.pathname==='/api/web/keys/1/devices')return route.fulfill({json:{key:{id:1,name:'Test'},devices,device_limit:3,actions:{},device_sync:{count,status:scenario==='confirmed empty'?'confirmed':'unavailable'}}});
     if(u.pathname==='/api/web/me')return route.fulfill({json:{profile:{is_admin:false,name:'Fixture',freeman_id:'FO-47'},balance:0,summary:{keys:1,keys_active:1},keys:[{id:1,name:'Test',status:'active',expires_at:'2028-01-01T00:00:00Z',device_limit:3,devices:[{id:99,name:'Previously known'}]}],tariffs:[],payments:{},trial:{available:false},useful_links:[],news:[]}});
     if(u.pathname.startsWith('/api/'))return route.fulfill({json:u.pathname==='/api/account/notifications'?{items:[]}: {}});
     const file=path.join(ROOT,u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');
     if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
     return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'});
    });
    const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto('https://example.test');
    await page.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);
    await page.evaluate(()=>keysDevices(1));
    const summary=await page.locator('.key-device-summary').innerText();
    if(scenario==='unknown failure'){
     assert.match(summary,/— из 3/);
     assert.equal(await page.evaluate(()=>state.data.keys[0].devices[0].id),99);
     assert.equal(await page.getByText('Устройств пока нет.',{exact:false}).count(),0);
    }else assert.match(summary,new RegExp(`${count} из 3`));
    assert.equal(await page.locator('.keys-device-modal [role="status"]').count(),scenario==='confirmed empty'?0:1);
    assert.deepEqual(errors,[]);
   }finally{await browser.close()}
  });
 }
}
