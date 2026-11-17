/* Layout contracts for sections missing from the older UI suite. Every request
   is fulfilled from fixtures/local files. No live account or business writes. */
const fs=require('node:fs'),path=require('node:path');
const {chromium,webkit}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
const staticDir=path.join(process.cwd(),'app/web/static');
const account={profile:{is_admin:true,name:'Admin',freeman_id:'FO-1',avatar:'/assets/freeman-logo.svg',email:'admin@example.test',email_verified:true},balance:90000,summary:{keys:1,keys_active:1},keys:[],tariffs:[],useful_links:[],news:[],notifications:[],payments:{owner_free:false}};
(async()=>{
 let cases=0;
 for(const engine of [chromium,webkit]){
  const browser=await engine.launch();
  for(const width of [320,390,1280])for(const theme of ['light','dark']){
   const page=await browser.newPage({viewport:{width,height:844},reducedMotion:'reduce'});
   await page.addInitScript(theme=>localStorage.setItem('freeman-theme',theme),theme);
   await page.route('**/*',route=>{
    const u=new URL(route.request().url());
    if(u.pathname.startsWith('/api/'))return route.fulfill({json:u.pathname==='/api/web/me'?account:{items:[],servers:[],stats:{},notifications:[]}});
    if(!u.pathname.startsWith('/assets/')&&u.pathname!=='/')return route.abort();
    const file=path.join(staticDir,u.pathname==='/'?'index.html':path.basename(u.pathname));
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':file.endsWith('.png')?'image/png':file.endsWith('.webp')?'image/webp':'text/html'});
   });
   await page.goto('https://example.test');await page.waitForFunction(()=>typeof state!=='undefined'&&state.data?.profile?.is_admin);
   await page.waitForTimeout(600);
   await page.evaluate(()=>{
    // Render functions are exercised with fixtures; background account refresh
    // must not replace the fixture DOM during the visual assertions.
    render=()=>{};
    state.data.tariffs=[{id:1,name:'1 месяц',months:1,price:10000,device_limit:1},{id:2,name:'3 месяца',months:3,price:25000,device_limit:1},{id:3,name:'1 месяц',months:1,price:15000,device_limit:3}];
    state.data.keys=[{id:1,subscription_id:1,name:'Мой ключ',duration_months:1,device_limit:3,status:'active',expires_at:'2027-11-10T12:00:00Z',display_key:'https://sub.example.test/fixture',connection_loaded:true,devices:[{id:1,name:'iPhone',type:'phone'}]}];state.expanded=1;
    state.extra.referrals={link:'https://example.test/r/fixture',invited:12,buyers:4,earned:150000,available_balance:90000,levels:[],history:[{status:'reward_credited',reward_amount:5000,masked_user_label:'Клиент FO-2',occurred_at:'2026-10-10T12:00:00Z'}],chart:[],tier:1};state.referralHistoryVisible=10;
    Payments.data={start:'2026-10-01',end:'2026-10-12',total:1,stats:{turnover:10000,successful:1,refunds:0,issues:0,turnover_growth:null,successful_growth:null,refunds_growth:null,issues_growth:null,total:1,success_count:1,topups:0,pending:0,errors:0},items:[{id:'P1',number:1,user_id:2,owner:'Клиент',freeman_id:'FO-2',operation_label:'Покупка подписки',tariff:'1 месяц',record_kind:'order',amount:10000,currency:'RUB',method:'Карта',status:'success',date:'2026-10-10T12:00:00Z'}],notes:[]};
    Pub.data={total:1,items:[{id:1,title:'Новости сервиса',excerpt:'Описание публикации',section:'news',status:'published',views:12,published_at:'2026-10-10T12:00:00Z',created_at:'2026-10-10T12:00:00Z'}],counts:{all:1,published:1}};
    Pub.draft={title:'Новости сервиса',section:'news',status:'draft',media:[],buttons:[],notify:false,pinned:false,body_html:'<p>Описание публикации</p>'};
    Catalog.data={can_edit:true,vpn:[{id:1,name:'1 месяц',months:1,price:10000,device_limit:1,enabled:true,traffic_limit_gb:0}],proxy:[],promotions:[],codes:[],history:[]};
    px.data={total:1,servers:[{id:1,name:'Основной'}],items:[{id:1,name:'Основной',host:'example.test',city:'Москва',country:'RU',capacity:2,max_accounts:100,clients:2,traffic:1024,online:2,health:'online'}],summary:{servers:1,healthy:1,proxies:2,active:2,clients:2,traffic:1024,sales:1,revenue:10000,online:2},recent:[],series:[{day:'2026-10-10',traffic:1024}]};
    state.extra.report={start:'2026-10-01',end:'2026-10-12',stats:{sales:1,revenue:10000,new_customers:1,average:10000},renewals:0,active_keys:1,expiring:0,tariffs:[],statuses:{active:1},customers:[],sources:[],series:[],items:[],sales:[],payments:[],finance:{},proxy:{},devices:{},breakdown:[],channels:{},previous_start:'2026-09-01',previous_end:'2026-09-30',page:1,limit:10,total:1};
    window.styleFixture=kind=>{
     state.view='style-fixture';document.body.classList.toggle('admin-account',!['plans','keys','referrals'].includes(kind));document.body.classList.toggle('client-account',['plans','keys','referrals'].includes(kind));
     let html;
     if(kind==='plans')html=plansPage(state.data);
     if(kind==='keys')html=keysPage(state.data);
     if(kind==='referrals')html=referralPage();
     if(kind==='payments')html=paymentsPage();
     if(kind==='reports')html=reportsPage();
     if(kind==='publications')html=postsPage(true)+pubEditorHTML();
     if(kind==='catalog')html=catalogPage();
     if(kind==='proxy')html='<div class="px-console">'+pxSummary()+pxToolbar()+pxServers()+'</div>';
     document.querySelector('.brand-intro')?.remove();
     document.getElementById('app').innerHTML='<div class="app-shell"><aside class="sidebar"></aside><main class="profile-content"><div class="stage"><section class="screen" data-screen="'+kind+'"><div class="screen__inner stagger">'+FreemanUI.markup(html)+'</div></section></div></main></div>';
    };
   });
   for(const kind of ['plans','keys','referrals','payments','reports','publications','catalog','proxy']){
    console.log(engine.name(),width,theme,kind);
    await page.evaluate(kind=>window.styleFixture(kind),kind);
    await page.waitForTimeout(50);
    const result=await page.locator('.screen__inner').evaluate(n=>({width:n.clientWidth,scroll:n.scrollWidth,visible:n.getBoundingClientRect().width>0}));
    if(!result.visible)throw Error('Invisible '+kind);
    // Wide data tables may scroll inside their own shell, never the page.
    if(result.scroll>result.width+2)throw Error(`${engine.name()} ${kind} overflow ${width}/${theme}: ${JSON.stringify(result)}`);
    fs.mkdirSync('artifacts/style-sections',{recursive:true});
    await page.screenshot({path:`artifacts/style-sections/${engine.name()}-${kind}-${width}-${theme}.png`});cases++;
   }
   await page.close();
  }
  await browser.close();
 }
 console.log(`PASS ${cases} section layouts: Chromium/WebKit, 320/390/1280, light/dark`);
})().catch(error=>{console.error(error);process.exit(1)});
