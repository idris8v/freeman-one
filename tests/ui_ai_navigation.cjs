/* Complete cabinet: explicit navigation ownership, isolated support APIs. */
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,navigate,clean}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{const b=await chromium.launch(),f=await fixture(b,{admin:true}),{page,model}=f;
 try{
  await navigate(page,'support','.native-inbox');
  assert.equal(await page.locator('.mobile-nav button[aria-current=page]').getAttribute('data-view'),'support');
  assert.equal(await page.locator('.mobile-nav>button').count(),5);
  assert.equal(await page.locator('.mobile-nav>button').nth(3).getAttribute('data-ai-open'),'/admin/assistant');
  assert.equal(await page.locator('.sidebar-main [data-ai-open]').count(),2);
  await page.locator('[data-support-ticket="7"]').click();const input=page.locator('[data-support-message] textarea');await input.waitFor();
  await input.fill('Тест');await input.press('Shift+Enter');assert.equal(model.writes.length,0);await input.press('Enter');
  await page.waitForFunction(()=>!state.busy&&document.querySelector('[data-support-message] textarea').value==='');
  assert.equal(model.writes.filter(x=>x.key==='POST /api/web/support/messages').length,1);
  let deleted=0;await f.context.route('https://example.test/api/web/support/tickets/7',route=>{assert.equal(route.request().method(),'DELETE');deleted++;return route.fulfill({json:{deleted:true}})});
  page.on('dialog',dialog=>dialog.accept());await page.locator('[data-support-menu]').click();await page.locator('[data-support-delete-ticket]').click();
  await page.waitForFunction(()=>!state.supportTicket);assert.equal(deleted,1);
  await page.evaluate(()=>{render();render()});assert.equal(await page.locator('.mobile-nav [data-ai-open]').count(),1);
  await page.evaluate(()=>{state.data.profile.is_admin=false;state.view='profile';render()});
  assert.equal(await page.locator('.mobile-nav [data-ai-open]').count(),0);assert.equal(await page.locator('.mobile-nav>button').count(),4);
  clean(model);
 }finally{await f.context.close();await b.close()}
 console.log('PASS native navigation, assistant after support, Enter/Shift+Enter, delete action, explicit idempotent render and role change');
})().catch(e=>{console.error(e);process.exit(1)});
