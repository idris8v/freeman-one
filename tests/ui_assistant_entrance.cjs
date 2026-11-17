/* Real standalone entry point, delayed mocked API, no production UI access. */
const fs=require('fs'),path=require('path'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{const browser=await chromium.launch();
for(const width of [390,1280])for(const theme of ['light','dark'])for(const reducedMotion of ['no-preference','reduce']){
 const context=await browser.newContext({viewport:{width,height:844},reducedMotion});
 await context.addInitScript(theme=>{localStorage.setItem('freeman-theme',theme);window.entrances=[];const animate=Element.prototype.animate;Element.prototype.animate=function(...args){if(this.id==='assistant-app')window.entrances.push(args[1]);return animate.apply(this,args)}},theme);
 const page=await context.newPage();let release;const response=new Promise(resolve=>{release=resolve});
 await page.route('https://example.test/**',async route=>{const u=new URL(route.request().url());if(u.pathname.startsWith('/api/')){await response;return route.fulfill({json:u.pathname.endsWith('/chats')?{items:[]}:{id:1,messages:[]}})}
 const file=path.join(process.cwd(),'app/web/static',u.pathname.startsWith('/assets/')?path.basename(u.pathname):'freeman-assistant.html');return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'});
 });
 await page.goto('https://example.test/admin/assistant',{waitUntil:'domcontentloaded'});
 const first=await page.evaluate(()=>({ready:document.querySelector('#assistant-app').classList.contains('fa-route-ready'),busy:document.querySelector('#assistant-app').hasAttribute('aria-busy'),entrances:window.entrances}));
 if(!first.ready||!first.busy||first.entrances.length!==(reducedMotion==='reduce'?0:1))throw Error('Shell did not animate before delayed API: '+JSON.stringify(first));
 await page.waitForTimeout(350);if(await page.locator('#assistant-app').evaluate(el=>getComputedStyle(el).opacity)!=='1')throw Error('Shell remains hidden');
 release();await page.locator('#assistant-app:not([aria-busy])').waitFor();
 if(await page.evaluate(()=>window.entrances.length)!==first.entrances.length)throw Error('API completion replayed page entrance');
 await context.close();
}await browser.close();console.log('PASS first assistant entrance precedes API, no replay; mobile/desktop, light/dark, reduced motion');
})().catch(error=>{console.error(error);process.exit(1)});
