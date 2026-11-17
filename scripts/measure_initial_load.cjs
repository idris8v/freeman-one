// Controlled loopback HTTP benchmark. Synthetic signed-out API; real static bytes.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),zlib=require('node:zlib');
const {chromium}=require('playwright');
const root=path.resolve(process.env.FREEMAN_BENCH_STATIC||'app/web/static');
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname.startsWith('/api/')){res.writeHead(url.pathname==='/api/web/me'?401:200,{'Content-Type':'application/json','Cache-Control':'no-store'});return res.end(url.pathname==='/api/web/me'?'{"detail":"Требуется вход"}':'{}');}
 const name=url.pathname==='/'?'index.html':url.pathname.startsWith('/assets/')?url.pathname.slice(8):null;
 if(!name||name.includes('..')||!fs.existsSync(path.join(root,name))){res.writeHead(404);return res.end();}
 const mime={'.js':'text/javascript','.html':'text/html','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webp':'image/webp'},body=fs.readFileSync(path.join(root,name));
 const compressed=/text|svg|javascript/.test(mime[path.extname(name)]||'')&&req.headers['accept-encoding']?.includes('gzip');
 res.writeHead(200,{'Content-Type':mime[path.extname(name)]||'application/octet-stream',...(compressed?{'Content-Encoding':'gzip'}:{}),'Cache-Control':'no-cache'});res.end(compressed?zlib.gzipSync(body):body);
});
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port,browser=await chromium.launch(),results=[];
 try{for(const mobile of [true,false])for(let run=1;run<=3;run++){
 const context=await browser.newContext({viewport:{width:mobile?390:1440,height:900}}),page=await context.newPage(),cdp=await context.newCDPSession(page),errors=[];
 page.on('pageerror',e=>errors.push(e.message));await cdp.send('Network.enable');await cdp.send('Network.setCacheDisabled',{cacheDisabled:true});
 await cdp.send('Network.emulateNetworkConditions',{offline:false,latency:mobile?150:20,downloadThroughput:(mobile?1.6:20)*1024*1024/8,uploadThroughput:1024*1024/8});await cdp.send('Emulation.setCPUThrottlingRate',{rate:mobile?4:1});
 await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
 await page.addInitScript(()=>{window.loadLCP=[];new PerformanceObserver(list=>window.loadLCP.push(...list.getEntries().map(x=>x.startTime))).observe({type:'largest-contentful-paint',buffered:true})});
 const start=Date.now();await page.goto(origin,{waitUntil:'domcontentloaded'});await page.locator('[name=identity]').waitFor();const interactive=Date.now()-start;await page.waitForTimeout(600);
 const result=await page.evaluate(()=>{const nav=performance.getEntriesByType('navigation')[0],resources=performance.getEntriesByType('resource'),scripts=resources.filter(x=>x.initiatorType==='script');return{ttfb_ms:nav.responseStart-nav.requestStart,lcp_ms:Math.max(0,...window.loadLCP),transferred_bytes:nav.transferSize+resources.reduce((s,x)=>s+x.transferSize,0),javascript_encoded_bytes:scripts.reduce((s,x)=>s+x.encodedBodySize,0),javascript_decoded_bytes:scripts.reduce((s,x)=>s+x.decodedBodySize,0),requests:resources.length+1,scripts:scripts.map(x=>new URL(x.name).pathname)}});
 results.push({profile:mobile?'mobile-1.6Mbps-150ms-CPU4x':'desktop-20Mbps-20ms',run,interactive_ms:interactive,...result,errors});await context.close();
 }fs.writeFileSync(process.argv[2],JSON.stringify({scope:'Loopback real HTTP/gzip static assets; synthetic anonymous API; cold browser cache; Chromium. Excludes production DNS/TLS/server load and is not field CWV.',results},null,2)+'\n');console.log(JSON.stringify(results.map(({scripts,...x})=>x),null,2));}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
