/* Full page scripts + shared router + mutation observers, offline APIs only. */
const fs=require('fs'),path=require('path'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch();for(const theme of ['light','dark']){
 const p=await b.newPage({viewport:{width:390,height:844}});await p.addInitScript(t=>localStorage.setItem('freeman-theme',t),theme);const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',r=>{const u=new URL(r.request().url());if(u.pathname.startsWith('/api/')){let d={};
 if(u.pathname==='/api/web/me')d={profile:{is_admin:true,name:'Admin',freeman_id:'FRM-000001',avatar:'/assets/freeman-logo.svg',email:'admin@example.test',email_verified:true,telegram_id:123,password_login_available:true},balance:900,summary:{keys:0,keys_active:0},keys:[],tariffs:[],useful_links:[],news:[],notifications:[{id:1,title:'Old notice',text:'Legacy notification',created_at:new Date().toISOString()}]};
 else if(u.pathname==='/api/account/notifications')d={items:[]};else if(u.pathname==='/api/web/admin/reports')d={stats:{revenue:900},sales:[],daily:[]};else if(u.pathname==='/api/web/admin/dashboard')d={revenue:900,sales:1,active_keys:0,users:1,daily:[]};else if(u.pathname.includes('/admin/users'))d={items:[],total:0,page:1};else if(u.pathname==='/api/web/admin/catalog')d={vpn:[],proxy:[],codes:[],promotions:[],history:[],can_edit:true};else if(u.pathname==='/api/web/admin/proxy')d={servers:[],stats:{}};else if(u.pathname==='/api/web/config')d={};return r.fulfill({json:d})}
 const file=path.join(process.cwd(),'app/web/static',u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');return r.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'})});
 await p.goto('https://example.test');await p.locator('.screen:not([hidden]) .ma-home').waitFor();await p.waitForTimeout(500);
 const navSignature=()=>p.locator('.mobile-nav').evaluate(n=>{const tree=e=>e.nodeType===3?e.textContent:[e.tagName,[...e.attributes].map(a=>[a.name,a.value]).sort(),[...e.childNodes].map(tree)];return JSON.stringify(tree(n))});const nav=await navSignature();
 async function checkHome(){await p.waitForTimeout(200);const screen=p.locator('.screen:not([hidden])');if(await screen.locator('.ma-home').count()!==1||await screen.locator('[data-ai-profile] [data-ai-profile]').count()||await screen.locator('.profile-notices,.profile-stack,.profile-head,.service-grid').count())throw Error('Legacy or duplicate home');if(await screen.locator('[data-ai-open="/admin/assistant"]').count()!==1||await screen.locator('[data-ai-open="/admin/ai"]').count()!==1)throw Error('Duplicate AI entries');}
 await checkHome();
 for(const route of ['admin-catalog','admin-search','profile','home','admin-dashboard','admin-catalog','admin-dashboard']){
  await p.evaluate(v=>loadView(v),route);await p.waitForTimeout(400);
  if(['profile','home','admin-dashboard'].includes(route))await checkHome();else{if(await p.locator('.screen:not([hidden]) .ma-home,.screen:not([hidden]) [data-ai-profile],.screen:not([hidden]) .profile-notices').count())throw Error('Profile leaked into '+route);}
 }
 await p.locator('[data-shell-header] [data-profile-settings]').click();await p.waitForTimeout(400);if(await p.locator('.screen:not([hidden]) .ma-home').count()||await p.locator('.screen:not([hidden]) [data-ai-open]').count())throw Error('AI injected into Settings');await p.locator('.screen:not([hidden]) [data-admin-panel="home"]').click();await checkHome();
 if(await navSignature()!==nav)throw Error('Bottom nav changed');if(errors.length)throw Error(errors.join('\n'));
 await p.close();
}await b.close();console.log('PASS full-page admin routing and observer regression in both themes; aliases, repeated return, settings, no legacy blocks');})().catch(e=>{console.error(e);process.exit(1)});
