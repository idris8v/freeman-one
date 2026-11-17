const fs=require('fs'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage();
 const script=fs.readFileSync('app/web/static/page-motion.js','utf8');
 await page.route('https://example.test/**',async route=>{
  if(route.request().url().endsWith('/next')){await new Promise(resolve=>setTimeout(resolve,500));return route.fulfill({contentType:'text/html',body:'Destination'});}
  return route.fulfill({contentType:'text/html',body:`<div class="app-shell"><button data-ai-open="/next" disabled>Assistant</button></div><script>${script}</script>`});
 });
 await page.goto('https://example.test/');
 await page.evaluate(()=>{document.querySelector('button').disabled=true;setTimeout(()=>sessionStorage.setItem('departureOpacity',getComputedStyle(document.querySelector('.app-shell')).opacity),220);void PageMotion.navigate('/next')});
 await page.waitForURL('**/next');
 if(await page.evaluate(()=>sessionStorage.getItem('departureOpacity'))!=='0')throw Error('Departure flashes before document navigation');
 await page.goBack();
 if(await page.locator('button').isDisabled())throw Error('Entry remains disabled after Back');
 if(await page.locator('.app-shell').evaluate(el=>getComputedStyle(el).opacity)!=='1')throw Error('Returned page stays hidden');
 await browser.close();console.log('PASS cross-document fade and Back restoration');
})().catch(error=>{console.error(error);process.exit(1)});
