/* Complete shell and real mobile profile controls, isolated synthetic APIs. */
const assert=require('node:assert/strict'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,clean}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{const b=await chromium.launch();let cases=0;try{for(const width of [360,375,390,414,430])for(const theme of ['light','dark']){
 const f=await fixture(b,{admin:true,width,theme}),p=f.page;let revoked=0;
 try{
  await f.context.route('https://example.test/api/web/trusted-devices',r=>r.fulfill({json:{items:[{id:7,label:'Safari · iPhone',current:true,last_seen_at:'2026-10-10T08:00:00Z'}]}}));
  await f.context.route('https://example.test/api/web/trusted-devices/7',r=>{revoked++;return r.fulfill({json:{ok:true}})});
  await p.locator('.ma-home').waitFor();
  const signature=()=>p.locator('.mobile-nav').evaluate(n=>n.outerHTML),nav=await signature();
  const geometry=await p.evaluate(()=>{const a=document.querySelector('.ma-identity').getBoundingClientRect(),c=document.querySelector('.ma-balance').getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth,sameRow:Math.abs(a.top+a.height/2-c.top-c.height/2)<2,rowHeights:[...document.querySelectorAll('.ma-row')].map(n=>n.getBoundingClientRect().height),icons:[...document.querySelectorAll('.ma-icon')].every(n=>!!n.querySelector('svg,img')),header:document.querySelector('.profile-header-actions').getBoundingClientRect().right<=innerWidth}});
  assert.ok(!geometry.overflow&&geometry.sameRow&&geometry.icons&&geometry.header&&geometry.rowHeights.every(h=>h>=44&&h<=85),JSON.stringify({width,theme,geometry}));
  assert.equal(await p.locator('.ma-home [data-view="admin-catalog"]').count(),1);assert.equal(await p.locator('.ma-home [data-ai-open]').count(),2);
  await p.evaluate(()=>{state.view='admin-posts';window.pubCanLeave=async()=>false});await p.getByRole('button',{name:'Настройки',exact:true}).click();assert.equal(await p.locator('.ma-home').count(),1,'Unsaved publication protection');
  await p.evaluate(()=>{state.view='admin-dashboard'});await p.getByRole('button',{name:'Настройки',exact:true}).click();
  assert.equal(await p.locator('.ma-group').count(),4);
  assert.equal(await p.locator('.ma-page [data-admin-panel="sessions"],.ma-page [data-admin-panel="browsers"],.ma-page [data-admin-panel="language"],.ma-page [data-support-settings]').count(),0);
  await p.getByRole('button',{name:'Безопасность',exact:true}).click();await p.getByRole('button',{name:'Доверенные браузеры',exact:true}).click();
  await p.getByText('Safari · iPhone · Это устройство').waitFor();await p.getByRole('button',{name:'Удалить доверие'}).click();await p.waitForFunction(()=>!document.querySelector('[data-admin-browser-revoke]')?.disabled);assert.equal(revoked,1);
  await p.getByRole('button',{name:'Назад',exact:true}).click();await p.getByRole('heading',{name:'Безопасность',exact:true}).waitFor();
  await p.getByRole('button',{name:'Назад',exact:true}).click();await p.getByRole('heading',{name:'Настройки',exact:true}).waitFor();
  await p.getByRole('button',{name:'Личные данные',exact:true}).click();await p.getByText('fixture@example.test',{exact:true}).waitFor();
  await p.getByRole('button',{name:'Назад',exact:true}).click();await p.getByRole('heading',{name:'Настройки',exact:true}).waitFor();
  await p.getByRole('button',{name:'Назад',exact:true}).click();await p.locator('.ma-home').waitFor();assert.equal(await signature(),nav);
  await p.setViewportSize({width:1280,height:900});await p.waitForFunction(()=>!document.querySelector('.screen:not([hidden]) .ma-page'));
  await p.evaluate(()=>{state.data.profile.is_admin=false;state.view='profile';render()});await p.locator('.screen:not([hidden]) .profile-page').waitFor();assert.equal(await p.locator('[data-ai-open]').count(),0);clean(f.model);cases++;
 }finally{await f.context.close()}
}}finally{await b.close()}console.log('PASS '+cases+' mobile admin cases: real shell, grouped settings, browser revoke, contextual Back, stable bottom bar and role fallback')})().catch(e=>{console.error(e);process.exit(1)});
