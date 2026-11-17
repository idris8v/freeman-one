/* Full page scripts + shared router + mutation observers, offline APIs only. */
const fs=require('fs'),path=require('path'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch();for(const width of [320,390,900,1280])for(const theme of ['light','dark']){
 const p=await b.newPage({viewport:{width,height:844}});await p.addInitScript(t=>localStorage.setItem('freeman-theme',t),theme);const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',r=>{const u=new URL(r.request().url());if(u.pathname.startsWith('/api/')){let d={};
 if(u.pathname==='/api/web/me')d={profile:{is_admin:true,name:'Admin',freeman_id:'FRM-000001',avatar:'/assets/freeman-logo.svg',email:'admin@example.test',email_verified:true,telegram_id:123,password_login_available:true},balance:900,summary:{keys:0,keys_active:0},keys:[],tariffs:[],useful_links:[],news:[],notifications:[{id:1,title:'Old notice',text:'Legacy notification',created_at:new Date().toISOString()}]};
 else if(u.pathname==='/api/account/notifications')d={items:[]};else if(u.pathname==='/api/web/admin/reports')d={stats:{revenue:900},sales:[],daily:[]};else if(u.pathname==='/api/web/admin/dashboard')d={revenue:900,sales:1,active_keys:0,users:1,daily:[]};else if(/^\/api\/web\/admin\/keys\/\d+$/.test(u.pathname))d={key:{id:30,name:'360 дней',traffic:'2 ГБ'},devices:[{id:1,name:'iPhone',status:'active'},{id:2,name:'MacBook',status:'active'}],device_limit:3,actions:{}};else if(u.pathname.startsWith('/api/web/admin/keys'))d={items:[{id:30,user_id:3,owner:"Очень длинное имя клиента",freeman_id:"FRM-000003",title:"Подписка",status:"active",expires_at:"2027-11-10",device_count:1,device_limit:3,traffic:"2 ГБ",connection_url:"https://sub.example.test/key",months:1},{id:29,user_id:1,owner:"Admin",freeman_id:"FRM-000001",status:"active",expires_at:"2027-11-10",device_count:0,device_limit:1,months:1}],total:2,page:1,limit:10,summary:{},tariffs:[],servers:[],device_limits:[]};else if(u.pathname.includes('/admin/users'))d={items:[{id:17,name:'Ахмед',freeman_id:'FO-17',created_at:'2026-10-07T12:00:00+00:00',avatar:'/assets/freeman-logo.svg',active_keys:1,active_proxy:1,contacts:[{channel:'telegram',label:'@test'}],status:'active'},{id:18,avatar:'/api/test-avatar-missing',name:'Очень длинное имя клиента для проверки',freeman_id:'FO-18',active_keys:0,active_proxy:0,contacts:[],status:'inactive'}],total:2,page:1,limit:10};else if(u.pathname==='/api/web/admin/catalog')d={vpn:[],proxy:[],codes:[],promotions:[],history:[],can_edit:true};else if(u.pathname==='/api/web/admin/proxy')d={servers:[],stats:{}};else if(u.pathname==='/api/web/config')d={};return r.fulfill({json:d})}
 const file=path.join(process.cwd(),'app/web/static',u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');return r.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'})});
 await p.goto('https://example.test');await p.waitForFunction(()=>typeof state!=='undefined'&&state.data?.profile?.is_admin);await p.evaluate(()=>loadView('admin-keys'));await p.locator('.screen:not([hidden]) .fo-key-registry').waitFor();
 const root=p.locator('.screen:not([hidden]) .fo-key-registry');
 if(await root.locator('.keys-status-tabs').count()||await root.locator('.keys-heading [data-reset-all-keys]').count())throw Error('Duplicate controls');
 await root.getByLabel('Статус',{exact:true}).selectOption('active');await p.waitForFunction(()=>state.extra['admin-keysFilter'].status==='active');await p.waitForTimeout(200);
 await root.locator('.fo-registry-toolbar>details>summary').click();await p.getByRole('menuitem',{name:'Сбросить устройства всех ключей'}).waitFor();await p.keyboard.press('Escape');
 if(await root.locator('[data-admin-list-limit]').count()!==1)throw Error('Page limit duplicated or lost');
 await root.locator('[data-keys-filter]').click();await root.locator('[aria-label="Фильтры подписок"]').waitFor();
 if(await root.locator('[data-keys-filter-field="status"]').count()!==1)throw Error('Status duplicated');
 if(await root.locator('.fo-key-client small').first().innerText()!=='FO-3')throw Error('Identity not normalized');
 const rows=root.locator('.fo-key-row');if(await rows.count()!==2)throw Error('Missing rows');
 if(await root.locator('.fo-key-description .fo-status').count()!==2)throw Error('Status missing');
 if(!await root.locator('.fo-key-url').isVisible())throw Error('Subscription URL hidden');
 if(await root.locator('.fo-key-row [data-admin-key-operation="extend"]:visible').count())throw Error('Renew not moved');
 await rows.first().locator('.fo-key-capacity button').click();await p.getByRole('dialog',{name:'Устройства подписки'}).waitFor();
 if(await p.locator('.key-device-detail').count()!==2)throw Error('Device list lost');
 await p.getByRole('dialog',{name:'Устройства подписки'}).locator('header [data-close-modal]').click();
 if(!await rows.first().locator('.fo-key-capacity button').innerText().then(t=>t.includes('2 / 3')))throw Error('Count not refreshed');
 const bounds=await rows.evaluateAll(ns=>ns.every(n=>{const r=n.getBoundingClientRect();return [...n.children].every(c=>{const b=c.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1})}));if(!bounds)throw Error('Row overflow '+width);
 if(width<1100&&await rows.first().evaluate(n=>n.getBoundingClientRect().height)>145){await p.screenshot({path:'artifacts/keys-row-height.png'});throw Error('Rows too tall '+JSON.stringify(await rows.first().evaluate(n=>[...n.children].map(c=>({name:c.className,height:c.getBoundingClientRect().height,width:c.getBoundingClientRect().width})))))}
 await rows.first().locator('.fo-key-actions>summary').click();
 const menu=p.getByRole('menu');await menu.waitFor({state:'visible'});
 if(await menu.locator('[data-admin-key-operation="delete"]').count()!==1)throw Error('Delete action lost');
 if(await menu.evaluate(n=>getComputedStyle(n).display)!=='grid')throw Error('Unstyled row menu');
 await rows.first().locator('.fo-key-actions>summary').click();
 const dimensions=await root.locator('.fo-registry-toolbar').evaluate(n=>{const r=n.getBoundingClientRect();return [...n.children].every(x=>{const b=x.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1})});if(!dimensions)throw Error('Toolbar overflow '+width);
 await p.evaluate(()=>document.querySelector('.brand-intro')?.remove());fs.mkdirSync('artifacts',{recursive:true});await p.screenshot({path:`artifacts/keys-controls-${width}-${theme}.png`});if(errors.length)throw Error(errors.join('\n'));await p.close();
}await b.close();console.log('PASS key controls: statuses, filters, More, no duplicates; mobile/desktop themes');})().catch(e=>{console.error(e);process.exit(1)});
