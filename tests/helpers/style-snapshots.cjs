/* Optional computed-style snapshots for the existing offline Playwright suite.
   NODE_OPTIONS=--require=./tests/helpers/style-snapshots.cjs
   FREEMAN_STYLE_SNAPSHOTS=artifacts/styles/before node tests/ui_*.cjs
   Captures only fixture DOM/CSS, never account data or a live browser. */
const fs = require('fs');
const path = require('path');
const Module = require('module');
const load = Module._load;
const root = process.env.FREEMAN_STYLE_SNAPSHOTS;
const references = {};
if (process.env.FREEMAN_STYLE_REFERENCE) references['cabinet.css']=fs.readFileSync(process.env.FREEMAN_STYLE_REFERENCE,'utf8');
if (process.env.FREEMAN_STYLE_REFERENCE_DIR) {
 const groups={'freeman-assistant.css':['theme-system','freeman-assistant','motion-system'],'freeman-ai.css':['theme-system','freeman-ai','motion-system'],'guest-purchase.css':['auth-payment','guest-purchase','post-payment'],'account-management.css':['guest-purchase','account-management']};
 for (const [name,files] of Object.entries(groups)) references[name]=files.map(file=>fs.readFileSync(path.join(process.env.FREEMAN_STYLE_REFERENCE_DIR,file+'.css'),'utf8')).join('\n');
}
const script = path.basename(process.argv[1], '.cjs');
const pages = new WeakSet();
const contexts = new WeakSet();
let pageNumber = 0;

async function snapshot(page, name) {
  if (!root || page.isClosed() || page.url() === 'about:blank') return;
  const data = await page.evaluate(references => {
    const reference=Object.entries(references).find(([name])=>document.querySelector(`link[href*="${name}"]`))?.[1];
    const stop = document.createElement('style');
    stop.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
    document.head.append(stop);
    const running=document.getAnimations().filter(animation=>animation.playState==='running');
    running.forEach(animation=>animation.pause());
    try {
      const capture = () => {
      const properties = ['display','visibility','position','boxSizing','width','height','minWidth','maxWidth','minHeight','maxHeight',
        'paddingTop','paddingRight','paddingBottom','paddingLeft','marginTop','marginRight','marginBottom','marginLeft',
        'borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth','borderTopLeftRadius','borderTopRightRadius',
        'borderBottomRightRadius','borderBottomLeftRadius','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor',
        'backgroundColor','backgroundImage','color','fontFamily','fontSize','fontWeight','lineHeight','letterSpacing','textAlign',
        'whiteSpace','textOverflow','overflowX','overflowY','opacity','boxShadow','backdropFilter','transform',
        'gridTemplateColumns','gridTemplateRows','gap','flexDirection','flexGrow','flexShrink','flexBasis','alignItems','justifyContent',
        'objectFit','appearance','outlineColor','outlineWidth','fill','stroke'];
      const items = [];
      for (const [index, element] of [...document.querySelectorAll('body,body *')].entries()) {
        if (element.matches('script,style,link,meta')) continue;
        const bounds = element.getBoundingClientRect();
        if (!bounds.width && !bounds.height) continue;
        for (const pseudo of [null, '::before', '::after']) {
          const style = getComputedStyle(element, pseudo);
          if (pseudo && ['none', 'normal', ''].includes(style.content)) continue;
          items.push({index, tag: element.tagName, class: element.getAttribute('class'), pseudo,
            bounds: pseudo ? null : [bounds.x,bounds.y,bounds.width,bounds.height].map(n=>Math.round(n*100)/100),
            style: Object.fromEntries(properties.map(key=>[key,style[key]]))});
        }
      }
      return {width:innerWidth,height:innerHeight,theme:document.documentElement.dataset.theme,items};
      };
      if (!reference) return {current:capture()};
      const current=capture();
      const links=[...document.querySelectorAll('link[rel=stylesheet]')].map(link=>link.sheet).filter(Boolean);
      const previous=links.map(link=>link.disabled);
      links.forEach(link=>link.disabled=true);
      const legacy=document.createElement('style');legacy.textContent=reference;document.head.append(legacy);
      const original=capture();legacy.remove();links.forEach((link,i)=>link.disabled=previous[i]);
      return {original,current};
    } finally { stop.remove();running.forEach(animation=>animation.play()); }
  }, references);
  fs.mkdirSync(root, {recursive:true});
  fs.writeFileSync(path.join(root, `${script}-${name}.json`), JSON.stringify(data.current));
  if (data.original) {
    fs.mkdirSync(root+'-reference', {recursive:true});
    fs.writeFileSync(path.join(root+'-reference', `${script}-${name}.json`), JSON.stringify(data.original));
  }
}

function decoratePage(page) {
  if (pages.has(page)) return page;
  pages.add(page);
  const id = ++pageNumber;
  let shots = 0;
  const screenshot = page.screenshot.bind(page);
  page.screenshot = async options => {
    await snapshot(page, `${id}-shot-${++shots}`);
    return screenshot(options);
  };
  const close = page.close.bind(page);
  page.close = async (...args) => {
    await snapshot(page, `${id}-close`);
    return close(...args);
  };
  return page;
}

function decorateContext(context) {
  if (contexts.has(context)) return context;
  contexts.add(context);
  const newPage = context.newPage.bind(context);
  context.newPage = async (...args) => decoratePage(await newPage(...args));
  const close = context.close.bind(context);
  context.close = async (...args) => {
    for (const page of context.pages()) await snapshot(page, `${pageNumber}-context-close`);
    return close(...args);
  };
  return context;
}

Module._load = function(request, parent, isMain) {
  const result = load.call(this, request, parent, isMain);
  if (root && (request === process.env.FREEMAN_PLAYWRIGHT_MODULE || request === 'playwright') && !result.chromium.__styleSnapshots) {
    for (const engine of [result.chromium,result.webkit]) {
    const launch = engine.launch.bind(engine);
    engine.__styleSnapshots = true;
    engine.launch = async (...args) => {
      const browser = await launch(...args);
      const newContext = browser.newContext.bind(browser);
      browser.newContext = async (...options) => decorateContext(await newContext(...options));
      const newPage = browser.newPage.bind(browser);
      browser.newPage = async (...options) => decoratePage(await newPage(...options));
      return browser;
    };
    }
  }
  return result;
};
