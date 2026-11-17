/* B01 real entry/theme behavior. All HTTP is served from synthetic fixtures.
   Optional reference CSS enables a local before/after comparison; never writes B00. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,capture,NOW}=require('./helpers/ui-b00-fixture.cjs');
const ROOT=path.resolve(__dirname,'../app/web/static'),OUT=path.resolve(process.env.FREEMAN_B01_OUTPUT||'artifacts/ui-b01/themes');
const reference=process.env.FREEMAN_B01_REFERENCE_CSS&&path.resolve(process.env.FREEMAN_B01_REFERENCE_CSS);
const entries={cabinet:['/','index.html','.my-keys-page'],auth:['/','index.html','.unified-shell'],
 guest:['/buy','guest-purchase.html','#purchase'],account:['/account','account-management.html','[data-master]'],
 assistant:['/admin/assistant','freeman-assistant.html','.fa-welcome'],ai:['/admin/ai','freeman-ai.html','.ai-stats']};
const observations=[];let comparisons=0;
async function paintedCapture(page,directory,name){
 // WebKit can return unpainted native-select defaults on the first style read.
 // Establish a rendered frame for BOTH reference and candidate, then sample.
 await page.screenshot({animations:'disabled'});
 return capture(page,directory,name);
}
async function setup(browser,seed,{kind,width,theme}){
 const context=await browser.newContext({viewport:{width,height:900},isMobile:width<768,hasTouch:width<768,
  locale:'ru-RU',timezoneId:'Europe/Moscow',reducedMotion:'reduce',colorScheme:'dark',serviceWorkers:'block'});
 await context.addInitScript(pref=>{
  if(pref===null)localStorage.removeItem('freeman-theme');else localStorage.setItem('freeman-theme',pref);
  window.__themeProbe={changes:[],frames:[]};
  new MutationObserver(()=>{if(document.documentElement)window.__themeProbe.changes.push(document.documentElement.dataset.theme||null)}).observe(document,{subtree:true,attributes:true,attributeFilter:['data-theme']});
  let frames=0;const sample=()=>{if(document.body&&document.styleSheets.length){window.__themeProbe.frames.push({theme:document.documentElement.dataset.theme,bg:getComputedStyle(document.body).backgroundColor});frames++}if(frames<8)requestAnimationFrame(sample)};requestAnimationFrame(sample);
 },theme);
 const model=structuredClone(seed);let old=false;const errors=[],unknown=[],missing=[];
 const conversation={id:1,channel:'owner',mode:'ai',context_mode:'service',messages:[]};
 await context.route('**/*',async route=>{
  const u=new URL(route.request().url());if(u.origin!=='https://example.test')return route.abort();
  const json=data=>route.fulfill({json:data});
  if(u.pathname.startsWith('/api/')){
   if(u.pathname==='/api/web/me')return kind==='auth'?route.fulfill({status:401,json:{detail:'Требуется вход'}}):json(model);
   if(u.pathname==='/api/web/config')return json({});
   if(u.pathname==='/api/account/notifications')return json({items:[]});
   if(u.pathname==='/api/web/offers')return json({offers:[],notifications:[]});
   if(u.pathname==='/api/web/keys/1/connection')return json({value:'https://sub.example.test/fixture'});
   if(u.pathname==='/api/buy/identity')return json({recognized:false});
   if(u.pathname==='/api/buy/catalog')return json({available:true,bot_url:'https://t.me/Fixture',tariffs:model.tariffs.map(t=>({id:t.id,months:t.months,devices:t.device_limit,amount:t.price}))});
   if(u.pathname==='/api/account/management')return json({is_admin:false,master:true,payment_methods:[],subscriptions:[],tariffs:[]});
   if(u.pathname==='/api/web/admin/assistant'||u.pathname==='/api/web/admin/assistant/chats/1')return json(conversation);
   if(u.pathname==='/api/web/admin/assistant/chats')return json({items:[{id:1,title:'Тест темы',updated_at:NOW}]});
   if(u.pathname==='/api/web/admin/assistant/status')return json({model:'fixture',enabled:false,configured:false});
   if(u.pathname==='/api/web/admin/ai')return json({items:[],total:0,page:1,stats:{total:0,ai:0,human:0,failures:0,total_tokens:0},enabled:false,configured:false,owner_access:true});
   unknown.push(u.pathname);return route.fulfill({status:501,json:{detail:'Unmapped theme fixture'}});
  }
  let name=u.pathname.startsWith('/assets/')?path.basename(u.pathname):entries[kind][1];
  const file=path.join(old&&name.endsWith('.css')?reference:ROOT,name);
  if(!fs.existsSync(file)){missing.push(name);return route.fulfill({status:404,body:''})}
  // Simulate a stylesheet arriving after the saved preference script.
  if(name.endsWith('.css'))await new Promise(resolve=>setTimeout(resolve,80));
  return route.fulfill({body:fs.readFileSync(file),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.svg')?'image/svg+xml':name.endsWith('.png')?'image/png':name.endsWith('.webp')?'image/webp':'text/html'});
 });
 const page=await context.newPage();page.setDefaultTimeout(10000);await page.clock.setFixedTime(new Date(NOW));page.on('pageerror',e=>errors.push(e.message));
 async function load(useReference=false){
  old=useReference;await page.goto('https://example.test'+entries[kind][0]);
  if(kind==='cabinet'){await page.waitForFunction(()=>typeof state!=='undefined'&&!!state.data);await page.locator('[data-view="keys"]:visible').first().click()}
  await page.locator(entries[kind][2]).first().waitFor();
  if(kind==='assistant')await page.locator('#assistant-app:not([aria-busy])').waitFor();
 }
 return {context,page,model,errors,unknown,missing,load};
}
async function inspect(f,expected,canonical){
 const data=await f.page.evaluate(()=>({theme:document.documentElement.dataset.theme,saved:localStorage.getItem('freeman-theme'),
  probe:window.__themeProbe,canvas:getComputedStyle(document.documentElement).getPropertyValue('--fo-bg-page').trim(),
  text:getComputedStyle(document.documentElement).getPropertyValue('--fo-text-primary').trim(),overflow:document.documentElement.scrollWidth>innerWidth+1}));
 assert.equal(data.theme,expected);assert.ok(data.probe.frames.length,'No rendered theme sample');
 assert.ok(data.probe.changes.every(t=>t===expected),'Transient incorrect theme attribute');
 assert.ok(data.probe.frames.every(f=>f.theme===expected),'Incorrect theme on rendered frame');
 if(canonical){assert.equal(data.canvas,expected==='dark'?'#0d0e11':'#f6f7f9');assert.equal(data.text,expected==='dark'?'#f5f6f8':'#121826')}
 assert.equal(data.overflow,false);assert.deepEqual(f.errors,[]);assert.deepEqual(f.unknown,[]);assert.deepEqual(f.missing,[]);return data;
}
(async()=>{
 fs.mkdirSync(OUT,{recursive:true});
 for(const engine of [chromium,webkit]){
  const browser=await engine.launch();const source=await fixture(browser);const seed=source.model.account;await source.context.close();
  try{
   for(const kind of Object.keys(entries))for(const width of [390,1440])for(const theme of ['light','dark']){
    const name=`${engine.name()}-${kind}-${width}-${theme}`;
    if(process.env.FREEMAN_B01_CASE&&!name.includes(process.env.FREEMAN_B01_CASE))continue;
    const f=await setup(browser,seed,{kind,width,theme});console.log('THEME',name);
    try{
     if(reference){await f.load(true);await inspect(f,theme,false);await paintedCapture(f.page,path.join(OUT,'before'),name)}
     await f.load();const observation=await inspect(f,theme,true);await paintedCapture(f.page,path.join(OUT,'after'),name);
     if(reference){
      const before=JSON.parse(fs.readFileSync(path.join(OUT,'before',name+'.json'))),after=JSON.parse(fs.readFileSync(path.join(OUT,'after',name+'.json')));
      if(JSON.stringify(after)!==JSON.stringify(before)){
       const diagnostic=await f.page.evaluate(()=>{
        const keys=['--bg-page','--bg-surface','--bg-secondary','--text-primary','--border-subtle'];
        return {theme:document.documentElement.dataset.theme,root:Object.fromEntries(keys.map(k=>[k,getComputedStyle(document.documentElement).getPropertyValue(k)])),
         body:Object.fromEntries(keys.map(k=>[k,getComputedStyle(document.body).getPropertyValue(k)])),
         selects:[...document.querySelectorAll('select')].map(n=>({html:n.outerHTML,style:Object.fromEntries(['color','backgroundColor','border','appearance','fontFamily','colorScheme'].map(k=>[k,getComputedStyle(n)[k]])),ancestors:[n.parentElement,n.parentElement.parentElement,document.body].map(n=>[n.tagName,n.className,getComputedStyle(n).color])})),
         sheets:[...document.styleSheets].map(s=>({href:s.href,rules:s.cssRules.length}))};
       });fs.writeFileSync(path.join(OUT,name+'-diagnostic.json'),JSON.stringify(diagnostic,null,2));
      }
      assert.deepEqual(after,before,'Visible style/geometry drift: '+name);comparisons++;
     }
     // Use the actual theme buttons, retaining the current route and chat draft.
     if(['cabinet','assistant','ai'].includes(kind)){
      if(kind==='assistant'){
       await f.page.locator('textarea').fill('Черновик сохранён');
       if(width<768)await f.page.locator('[data-menu]').click();
       await f.page.locator('[data-panel="settings"]').click();await f.page.locator('button[data-theme]').waitFor();
      }
      await f.page.locator(kind==='assistant'?'button[data-theme]':'[data-theme-toggle]:visible').first().click();
      await f.page.waitForFunction(expected=>document.documentElement.dataset.theme===expected,theme==='dark'?'light':'dark');
      assert.equal(await f.page.evaluate(()=>localStorage.getItem('freeman-theme')),theme==='dark'?'light':'dark');
      if(kind==='assistant')assert.equal(await f.page.locator('textarea').inputValue(),'Черновик сохранён');
      if(kind==='cabinet')assert.equal(await f.page.evaluate(()=>state.view),'keys');
     }
     observations.push({name,...observation});
    }finally{await f.context.close()}
   }
   for(const kind of Object.keys(entries)){
    if(process.env.FREEMAN_B01_CASE)continue;
    const f=await setup(browser,seed,{kind,width:390,theme:null});try{await f.load();const d=await inspect(f,'light',true);assert.equal(d.saved,null);observations.push({name:`${engine.name()}-${kind}-no-preference-os-dark`,...d})}finally{await f.context.close()}
   }
   // Storage-denied fallback is tested in isolation; no wider app policy change.
   const p=await browser.newPage();await p.setContent('<html></html>');
   await p.evaluate(()=>{Storage.prototype.getItem=function(){throw Error('fixture storage unavailable')}});
   await p.addScriptTag({content:fs.readFileSync(path.join(ROOT,'theme-init.js'),'utf8')});assert.equal(await p.locator('html').getAttribute('data-theme'),'light');await p.close();
  }finally{await browser.close()}
 }
 fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify({coverage:'Real entry documents, mocked APIs; no live providers or physical iOS',reference:!!reference,comparisons,observations},null,2)+'\n');
 console.log(`PASS ${observations.length} entry/theme scenarios; ${comparisons} reference style comparisons; storage fallback in both engines`);
})().catch(e=>{console.error(e);process.exit(1)});
