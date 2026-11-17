const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {validate}=require('../scripts/style_policy.cjs');
function fixture(t,files,compatibility={}){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'freeman-style-policy-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 for(const [name,css]of Object.entries(files)){const file=path.join(root,name);fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,css)}
 fs.writeFileSync(path.join(root,'compatibility.json'),JSON.stringify(compatibility));return root;
}
test('one component owner can define responsive and theme states',t=>{
 const root=fixture(t,{'cabinet/keys.css':'.key{color:blue}@media(max-width:390px){.key{padding:8px}}html[data-theme=dark] .key{color:white}'});
 assert.doesNotThrow(()=>validate(root));
});
test('a second module cannot silently override the same selector',t=>{
 const root=fixture(t,{'cabinet/keys.css':'.key{color:blue}','cabinet/misc.css':'.key{color:red}'});
 assert.throws(()=>validate(root),/owned by both/);
});
test('a grouped selector cannot bypass component ownership',t=>{
 const root=fixture(t,{'cabinet/keys.css':'.key{color:blue}','cabinet/misc.css':'.other,.key{color:red}'});
 assert.throws(()=>validate(root),/owned by both/);
});
test('new priority escalation is rejected',t=>{
 for(const css of ['.key{color:red!important}','#app#app .key{color:red}']){
  const root=fixture(t,{'cabinet/keys.css':css});assert.throws(()=>validate(root),/New priority override/);
 }
});
test('a compatibility allowance permits removal, not increased overrides',t=>{
 const root=fixture(t,{'cabinet/keys.css':'.key{color:red!important}'},{'cabinet/keys.css|.key':{important:1,repeated:false}});
 assert.doesNotThrow(()=>validate(root));
 fs.writeFileSync(path.join(root,'cabinet/keys.css'),'.key{color:red!important;padding:10px!important}');
 assert.throws(()=>validate(root),/New priority override/);
 fs.writeFileSync(path.join(root,'cabinet/keys.css'),'.key{color:red}');
 assert.doesNotThrow(()=>validate(root));
});

// B01: one token owner, including bundles that previously omitted the theme.
const styles=path.resolve(__dirname,'../app/web/styles');
const postcss=require('postcss'),crypto=require('node:crypto');
test('all five generated entries start with exactly one shared theme',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(styles,'manifest.json')));
 assert.equal(Object.keys(manifest).length,5);
 for(const entry of Object.values(manifest)){
  assert.equal(entry.sources[0],'shared/theme.css');
  assert.equal(entry.sources.filter(p=>p==='shared/theme.css').length,1);
 }
});
test('semantic tokens and legacy palette aliases have one owner',()=>{
 const theme=postcss.parse(fs.readFileSync(path.join(styles,'shared/theme.css'),'utf8'));
 const owned=new Set();theme.walkDecls(d=>{if(d.prop.startsWith('--'))owned.add(d.prop);assert.equal(!!d.important,false,'No priority escalation in tokens');});
 function scan(directory){for(const item of fs.readdirSync(directory,{withFileTypes:true})){
  const p=path.join(directory,item.name);if(item.isDirectory()){scan(p);continue}if(!p.endsWith('.css')||p===path.join(styles,'shared/theme.css'))continue;
  postcss.parse(fs.readFileSync(p,'utf8')).walkDecls(d=>{
   assert.equal(d.prop.startsWith('--fo-'),false,p+': canonical token redefinition '+d.prop);
   assert.equal(owned.has(d.prop),false,p+': legacy token redefinition '+d.prop);
  });
 }}scan(styles);
 for(const role of ['--fo-bg-page','--fo-text-primary','--fo-font-family','--fo-space-4','--fo-radius-control',
  '--fo-surface-shadow','--fo-control-touch-target','--fo-layout-sidebar','--fo-motion-page'])assert.ok(owned.has(role),role);
});
test('standalone compatibility is loaded only at its original cascade position',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(styles,'manifest.json')));
 for(const [name,entry]of Object.entries(manifest)){
  const position=entry.sources.indexOf('shared/legacy-standalone.css');
  assert.equal(position,['freeman-assistant.css','freeman-ai.css'].includes(name)?5:-1,name);
 }
});
test('legacy standalone compatibility has no canonical token ownership',()=>{
 const root=postcss.parse(fs.readFileSync(path.join(styles,'shared/legacy-standalone.css'),'utf8'));
 root.walkDecls(d=>assert.equal(d.prop.startsWith('--fo-'),false));
});

