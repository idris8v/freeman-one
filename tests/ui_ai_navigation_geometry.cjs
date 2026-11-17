/* Real shell descriptors + all CSS; no observer-injection test doubles. */
const assert=require('node:assert/strict'),{chromium}=require(process.env.FREEMAN_PLAYWRIGHT_MODULE||'playwright');
const {fixture,navigate,clean}=require('./helpers/ui-b00-fixture.cjs');
(async()=>{const b=await chromium.launch();try{for(const width of [320,360,390]){
 const f=await fixture(b,{admin:true,width});try{await navigate(f.page,'admin-clients','.crm-page');
 const geometry=await f.page.locator('.mobile-nav').evaluate(n=>{const r=n.getBoundingClientRect();return [...n.querySelectorAll(':scope>button')].map(x=>{const b=x.getBoundingClientRect(),t=x.querySelector('span');return {inside:b.left>=r.left&&b.right<=r.right&&b.top>=r.top&&b.bottom<=r.bottom,top:b.top,textFits:t.scrollWidth<=t.clientWidth}})});
 assert.equal(geometry.length,5);assert.ok(geometry.every(x=>x.inside&&x.textFits&&Math.abs(x.top-geometry[0].top)<=1),JSON.stringify({width,geometry}));clean(f.model);
 }finally{await f.context.close()}
}}finally{await b.close()}console.log('PASS full stylesheet mobile nav geometry: 320, 360, 390')})().catch(e=>{console.error(e);process.exit(1)});
