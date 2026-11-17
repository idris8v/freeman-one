/* Mocked UI acceptance only; never contacts production or external providers. */
const {chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),path=require('path');
(async()=>{
 const browser=await chromium.launch({headless:true});let cases=0;
 for(const width of [320,390,768,1280])for(const theme of ['light','dark'])for(const admin of [true]){
  const context=await browser.newContext({viewport:{width,height:844}});await context.addInitScript(()=>{window.SpeechRecognition=class{start(){this.onresult?.({results:[[{transcript:'Голосовой тест'}]]});this.onend?.()}stop(){this.onend?.()}}});await context.addInitScript(t=>localStorage.setItem('freeman-theme',t),theme);const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const conv={id:1,mode:'ai',messages:[{id:1,role:'client',text:'Помогите подключить VPN',actions:[]},{id:2,role:'assistant',text:'Подскажите, какое у вас устройство?',actions:[]}]};
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());let data;
   if(u.pathname.startsWith('/api/')){
    if(u.pathname==='/api/web/admin/ai')data={items:[{id:1,name:'Тестовый клиент',channel:'website',mode:'ai',preview:'Помогите подключить VPN'}],total:1,page:1,stats:{total:1,ai:1,human:0,failures:0,total_tokens:50},enabled:false,configured:false,whatsapp_configured:false,owner_access:true};
    else if(u.pathname==='/api/web/admin/ai/assistant')data={id:2,channel:'owner',mode:'ai',messages:[{id:10,role:'assistant',text:'Предлагаю дополнить базу знаний.',actions:[{type:'knowledge_proposal',title:'Подключение',text:'Проверьте доступ к сети.'}]}]};
    else if(u.pathname==='/api/web/admin/ai/assistant/tasks')data={items:[{id:1,prompt:'Подготовить отчёт по продажам',due_at:'2026-10-11T07:00:00Z',status:'scheduled'}]};
    else if(u.pathname.endsWith('/cancel'))data={status:'canceled'};
    else if(u.pathname==='/api/web/admin/ai/assistant/confirm')data={saved:true,id:1};
    else if(u.pathname.endsWith('/knowledge')||u.pathname.endsWith('/events'))data={items:[]};
    else if(u.pathname.endsWith('/messages')){conv.messages.push({id:conv.messages.length+1,role:'client',text:'Проверка отправки',actions:[]});data={id:3,mode:'ai'};}
    else data=conv;
    return route.fulfill({json:data});
   }
   const file=u.pathname.startsWith('/assets/')?path.join(process.cwd(),'app/web/static',path.basename(u.pathname)):path.join(process.cwd(),'app/web/static/freeman-ai.html');
   return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':file.endsWith('.svg')?'image/svg+xml':file.endsWith('.png')?'image/png':'text/html'});
  });
  await page.goto('https://example.test/'+(admin?'admin/ai':'ai'));
  if(admin){
   const nav=width>1100?'.ai-sidebar':'.ai-tabs';
   for(const name of ['knowledge','settings','events','inbox']){
    await page.locator(`${nav} [data-tab="${name}"]`).click();
    await page.locator(name==='assistant'?'.ai-owner-chat':name==='knowledge'?'.ai-knowledge':name==='settings'?'.ai-settings':name==='events'?'.ai-events':'.ai-stats').waitFor();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error(`Overflow ${width} ${theme} ${name}`);
    if(name==='assistant'){await page.locator('[data-confirm-message]').click();await page.getByText('Добавлено в базу знаний',{exact:true}).waitFor();}
    await page.waitForTimeout(260);if([390,1280].includes(width)){fs.mkdirSync('artifacts',{recursive:true});await page.screenshot({path:`artifacts/ai-${name}-${width}-${theme}.png`,fullPage:true});}
   }
   await page.locator('[data-conversation="1"]').click();
  }
  await page.locator('.ai-bubble').last().waitFor();
  await page.locator('.ai-compose textarea').fill('Проверка отправки');await page.locator('.ai-compose textarea').press('Enter');await page.locator('.ai-bubble').filter({hasText:'Проверка отправки'}).waitFor();
  const metrics=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,theme:document.documentElement.dataset.theme,color:getComputedStyle(document.body).backgroundColor}));
  if(metrics.overflow||metrics.theme!==theme||errors.length)throw Error(JSON.stringify({width,theme,admin,metrics,errors}));
  await page.waitForTimeout(220);fs.mkdirSync('artifacts',{recursive:true});await page.screenshot({path:`artifacts/ai-${admin?'admin':'client'}-${width}-${theme}.png`,fullPage:true});if(admin&&width<=700){await page.locator('[data-chat-back]').click();await page.locator('.ai-inbox').waitFor({state:'visible'});}
  cases++;await context.close();
 }
 await browser.close();console.log(`PASS ${cases} UI cases: mobile/desktop, light/dark, admin/customer, message submission, no overflow`);
})().catch(e=>{console.error(e);process.exit(1)});