test('B02 components are opt-in, token-driven, and never add priority debt',()=>{
 const source=fs.readFileSync(path.join(styles,'shared/components.css'),'utf8'),root=postcss.parse(source);
 root.walkDecls(d=>{assert.equal(!!d.important,false);assert.equal(d.prop.startsWith('--'),false);assert.equal(/#[a-f\d]{3,8}\b|rgba?\(/i.test(d.value),false,'Color belongs in theme: '+d.toString());});
 root.walkRules(r=>{if(r.parent.name?.endsWith('keyframes'))return;for(const branch of require('postcss-selector-parser')().astSync(r.selector).nodes)assert.match(branch.toString(),/\.fo-/,r.selector);});
 const manifest=JSON.parse(fs.readFileSync(path.join(styles,'manifest.json')));
 for(const entry of Object.values(manifest))assert.equal(entry.sources.filter(p=>p==='shared/components.css').length,1);
 // B03 enables only the explicit, corrected overlay lifecycle.
 const html=fs.readFileSync(path.resolve(__dirname,'../app/web/static/index.html'),'utf8');
 assert.equal((html.match(/src="\/assets\/ui-system.js/g)||[]).length,1);
 const lifecycle=fs.readFileSync(path.resolve(__dirname,'../app/web/static/ui-system.js'),'utf8');
 assert.equal(lifecycle.includes('PageMotion.reduced'),false);
 assert.equal(lifecycle.includes('dataset.uiButton'),false);
});

test('pilot renderer references use current content hashes without changing script order',()=>{
 const root=path.resolve(__dirname,'../app/web/static'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
 for(const name of ['ui-system.js','app.js','clients.js','clients-mobile.js','catalog.js','keys-controls.js']){
  const hash=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex').slice(0,12);
  assert.ok(html.includes('/assets/'+name+'?v='+hash),name+' stale renderer cache');
 }
});

test('B04 shared motion is built once per entry and navigation has an explicit owner',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(styles,'manifest.json')));
 for(const entry of Object.values(manifest))assert.equal(entry.sources.filter(p=>p==='shared/motion.css').length,1);
 const root=path.resolve(__dirname,'../app/web/static'),entry=fs.readFileSync(path.join(root,'freeman-ai-entry.js'),'utf8');
 assert.equal(entry.includes('MutationObserver'),false);assert.equal(entry.includes('createElement(\'style\')'),false);
 const shell=fs.readFileSync(path.join(styles,'cabinet/shell.css'),'utf8').split('/* B04 shell ownership.')[1];
 assert.ok(shell);assert.equal(shell.includes('!important'),false);assert.equal(/#app#app/.test(shell),false);
 const motion=postcss.parse(fs.readFileSync(path.join(styles,'shared/motion.css'),'utf8'));
 motion.walkDecls(d=>assert.equal(!!d.important,false));
});
test('B04 shell and standalone runtime fingerprints are current',()=>{
 const root=path.resolve(__dirname,'../app/web/static');
 for(const [html,scripts]of Object.entries({'index.html':['page-motion.js','support-motion.js','mobile-admin-profile.js','freeman-ai-entry.js','customer-workspace.js'],'freeman-assistant.html':['page-motion.js','freeman-assistant.js'],'freeman-ai.html':['page-motion.js','freeman-ai.js']})){
  const text=fs.readFileSync(path.join(root,html),'utf8');
  for(const name of scripts){const hash=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex').slice(0,12);assert.ok(text.includes('/assets/'+name+'?v='+hash),name);}
 }
 const assistant=fs.readFileSync(path.join(root,'freeman-assistant.html'),'utf8');
 assert.doesNotMatch(assistant,/maximum-scale=1|user-scalable=no/);
});


test('global components have one owner across all five entry points',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(styles,'manifest.json')));
 for(const file of ['components','forms','navigation','overlays']){
  const name='shared/'+file+'.css';
  for(const entry of Object.values(manifest))assert.equal(entry.sources.filter(p=>p===name).length,1);
  postcss.parse(fs.readFileSync(path.join(styles,name),'utf8')).walkDecls(d=>{
   assert.equal(!!d.important,false,name);
   assert.equal(d.prop.startsWith('--'),false,name);
   assert.equal(/#[a-f\d]{3,8}\b|rgba?\(/i.test(d.value),false,name+': colors belong in theme');
  });
 }
 const root=path.resolve(__dirname,'../app/web/static');
 for(const [html,controller]of Object.entries({'index.html':'app.js','guest-purchase.html':'guest-purchase.js','account-management.html':'account-management.js','freeman-assistant.html':'freeman-assistant.js','freeman-ai.html':'freeman-ai.js'})){
  const text=fs.readFileSync(path.join(root,html),'utf8');
  assert.equal((text.match(/src="\/assets\/ui-system.js/g)||[]).length,1,html);
  assert.ok(text.indexOf('/assets/ui-system.js')<text.indexOf('/assets/'+controller),html);
  assert.doesNotMatch(text,/class="brand-intro"|class="legal-footer"/);
 }
});
