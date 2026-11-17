/* Full page scripts + shared router + mutation observers, offline APIs only. */
const fs=require('fs'),path=require('path'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch();for(const width of [320,390,900,1280])for(const theme of ['light','dark']){
 const p=await b.newPage({viewport:{width,height:844}});await p.addInitScript(t=>localStorage.setItem('freeman-theme',t),theme);const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',r=>{const u=new URL(r.request().url());if(u.pathname.startsWith('/api/')){let d={};
 if(u.pathname==='/api/web/me')d={profile:{is_admin:true,name:'Admin',freeman_id:'FRM-000001',avatar:'/assets/freeman-logo.svg',email:'admin@example.test',email_verified:true,telegram_id:123,password_login_available:true},balance:900,summary:{keys:0,keys_active:0},keys:[],tariffs:[],useful_links:[],news:[],notifications:[{id:1,title:'Old notice',text:'Legacy notification',created_at:new Date().toISOString()}]};
 else if(u.pathname==='/api/account/notifications')d={items:[]};else if(u.pathname==='/api/web/admin/reports')d={stats:{revenue:900},sales:[],daily:[]};else if(u.pathname==='/api/web/admin/dashboard')d={revenue:900,sales:1,active_keys:0,users:1,daily:[]};else if(u.pathname.includes('/admin/users'))d={items:[{id:17,name:'Ахмед',freeman_id:'FO-17',created_at:'2026-10-07T12:00:00+00:00',avatar:'/assets/freeman-logo.svg',active_keys:1,active_proxy:1,contacts:[{channel:'telegram',label:'@test'}],status:'active'},{id:18,avatar:'/api/test-avatar-missing',name:'Очень длинное имя клиента для проверки',freeman_id:'FO-18',active_keys:0,active_proxy:0,contacts:[],status:'inactive'}],total:2,page:1,limit:10};else if(u.pathname==='/api/web/admin/catalog')d={vpn:[],proxy:[],codes:[],promotions:[],history:[],can_edit:true};else if(u.pathname==='/api/web/admin/proxy')d={servers:[],stats:{}};else if(u.pathname==='/api/web/config')d={};return r.fulfill({json:d})}
 const file=path.join(process.cwd(),'app/web/static',u.pathname.startsWith('/assets/')?path.basename(u.pathname):'index.html');return r.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html'})});
 await p.goto('https://example.test');await p.waitForFunction(()=>typeof state!=='undefined'&&state.data?.profile?.is_admin);
 await p.evaluate(()=>loadView('admin-clients'));await p.locator('.screen:not([hidden]) .crm-compact').first().waitFor();await p.waitForTimeout(400);
 const rows=p.locator('.screen:not([hidden]) .crm-row');
 if(await rows.first().locator('.crm-freeman-id').textContent()!=='FO-17'||await rows.first().locator('.crm-registration').textContent()!=='07.10.26')throw Error('Identity or registration missing');
 await p.waitForFunction(()=>!document.querySelector('.screen:not([hidden]) [data-client-row="18"] .admin-list-avatar'));
 if(await rows.count()!==2)throw Error('Wrong client count');
 const geometry=await rows.evaluateAll(rows=>rows.map(row=>{const r=row.getBoundingClientRect(),children=[...row.children].map(n=>n.getBoundingClientRect()).filter(r=>r.width);return {height:r.height,spread:Math.max(...children.map(n=>n.top+n.height/2))-Math.min(...children.map(n=>n.top+n.height/2)),overflow:children.some(n=>n.right>r.right+1||n.left<r.left-1),telegram:row.querySelectorAll('.telegram-logo').length,centered:[...row.querySelectorAll('.crm-key-count,.crm-proxy-count')].every(n=>getComputedStyle(n).justifyContent==='center'),extra:row.querySelectorAll('.crm-contacts,.crm-activity,.crm-expiry,.crm-state,.crm-person small').length}}));
 if(geometry.some(x=>x.height>60||x.spread>2||x.overflow||x.extra||!x.centered)||geometry[0].telegram!==1||geometry[1].telegram!==0)throw Error(JSON.stringify({width,theme,geometry}));
 if(width<1100){
 const more=p.locator('.crm-list-actions>summary');await more.click();
 await p.locator('.crm-list-menu [data-crm-export]').waitFor();
 if(await p.locator('.crm-list-menu [data-crm-mobile-segments],.crm-list-menu [data-crm-campaign-history],.crm-list-menu [data-crm-select-page]').count()!==3)throw Error('List actions missing');
 await more.click();if(await p.locator('.crm-page-selection').isVisible())throw Error('Page checkbox leaked into toolbar');
 }
 if(width<1100){
 const table=await rows.evaluateAll(rows=>rows.map(row=>{const r=row.getBoundingClientRect(),button=row.querySelector('.crm-actions>button').getBoundingClientRect(),avatar=row.querySelector('.admin-avatar-circle').getBoundingClientRect();return {height:r.height,radius:getComputedStyle(row).borderRadius,margin:r.right-button.right,avatar:[avatar.width,avatar.height],divider:getComputedStyle(row).borderTopWidth}}));
 if(table.some(x=>x.height!==56||x.margin<8||x.avatar[0]!==24||x.avatar[1]!==24)||table[1].divider!=='1px')throw Error('Mobile table bounds '+JSON.stringify(table));
 await p.getByLabel('Сортировка клиентов').selectOption('name');await p.waitForFunction(()=>crmFilters().sort==='name');await p.waitForTimeout(200);
 if(await p.getByLabel('Сортировка клиентов').inputValue()!=='name')throw Error('Sort not retained');
 }
 if(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Page overflow');
 await p.evaluate(()=>{window.openedCustomer=null;openAdminCustomer=async id=>{window.openedCustomer=id}});
 await rows.first().locator('.crm-person').click();if(await p.evaluate(()=>window.openedCustomer)!==17)throw Error('Client not opened');
 await rows.first().locator('[data-crm-select]').check();if(!await rows.first().locator('[data-crm-select]').isChecked())throw Error('Selection broken');
 if(width<1100){
 const bulk=p.locator('.screen:not([hidden]) .crm-bulk');
 if(await p.locator('.screen:not([hidden]) [data-crm-offer]').count()!==1)throw Error('Duplicate offer action');
 const shape=await bulk.evaluate(n=>({height:n.getBoundingClientRect().height,position:getComputedStyle(n).position}));if(shape.height>50||shape.position==='fixed')throw Error('Oversized selection toolbar');
 const mark=await rows.first().locator('input[type=checkbox]').evaluate(n=>({size:n.getBoundingClientRect().width,radius:getComputedStyle(n).borderRadius,appearance:getComputedStyle(n).appearance}));if(mark.size!==16||mark.radius!=='5px'||mark.appearance!=='none')throw Error('Checkbox artwork '+JSON.stringify(mark));
 await bulk.locator('summary').click();await p.locator('.fo-popover [data-crm-message]').waitFor();await bulk.locator('summary').click();
 await p.evaluate(()=>{window.campaignAudience=null;crmCampaignOpen=async(kind,audience)=>{window.campaignAudience=audience}});
 await p.locator('.screen:not([hidden]) .crm-heading [data-crm-offer]').click();if((await p.evaluate(()=>window.campaignAudience?.user_ids))?.join()!=='17')throw Error('Top action lost selected audience');
 const arrow=await p.locator('.crm-sort-control select').evaluate(n=>({padding:parseFloat(getComputedStyle(n).paddingRight),image:getComputedStyle(n).backgroundImage}));if(arrow.padding<24||arrow.image==='none')throw Error('Sort arrow inset');
 }
 await rows.first().locator('[data-crm-menu]').click();await p.getByRole('menuitem',{name:'Открыть клиента',exact:true}).waitFor();
 if(errors.length)throw Error(errors.join('\n'));
 await p.evaluate(()=>document.querySelector('.brand-intro')?.remove());
 fs.mkdirSync('artifacts',{recursive:true});await p.screenshot({path:`artifacts/compact-clients-${width}-${theme}.png`});await p.close();
}await b.close();console.log('PASS compact client rows: single line, one Proxy icon, no extra metadata, selection/menu/card, mobile/desktop and themes');})().catch(e=>{console.error(e);process.exit(1)});
