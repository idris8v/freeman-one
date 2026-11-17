/* Checkout/account use their real scripts against local fixture APIs. */
const fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{
 let count=0;
 for(const engine of [chromium,webkit]){
  const browser=await engine.launch();
  for(const width of [320,390,1280])for(const theme of ['light','dark'])for(const view of ['buy','account']){
   const page=await browser.newPage({viewport:{width,height:844},reducedMotion:'reduce'});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(theme=>localStorage.setItem('freeman-theme',theme),theme);
   await page.route('**/*',route=>{
    const u=new URL(route.request().url());
    if(u.pathname.startsWith('/api/')){
     let data={};
     if(u.pathname==='/api/buy/catalog')data={available:true,bot_url:'https://t.me/example',tariffs:[{months:1,devices:1,amount:10000},{months:3,devices:1,amount:25000},{months:1,devices:3,amount:15000}]};
     if(u.pathname==='/api/account/management')data={is_admin:false,master:true,payment_methods:[],subscriptions:[],tariffs:[]};
     if(u.pathname==='/api/account/notifications')data={items:[]};
     return route.fulfill({json:data});
    }
    if(!u.pathname.startsWith('/assets/')&&!['/buy','/account'].includes(u.pathname))return route.abort();
    const file=path.join(process.cwd(),'app/web/static',u.pathname.startsWith('/assets/')?path.basename(u.pathname):view==='buy'?'guest-purchase.html':'account-management.html');
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':file.endsWith('.webp')?'image/webp':'text/html'});
   });
   await page.goto('https://example.test/'+view);
   await page.locator(view==='buy'?'#purchase':'[data-master]').waitFor();
   if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Overflow '+view+'/'+width);
   if(errors.length)throw Error(errors.join('\n'));
   fs.mkdirSync('artifacts/style-standalone',{recursive:true});
   await page.screenshot({path:`artifacts/style-standalone/${engine.name()}-${view}-${width}-${theme}.png`});count++;
   if(view==='buy'){
    await page.evaluate(()=>{document.querySelector('#content').innerHTML=PostPayment.render({status:'order_paid',key:'https://sub.example.test/fixture',freeman_id:'FO-10',password:'fixture-only',expires_at:'2027-11-10',devices:1,months:1,started:Date.now()})});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1)){
     await page.screenshot({path:'artifacts/style-standalone/receipt-overflow.png'});
     console.log(await page.evaluate(()=>[...document.body.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).map(n=>[n.tagName,n.className,n.getBoundingClientRect().width,n.getBoundingClientRect().right]).slice(0,20)));
     throw Error('Receipt overflow '+width);
    }
    await page.screenshot({path:`artifacts/style-standalone/${engine.name()}-receipt-${width}-${theme}.png`});count++;
   }
   await page.close();
  }
  await browser.close();
 }
 console.log(`PASS ${count} standalone layouts: Chromium/WebKit, light/dark, 320/390/1280`);
})().catch(error=>{console.error(error);process.exit(1)});
