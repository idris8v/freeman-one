const assert=require('node:assert/strict');const {chromium,webkit}=require('playwright');
const {fixture}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{for(const [name,type]of Object.entries({chromium,webkit})){const browser=await type.launch();const {page:p,context,model}=await fixture(browser,{admin:true});let release;const gate=new Promise(r=>release=r);model.gates['GET /api/web/admin/assistant/chats']=gate;
 await p.goto('https://example.test/admin/assistant?chat=1');await p.locator('.fa-composer textarea').fill('Проверка');await p.waitForFunction(()=>!document.querySelector('.fa-composer [type=submit]').disabled);
 assert.equal(await p.locator('#assistant-app').getAttribute('aria-busy'),null,'A delayed sidebar must not block a ready chat');assert.equal(model.reads.filter(r=>r.key==='GET /api/web/admin/assistant/chats').length,1);release();delete model.gates['GET /api/web/admin/assistant/chats'];await p.locator('[data-chat="1"]').waitFor();
 assert.deepEqual(model.errors,[]);await context.close();await browser.close();console.log('PASS independent Assistant loading',name)
}})().catch(e=>{console.error(e);process.exit(1)});
