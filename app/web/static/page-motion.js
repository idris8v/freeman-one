/* Shared motion and viewport lifecycle. Existing route owners supply content. */
window.PageMotion = (() => {
  if(window.PageMotion)return window.PageMotion;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const running = new Set(), stages = new Set(), viewports = new Set(), drawers = new Set();
  const defaults = {page:280,exit:140,panel:220,drawer:280,message:220,press:140,response:320};
  const tokens = {};
  for (const [role, fallback] of Object.entries(defaults)) Object.defineProperty(tokens,role,{enumerable:true,get(){
    const value=getComputedStyle(document.documentElement).getPropertyValue('--fo-motion-'+role).trim();
    const match=/^(\d+(?:\.\d+)?)(ms|s)$/.exec(value), n=match?Number(match[1])*(match[2]==='s'?1000:1):NaN;
    return Number.isFinite(n)&&n>=0&&n<=2000?n:fallback;
  }});
  Object.defineProperty(tokens,'easing',{enumerable:true,get(){
    const value=getComputedStyle(document.documentElement).getPropertyValue('--fo-motion-ease').trim();
    return value&&CSS.supports('transition-timing-function',value)?value:'cubic-bezier(.22,1,.36,1)';
  }});
  Object.freeze(tokens);
  function animate(node,frames,duration=tokens.page,channel='feedback') {
    if(!node||reduced.matches||document.hidden)return null;
    node.__foAnimations ||= new Map();
    const previous=node.__foAnimations.get(channel);
    if(previous?.playState==='running'&&Array.isArray(frames)){
      const style=getComputedStyle(node),first={...frames[0]};
      for(const property of ['opacity','transform','translate','scale'])if(property in first)first[property]=style[property];
      frames=[first,...frames.slice(1)];
    }
    previous?.cancel();
    const animation=node.animate(frames,{duration,easing:tokens.easing});
    node.__foAnimations.set(channel,animation);running.add(animation);
    animation.finished.catch(()=>{}).finally(()=>{running.delete(animation);if(node.__foAnimations.get(channel)===animation)node.__foAnimations.delete(channel);});
    return animation;
  }
  function depart(root) {
    if(!root?.matches('.fo-overlay')||!root.isConnected||reduced.matches||document.hidden)return;
    const box=root.getBoundingClientRect(),style=getComputedStyle(root);
    if(!box.width||!box.height)return;
    // End focus/input ownership immediately; retain the real, inert chat sheet
    // for its short exit. No duplicate interactive content or business state.
    const layer=document.createElement('div');layer.className='fo-motion-departure';layer.inert=true;layer.setAttribute('aria-hidden','true');
    Object.assign(layer.style,{left:box.left+'px',top:box.top+'px',width:box.width+'px',height:box.height+'px',background:style.backgroundColor,backdropFilter:style.backdropFilter,zIndex:style.zIndex});
    const panel=root.querySelector('.fo-chat-sheet[data-fo-panel]');
    if(panel){
      const bounds=panel.getBoundingClientRect(),current=getComputedStyle(panel);
      const opacity=current.opacity;
      panel.getAnimations().forEach(animation=>animation.cancel());
      Object.assign(panel.style,{position:'absolute',left:(bounds.left-box.left)+'px',top:(bounds.top-box.top)+'px',width:bounds.width+'px',height:bounds.height+'px',maxHeight:'none',margin:'0',transform:'none'});
      panel.removeAttribute('aria-modal');panel.removeAttribute('aria-labelledby');panel.removeAttribute('role');
      panel.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
      layer.classList.add('fo-scope');layer.append(panel);
      animate(panel,[{opacity,translate:'0 0',scale:1},{opacity:0,translate:'0 10px',scale:.99}],tokens.exit,'sheet-exit');
    }
    document.body.append(layer);
    const animation=animate(layer,[{opacity:style.opacity},{opacity:0}],tokens.exit,'departure');
    if(animation)animation.finished.catch(()=>{}).finally(()=>layer.remove());else layer.remove();
  }
  let direction=1;
  function intent(value=1){direction=value<0?-1:1}
  function content(node,key){if(!node||node.__foContentKey===key)return null;node.__foContentKey=key;return animate(node,[{opacity:.55},{opacity:1}],tokens.panel,"content")}
  function enter(node,flat=false,axis="y") {
    const animation=animate(node,flat?[{opacity:0},{opacity:1}]:[{opacity:.2,translate:axis==='x'?`${direction*18}px 0`:'0 6px'},{opacity:1,translate:'0 0'}],tokens.page,'page');
    if(node)node.__pageEntrance=animation;return animation;
  }
  function after(root,key,pending,chat,force) {
    if(!root)return;const previous=root.__pageMotion;root.__pageMotion={key,pending};
    if(pending||!force&&(root.__foEnteredKey===key||previous?.key===key&&!previous.pending))return;
    root.__foEnteredKey=key;enter(root,chat);
  }
  function settle(stage) {stage?.__foTransition?.();}
  function swap(stage,from,to,key,flat=false) {
    const opacity=from?getComputedStyle(from).opacity:1;
    swipes.forEach(control=>control.cancel());settle(stage);to.hidden=false;to.__pageMotion={key,pending:false};to.__foEnteredKey=key;
    if(!from||from===to){enter(to,flat);return;}
    from.__pageEntrance?.cancel();const inert=from.inert;from.inert=true;
    from.classList.add('is-leaving');to.classList.add('is-entering');
    // Ordinary routes cross-fade in place; physical horizontal motion belongs to chat gestures.
    const out=animate(from,[{opacity},{opacity:0}],tokens.exit,'page'),incoming=enter(to,true,'x');
    out?.effect.updateTiming({fill:'forwards'});
    let done=false;const finish=()=>{
      if(done)return;done=true;out?.cancel();incoming?.cancel();
      from.hidden=true;from.inert=inert;from.classList.remove('is-leaving');to.classList.remove('is-entering');
      if(stage.__foTransition===finish)delete stage.__foTransition;stages.delete(stage);
    };
    stage.__foTransition=finish;stages.add(stage);
    if(!incoming)finish();else Promise.allSettled([out?.finished,incoming.finished]).then(finish);
  }
  let navigating=0,departure=null,departureTimer=null;
  function resetDeparture(enable=true) {
    clearTimeout(departureTimer);departure?.cancel();departure=null;
    if(enable)document.querySelectorAll('[data-ai-open]:disabled').forEach(button=>{button.disabled=false});
  }
  async function navigate(url,node) {
    const destination=new URL(url,location.href);
    if(destination.origin!==location.origin)return location.assign(destination.href);
    window.CabinetNavigation?.leaving();
    if(document.querySelector('#assistant-app,#ai-app')&&destination.pathname==='/'){
      try{sessionStorage.setItem('freeman-cabinet-return','1')}catch{}
    }
    const request=++navigating;resetDeparture(false);
    try {
      departure=animate(node||document.querySelector('.app-shell,#assistant-app,#ai-app'),[{opacity:1},{opacity:0}],tokens.exit,'departure');
      if(departure)departure.effect.updateTiming({fill:'forwards'});
      if(request!==navigating)return;
      // Retain the old page if document loading stalls, rather than a blank shell.
      departureTimer=setTimeout(resetDeparture,Math.max(1200,tokens.page*4));
      location.assign(destination.href);
    }catch(error){if(request===navigating)resetDeparture();throw error;}
  }
  function watchViewport(root,{height,top,left,width,safe,insetTop,telegram=false,lockClass}={}) {
    let wanted=false,attached=false,frame=0,scroll=null,saved=null;
    const viewport=window.visualViewport;let tg=null,tgHeight=0;
    const properties=[height,top,left,width,safe,insetTop].filter(Boolean);
    const tgViewport=event=>{if(event?.isStateStable!==false||root.contains(document.activeElement)&&document.activeElement?.matches('textarea,input,[contenteditable]'))tgHeight=Number(tg?.viewportHeight)||0;schedule()};
    function paint(){
      frame=0;if(!wanted||!root.isConnected)return;
      const v=window.visualViewport,h=Math.min(v?.height||innerHeight,tgHeight>0?tgHeight:Infinity),reference=Math.max(innerHeight,Number(tg?.viewportStableHeight)||0),keyboard=h<reference-100;
      const bottom=keyboard?'0px':tg?Math.max(0,tg.safeAreaInset?.bottom||0,tg.contentSafeAreaInset?.bottom||0)+'px':'env(safe-area-inset-bottom,0px)';
      const upper=tg?Math.max(0,tg.safeAreaInset?.top||0,tg.contentSafeAreaInset?.top||0)+'px':'0px';
      const values=[[height,h+'px'],[top,(v?.offsetTop||0)+'px'],[left,(v?.offsetLeft||0)+'px'],[width,(v?.width||innerWidth)+'px'],[safe,bottom],[insetTop,upper]];
      for(const [name,value]of values)if(name)root.style.setProperty(name,value);
    }
    function schedule(){if(!frame)frame=requestAnimationFrame(paint);}
    function connect(){
      if(attached||!wanted||document.hidden)return;attached=true;
      if(!saved)saved=properties.map(name=>[name,root.style.getPropertyValue(name),root.style.getPropertyPriority(name)]);
      if(lockClass&&scroll===null){scroll=scrollY;root.classList.add(lockClass);}
      const app=window.Telegram?.WebApp;if(telegram&&app?.initData){tg=app;tgHeight=Number(app.viewportStableHeight)||Number(app.viewportHeight)||0;for(const name of ['viewportChanged','safeAreaChanged','contentSafeAreaChanged'])tg.onEvent?.(name,tgViewport);}
      viewport?.addEventListener('resize',schedule);viewport?.addEventListener('scroll',schedule);
      window.addEventListener('resize',schedule);window.addEventListener('orientationchange',schedule);root.addEventListener('focusin',schedule);
      schedule();
    }
    function disconnect(){
      if(!attached)return;attached=false;cancelAnimationFrame(frame);frame=0;
      viewport?.removeEventListener('resize',schedule);viewport?.removeEventListener('scroll',schedule);
      window.removeEventListener('resize',schedule);window.removeEventListener('orientationchange',schedule);root.removeEventListener('focusin',schedule);
      if(tg)for(const name of ['viewportChanged','safeAreaChanged','contentSafeAreaChanged'])tg.offEvent?.(name,tgViewport);tg=null;tgHeight=0;
    }
    function update(active=true){
      wanted=active;if(active){connect();schedule();return;}
      disconnect();if(lockClass){root.classList.remove(lockClass);if(scroll!==null)window.scrollTo(0,scroll);scroll=null;}
      for(const [name,value,priority]of saved||[]){if(value)root.style.setProperty(name,value,priority);else root.style.removeProperty(name);}saved=null;
    }
    const control={update,suspend:disconnect,resume:()=>{connect();schedule()},destroy:()=>{update(false);viewports.delete(control)},get active(){return wanted},get attached(){return attached}};
    viewports.add(control);return control;
  }
  // Exact critical damping is shared by drawers and cancelled Back gestures.
  function damped(position,velocity,target,dt,response=tokens.response){const omega=2*Math.PI/Math.max(.1,response/1000),delta=position-target,c=velocity+omega*delta,decay=Math.exp(-omega*dt);return {position:target+(delta+c*dt)*decay,velocity:(velocity-omega*c*dt)*decay}}
  // One scoped gesture subscription per stable application root. Back still
  // goes through the existing control, preserving drafts, routing and guards.
  const swipeBackRoots=new WeakMap(),swipes=new Set();
  function swipeBack({root,target,back,enabled=()=>true,breakpoint=900}){
    if(!root)return null;if(swipeBackRoots.has(root))return swipeBackRoots.get(root);
    const listeners=new AbortController();let gesture=null,returning=null;
    const ignored='button,a,input,textarea,select,summary,video,audio,img,canvas,table,pre,[contenteditable],[data-chat-photo],[data-fo-overlay]';
    function restore(g){if(g?.node)g.node.style.translate=g.original;}
    function cancel(){const g=gesture||returning?.g;gesture=null;returning?.cancel();returning=null;restore(g);}
    function blocked(){return [...document.querySelectorAll('[data-fo-overlay],dialog[open]')].some(n=>n.getClientRects().length)||root.querySelector('[data-voice-active=true]');}
    root.addEventListener('touchstart',event=>{
      if(gesture)cancel();if(innerWidth>breakpoint||!enabled()||blocked()||event.touches.length!==1||event.target.closest(ignored)||getSelection()?.isCollapsed===false)return;
      const point=event.touches[0],node=target();if(point.clientX<24||!node?.contains(event.target)||!node.getClientRects().length)return;
      const live=getComputedStyle(node).translate,start=live==='none'?0:parseFloat(live)||0,original=returning?.g.node===node?returning.g.original:node.style.translate;returning?.cancel();returning=null;
      gesture={node,x:point.clientX,y:point.clientY,at:performance.now(),dx:start,start,original,locked:false,samples:[[performance.now(),point.clientX]]};
    },{passive:true,capture:true,signal:listeners.signal});
    root.addEventListener('touchmove',event=>{
      const g=gesture;if(!g)return;if(event.touches.length!==1||document.hidden||blocked()||!enabled()||target()!==g.node){cancel();return}
      const p=event.touches[0],dx=p.clientX-g.x,dy=p.clientY-g.y;
      if(!g.locked){if(Math.abs(dx)<10&&Math.abs(dy)<10)return;if(dx<=0||Math.abs(dx)<Math.abs(dy)*1.35){cancel();return}g.locked=true}
      if(!event.cancelable){cancel();return}event.preventDefault();event.stopImmediatePropagation();g.dx=Math.max(0,Math.min(g.node.clientWidth*.9,g.start+dx));g.node.style.translate=g.dx+'px 0';const now=performance.now();g.samples.push([now,p.clientX]);g.samples=g.samples.filter(s=>now-s[0]<100);
    },{passive:false,capture:true,signal:listeners.signal});
    root.addEventListener('touchend',event=>{
      const g=gesture;gesture=null;if(!g)return;if(!g.locked){restore(g);return}event.stopImmediatePropagation();const p=event.changedTouches[0],sample=g.samples[0],elapsed=sample?performance.now()-sample[0]:0,velocity=elapsed>=8?Math.max(-3,Math.min(3,(p.clientX-sample[1])/elapsed)):0;
      const commit=!document.hidden&&!blocked()&&enabled()&&target()===g.node&&(g.dx>=Math.max(80,g.node.clientWidth*.22)||g.dx>=40&&velocity>.55);
      if(commit){restore(g);back();return}rebound(g,velocity*1000);
    },{passive:true,capture:true,signal:listeners.signal});
    root.addEventListener('touchcancel',cancel,{passive:true,capture:true,signal:listeners.signal});
    function rebound(g,velocity){
      if(reduced.matches||document.hidden){restore(g);return}
      let position=g.dx,frame=0,last=performance.now();const original=parseFloat(g.original)||0;
      const returnState={g,cancel(){cancelAnimationFrame(frame)}};returning=returnState;
      function tick(now){if(!g.node.isConnected||document.hidden||reduced.matches){restore(g);if(returning===returnState)returning=null;return}const next=damped(position,velocity,original,Math.min((now-last)/1000,.032));last=now;position=next.position;velocity=next.velocity;g.node.style.translate=position+'px 0';if(Math.abs(position-original)<.15&&Math.abs(velocity)<3){restore(g);if(returning===returnState)returning=null}else frame=requestAnimationFrame(tick)}
      frame=requestAnimationFrame(tick);
    }
    const api={cancel,destroy(){cancel();listeners.abort();swipeBackRoots.delete(root);swipes.delete(api)}};swipeBackRoots.set(root,api);swipes.add(api);return api;
  }

  function drawer({root,side,main,shade,trigger,breakpoint=768,swipeSurface=null}) {
    // One physical value owns touch tracking and button transitions. Reversing a
    // transition keeps both its current position and velocity; no input lock.
    let opened=false,position=0,velocity=0,target=0,frame=0,last=0,gesture=null,width=0,response=320;
    const measure=()=>{width=Math.max(1,side.getBoundingClientRect().width);response=tokens.response};
    const saved=new Map([side,shade].map(node=>[node,['transform','opacity','transition','will-change'].map(key=>[key,node.style.getPropertyValue(key)])]));
    function restoreStyles(){for(const[node,properties]of saved)for(const[key,value]of properties){if(value)node.style.setProperty(key,value);else node.style.removeProperty(key);}}
    function paint(){
      side.style.transition='none';shade.style.transition='none';
      side.style.transform=`translate3d(${(position-1)*width}px,0,0)`;
      shade.style.opacity=String(Math.max(0,Math.min(1,position)));
    }
    function stop(){cancelAnimationFrame(frame);frame=0;last=0;}
    function settle(){stop();position=target;velocity=0;if(innerWidth>=breakpoint){restoreStyles();shade.hidden=true;return}paint();shade.hidden=!opened;side.inert=!opened;side.style.removeProperty('will-change');}
    function tick(now){
      if(!root.isConnected){stop();drawers.delete(control);return}
      const dt=Math.min((now-last)/1000||1/60,.032);last=now;
      // Exact critically damped integration, stable even after a delayed frame.
      const next=damped(position,velocity,target,dt,response);position=next.position;velocity=next.velocity;paint();
      if(Math.abs(position-target)<.001&&Math.abs(velocity)<.01)settle();else frame=requestAnimationFrame(tick);
    }
    function move(){if(reduced.matches||document.hidden){settle();return}side.style.willChange='transform';if(!frame){last=performance.now();frame=requestAnimationFrame(tick);}}
    function set(open,restore=true){
      const mobile=innerWidth<breakpoint;measure();opened=mobile&&open;target=opened?1:0;
      side.inert=mobile&&!opened&&position<=.001;main.inert=opened;trigger.setAttribute('aria-expanded',String(opened));
      if(mobile)shade.hidden=false;
      root.classList.toggle('drawer-open',opened);
      if(mobile)move();else settle();
      if(opened)(side.querySelector('button')||side.querySelector('a[href],input'))?.focus({preventScroll:true});else if(mobile&&restore)trigger.focus({preventScroll:true});
    }
    root.addEventListener('keydown',event=>{
      if(!opened||root.querySelector('dialog[open]')||event.isComposing)return;
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();set(false);return;}
      if(event.key==='Tab'){
        const nodes=[...side.querySelectorAll('button,a[href],input,select,[tabindex]')].filter(n=>!n.disabled&&n.tabIndex>=0&&n.getClientRects().length);
        const first=nodes[0],last=nodes.at(-1);
        if(event.shiftKey&&document.activeElement===first||!event.shiftKey&&document.activeElement===last){event.preventDefault();(event.shiftKey?last:first)?.focus();}
      }
    });
    if(swipeSurface){
      const ignore='a,button,input,textarea,select,summary,[contenteditable=true],pre,table,.fa-gallery';
      root.addEventListener('touchstart',event=>{
        gesture=null;if(innerWidth>=breakpoint||event.touches.length!==1||event.target.closest(ignore)||!getSelection()?.isCollapsed)return;
        const touch=event.touches[0],closing=(opened||position>.001)&&side.contains(event.target);
        if(!closing&&(!swipeSurface.contains(event.target)||opened||touch.clientX<24||touch.clientX>innerWidth-24))return;
        measure();gesture={x:touch.clientX,y:touch.clientY,closing,dx:0,locked:false,start:position,samples:[[performance.now(),touch.clientX]]};
      },{passive:true});
      root.addEventListener('touchmove',event=>{
        if(!gesture)return;if(event.touches.length!==1){if(gesture.locked)set(opened,false);gesture=null;return}
        const touch=event.touches[0],dx=touch.clientX-gesture.x,dy=touch.clientY-gesture.y;
        if(Math.max(Math.abs(dx),Math.abs(dy))<10)return;
        if(!gesture.locked&&(Math.abs(dy)>Math.abs(dx)*.8||(!gesture.closing&&dx<0))){gesture=null;return}
        if(Math.abs(dx)>Math.abs(dy)*1.4){
          if(!event.cancelable){if(gesture.locked)set(opened,false);gesture=null;return}
          if(!gesture.locked){stop();shade.hidden=false;side.inert=false;gesture.locked=true;}
          gesture.dx=dx;event.preventDefault();
          position=Math.max(0,Math.min(1,gesture.start+dx/width));paint();
          const now=performance.now();gesture.samples.push([now,touch.clientX]);gesture.samples=gesture.samples.filter(s=>now-s[0]<100);
        }
      },{passive:false});
      root.addEventListener('touchend',()=>{
        const g=gesture;gesture=null;if(!g?.locked)return;
        const end=g.samples.at(-1),start=g.samples[0],elapsed=end[0]-start[0];
        velocity=performance.now()-end[0]<100&&elapsed>8?(end[1]-start[1])/elapsed*1000/width:0;
        set(position+velocity*.16>.5,false);
      },{passive:true});
      root.addEventListener('touchcancel',()=>{if(gesture?.locked)set(opened,false);gesture=null},{passive:true});
    }
    const control={settle};drawers.add(control);set(false,false);return set;
  }
  function settleAll(){for(const stage of [...stages])settle(stage);for(const animation of [...running])animation.cancel();drawers.forEach(drawer=>drawer.settle());swipes.forEach(swipe=>swipe.cancel());resetDeparture();}
  reduced.addEventListener('change',event=>{if(event.matches)settleAll();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){settleAll();viewports.forEach(v=>v.suspend());}else viewports.forEach(v=>v.resume());});
  addEventListener('pagehide',()=>{settleAll();viewports.forEach(v=>v.suspend());});
  addEventListener('pageshow',()=>{resetDeparture();viewports.forEach(v=>v.resume());});
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href]');
    if(event.defaultPrevented||!link||link.target||link.download||event.button||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    const url=new URL(link.href,location.href);
    if(url.origin===location.origin&&document.querySelector('#assistant-app,#ai-app')&&['/','/admin/ai','/admin/assistant'].includes(url.pathname)){event.preventDefault();void navigate(url.href);}
  });
  return {tokens,reduced,animate,depart,enter,content,intent,after,swap,settle,navigate,watchViewport,drawer,swipeBack,
    diagnostics:()=>({animations:running.size,transitions:stages.size,viewportSubscriptions:[...viewports].filter(v=>v.attached).length})};
})();
