/* Actual website scripts in Chromium/WebKit; publication HTTP responses are fixtures. */
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{chromium,webkit}=require('playwright');
const ROOT=process.env.FREEMAN_PUBLICATION_STATIC||path.resolve(__dirname,'../app/web/static');
for(const [engineName,engine] of Object.entries({chromium,webkit})){
 for(const base of ['https://public.example/','https://other.example:9443','https://public.example/cabinet/','https://public.example/a/b///']){
  for(const admin of [false,true])test(`${engineName}: ${admin?'admin':'customer'} publication links use ${base}`,{timeout:30000},async()=>{
   const browser=await engine.launch();try{
    const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'}),canonical=base.replace(/\/+$/,'')+'/?post=41',section=base.replace(/\/+$/,'')+'/?section=faq';
    const post={id:41,url:canonical,title:'Publication fixture',excerpt:'Existing text',plain:'Existing text',section:'news',status:'published',notify:true,notify_clients:true,version:7,published_at:'2026-10-14T00:00:00Z',created_at:'2026-10-13T00:00:00Z',updated_at:'2026-10-14T00:00:00Z',date:'2026-10-14T00:00:00Z',media:[],content:{ops:[{insert:'Existing text\n'}]},body:`<p>Existing text</p><p class="post-cta-row"><a class="post-cta" href="${section}" target="_blank" rel="noopener noreferrer">FAQ</a></p>`};
    const requests=[];await context.route('**/*',async route=>{
     const req=route.request(),u=new URL(req.url());requests.push({method:req.method(),url:req.url()});
     if(u.origin!=='https://internal.example')return route.fulfill({status:404,body:'External navigation blocked by fixture'});
     if(u.pathname==='/api/web/me')return route.fulfill({json:{profile:{is_admin:admin,name:'Fixture',freeman_id:'FO-47'},balance:0,summary:{keys:0,keys_active:0},keys:[],tariffs:[],payments:{},trial:{available:false},useful_links:[{id:9,label:'Publication fixture',target:canonical,icon:'book'}],news:[]}});
     if(u.pathname==='/api/web/posts')return route.fulfill({json:[post]});
     if(u.pathname==='/api/web/admin/publications')return route.fulfill({json:{items:[post],total:1,page:1,limit:10,counts:{all:1,published:1}}});
     if(u.pathname==='/api/web/posts/41')return route.fulfill({json:post});
     if(u.pathname==='/api/web/admin/reports')return route.fulfill({json:{stats:{revenue:0},daily:[],sales:[]}});
    if(u.pathname==='/api/web/admin/proxy')return route.fulfill({json:{stats:{},servers:[]}});
    if(u.pathname.startsWith('/api/'))return route.fulfill({json:u.pathname==='/api/account/notifications'?{items:[]}:u.pathname==='/api/web/admin/dashboard'?{revenue:0,sales:0,active_keys:0,users:0,daily:[]}: {}});
     const file=path.join(ROOT,u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
     return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'});
    });
    const p=await context.newPage(),errors=[];p.setDefaultTimeout(5000);p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.dismiss());
    await p.goto('https://internal.example/?post=41&next=https://evil.example');await p.locator('.post-reader h2').waitFor();assert.equal(await p.locator('.post-reader h2').innerText(),post.title);assert.equal(await p.locator('.post-reader .post-cta').getAttribute('href'),section);await p.locator('.post-reader [data-close-modal]').click();
    await p.evaluate(()=>refreshPostNotices());const stateURLs=await p.evaluate(()=>({news:state.data.news.map(n=>n.target),notices:state.extra.publicationNotices.map(n=>n.target)}));assert.deepEqual(stateURLs,{news:[canonical],notices:[canonical]});
    await p.locator('.notification-bell').first().click();await p.locator('#overlay-root [data-notice-open="0"]').click();await p.locator('.notice-detail [data-post-open="41"]').click();await p.locator('.post-reader h2').waitFor();await p.locator('.post-reader [data-close-modal]').click();
    if(admin){await p.evaluate(()=>{state.view='admin-posts';return loadPosts(true)});await p.locator('[data-pub-menu="41"]').click();await p.locator('[data-pub-action="preview"][data-id="41"]').click();await p.locator('.post-reader h2').waitFor();assert.equal(await p.locator('.post-reader .post-cta').getAttribute('href'),section)}
    await p.reload();await p.locator('.post-reader h2').waitFor();assert.equal(p.url(),'https://internal.example/?post=41&next=https://evil.example');assert.deepEqual(errors,[]);assert.ok(requests.every(r=>new URL(r.url).origin==='https://internal.example'));
   }finally{await browser.close()}
  });
 }
}
