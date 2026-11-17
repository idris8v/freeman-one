/* Explicit overlay lifecycle for opted-in templates. No API, route, or global
   control inference. Legacy overlays and native select/chooser behavior stay owned
   by their controllers. Loaded once, before the cabinet renderers. */
(() => {
  if (window.FreemanUI) return;
  const records = [], hosts = new WeakMap(), inert = new Map(), pending = new Map();
  const focusable = 'button,a[href],input:not([type=hidden]),select,textarea,summary,[tabindex]';
  let modality='initial';
  let origin = null, sequence = 0, locked = null, observer = null, frame = 0, redirecting = false, overlayTelegram = null;
  const visible = n => n?.isConnected && !n.closest('[hidden],[inert]') && n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
  const tabs = panel => [...panel.querySelectorAll(focusable)].filter(n => visible(n) && !n.disabled && n.tabIndex >= 0);
  const top = () => records.at(-1);
  const find = node => records.find(r => r.root === node || r.panel === node || r.panel.contains(node));
  function bookmark(node) {
    if (!node || node === document.body) return null;
    const attrs = [...node.attributes || []].filter(a => a.name === 'id' || a.name === 'name' || a.name.startsWith('data-') && !a.name.startsWith('data-fo-'));
    const attribute = attrs.find(a => a.name === 'id') || attrs.find(a => a.name.startsWith('data-')) || attrs.find(a => a.name === 'name');
    const selector = attribute ? `${node.tagName.toLowerCase()}[${attribute.name}${attribute.value?'="'+CSS.escape(attribute.value)+'"':''}]` : null;
    return {node, selector, scope: node.closest('[data-screen]')?.dataset.screen};
  }
  function resolve(mark, within) {
    if (!mark) return null;
    if (visible(mark.node) && (!within || within.contains(mark.node))) return mark.node;
    const scope = within || (mark.scope && document.querySelector(`.screen[data-screen="${CSS.escape(mark.scope)}"]`)) || document;
    return mark.selector ? [...scope.querySelectorAll(mark.selector)].find(visible) : null;
  }
  function focus(node) {if (visible(node)) node.focus({preventScroll: true});}
  function remember(node) {if (!records.some(r => r.panel.contains(node))) {const mark=bookmark(node);if(mark){origin=mark;setTimeout(()=>{if(origin===mark)origin=null;},0);}}}
  function restore(mark) {
    const modal = [...records].reverse().find(r => r.modal);
    const target = resolve(mark);
    if (target && (!modal || modal.panel.contains(target))) focus(target);
    else if (modal) focus(tabs(modal.panel)[0] || modal.panel);
  }
  function viewport() {
    const v = window.visualViewport,app=window.Telegram?.WebApp,mini=app?.initData?Number(app.viewportHeight):0;
    return {left: v?.offsetLeft || 0, top: v?.offsetTop || 0, width: v?.width || innerWidth, height: Math.min(v?.height || innerHeight,mini>0?mini:Infinity)};
  }
  function position(r) {
    const v = viewport();
    if (r.modal) {
      for (const [key, value] of Object.entries({top:v.top,left:v.left,width:v.width,height:v.height})) r.root.style.setProperty('--fo-overlay-'+key, value+'px');
      return;
    }
    const a = r.anchor?.getBoundingClientRect(); if (!a) return;
    const gap = 8, edge = 8, width = Math.min(r.panel.offsetWidth, v.width - edge*2);
    const below = v.top + v.height - a.bottom - gap - edge, above = a.top - v.top - gap - edge;
    const height = Math.min(r.panel.scrollHeight, Math.max(below, above), v.height-edge*2);
    r.panel.style.maxHeight = Math.max(44, height)+'px';
    r.panel.style.maxWidth = (v.width-edge*2)+'px';
    const under = below >= height || below >= above;
    const y = under ? a.bottom+gap : a.top-gap-Math.min(r.panel.offsetHeight,height);
    r.panel.style.transformOrigin=under?'top right':'bottom right';
    r.panel.style.left = Math.max(v.left+edge, Math.min(a.right-width, v.left+v.width-width-edge))+'px';
    r.panel.style.top = Math.max(v.top+edge, Math.min(y,v.top+v.height-r.panel.offsetHeight-edge))+'px';
  }
  function anchorPanel(panel,anchor){if(!panel||!anchor)return;panel.style.position='fixed';panel.style.right='auto';position({panel,anchor});}
  function layout() {frame = 0; records.forEach(position);}
  function schedule() {if (!frame) frame = requestAnimationFrame(layout);}
  function effects() {
    for (const [node, saved] of inert) {node.inert = saved.inert; if (saved.hidden === null) node.removeAttribute('aria-hidden'); else node.setAttribute('aria-hidden',saved.hidden);}
    inert.clear();
    const modal = [...records].reverse().find(r => r.modal);
    if (modal) {
      // Inert siblings along the ancestor path, never an ancestor of the dialog.
      let node = modal.root;
      while (node && node !== document.body) {
        for (const sibling of node.parentElement?.children || []) if (sibling !== node && !['SCRIPT','STYLE','LINK'].includes(sibling.tagName) && !records.slice(records.indexOf(modal)).some(r => sibling === r.root || sibling.contains(r.root))) {
          inert.set(sibling,{inert:sibling.inert,hidden:sibling.getAttribute('aria-hidden')}); sibling.inert=true; sibling.setAttribute('aria-hidden','true');
        }
        node = node.parentElement;
      }
      if (!locked) {
        const names=['position','top','left','width','overflow','padding-right'];
        locked={x:scrollX,y:scrollY,body:names.map(p=>[p,document.body.style.getPropertyValue(p),document.body.style.getPropertyPriority(p)]),overflow:['overflow',document.documentElement.style.getPropertyValue('overflow'),document.documentElement.style.getPropertyPriority('overflow')]};
        const scrollbar=innerWidth-document.documentElement.clientWidth;
        if(scrollbar>0)document.body.style.paddingRight=(parseFloat(getComputedStyle(document.body).paddingRight)+scrollbar)+'px';
        Object.assign(document.body.style,{position:'fixed',top:-locked.y+'px',left:-locked.x+'px',width:'100%',overflow:'hidden'});
        document.documentElement.style.overflow='hidden';
      }
    } else if (locked) {
      const saved=locked;locked=null;
      for(const [p,value,priority] of saved.body) {if(value)document.body.style.setProperty(p,value,priority);else document.body.style.removeProperty(p);}
      const [property,value,priority]=saved.overflow;if(value)document.documentElement.style.setProperty(property,value,priority);else document.documentElement.style.removeProperty(property);window.scrollTo(saved.x,saved.y);
    }
    records.forEach((r,i)=>{r.root.style.zIndex=String(1000+i*2);position(r);});
    if (records.length && !observer) {
      observer=new MutationObserver(()=>{for(const r of [...records])if(!r.root.isConnected||!r.modal&&!r.anchor?.isConnected)finish(r,'unmount',false);effects();});
      observer.observe(document.body,{childList:true,subtree:true});
      document.addEventListener('focusin',containFocus,true);
      window.addEventListener('resize',schedule);document.addEventListener('scroll',schedule,true);
      window.visualViewport?.addEventListener('resize',schedule);window.visualViewport?.addEventListener('scroll',schedule);
      const app=window.Telegram?.WebApp;if(app?.initData){overlayTelegram=app;for(const name of ['viewportChanged','safeAreaChanged','contentSafeAreaChanged'])app.onEvent?.(name,schedule);}
    } else if (!records.length && observer) {
      observer.disconnect();observer=null;document.removeEventListener('focusin',containFocus,true);
      window.removeEventListener('resize',schedule);document.removeEventListener('scroll',schedule,true);
      window.visualViewport?.removeEventListener('resize',schedule);window.visualViewport?.removeEventListener('scroll',schedule);
      if(overlayTelegram)for(const name of ['viewportChanged','safeAreaChanged','contentSafeAreaChanged'])overlayTelegram.offEvent?.(name,schedule);overlayTelegram=null;
      if(frame)cancelAnimationFrame(frame);frame=0;
    }
  }
  function containFocus(e) {
    const r=top();if(!r||redirecting||!records.some(x=>x.modal))return;
    if (r.panel.contains(e.target)) return;
    redirecting=true;focus(tabs(r.panel)[0]||r.panel);redirecting=false;
  }
  function dirty(r) {
    if(!r.root.hasAttribute('data-fo-dirty'))return false;
    return values(r.panel)!==r.initial;
  }
  function values(panel) {
    return JSON.stringify([...panel.querySelectorAll('input,select,textarea')].filter(n=>!['submit','button','hidden'].includes(n.type)).map(n=>[n.name,n.type,n.type==='checkbox'||n.type==='radio'?n.checked:n.value]));
  }
  function busy(node) {
    const r=node && find(node);
    return !!r && (r.busy || !!r.panel.querySelector('button[aria-busy=true],[type=submit]:disabled'));
  }
  function setBusy(node,value) {const r=find(node);if(r){r.busy=!!value;r.panel.setAttribute('aria-busy',String(!!value));}}
  function detach(r,restoreFocus=true) {
    if(!records.includes(r))return;
    records.splice(records.indexOf(r),1);r.animation?.cancel();
    if(r.placeholder){if(!r.details)r.panel.hidden=true;if(r.placeholder.isConnected)r.placeholder.replaceWith(r.panel);else r.panel.remove();r.root.remove();}
    if(r.details)r.details.open=false;
    if(r.anchor?.isConnected)r.anchor.setAttribute('aria-expanded','false');
    if(r.menuTabs)for(const [node,value]of r.menuTabs){if(value===null)node.removeAttribute('tabindex');else node.setAttribute('tabindex',value);}
    if(r.confirmResolve&&!r.resolved){r.resolved=true;r.confirmResolve(false);r.root.remove();}
    r.closed=true;if(restoreFocus)queueMicrotask(()=>restore(r.opener));
  }
  function finish(r,reason,restoreFocus=true) {if(r.closed)return;if(!['unmount','navigation','commit','native-close','tab'].includes(reason))window.PageMotion?.depart(r.root);detach(r,restoreFocus);effects();r.onClose?.(reason);}
  async function requestClose(r,reason='dismiss',restoreFocus=true) {
    if(!r||r.closed||r.asking)return false;
    if(r.requestClose){r.requestClose(reason);return false;}
    if(busy(r.panel))return false;
    if(dirty(r)) {
      r.asking=true;
      const discard=await confirm({title:'Закрыть без сохранения?',message:'Несохранённые изменения будут потеряны.',accept:'Не сохранять',cancel:'Продолжить редактирование',danger:true});
      r.asking=false;if(!discard||r.closed||busy(r.panel))return false;
    }
    finish(r,reason,restoreFocus);return true;
  }
  function mount(root,options={}) {
    controls(root);
    const existing=records.find(r=>r.root===root);if(existing)return existing;
    const panel=root.querySelector('[data-fo-panel]')||root;
    const r={root,panel,modal:options.modal!==false,kind:options.kind||'dialog',onClose:options.onClose,opener:options.opener||origin||bookmark(document.activeElement),busy:false,requestClose:options.requestClose};
    if(r.modal){panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.tabIndex=-1;}
    records.push(r);r.initial=values(panel);effects();
    const initial=resolve(options.focus,panel)||panel.querySelector(root.dataset.foFocus||'[data-fo-initial-focus]')||tabs(panel)[0]||panel;
    focus(initial);
    if(!matchMedia('(prefers-reduced-motion: reduce)').matches)r.animation=window.PageMotion?.animate(panel,[{opacity:0,transform:'translateY(6px)'},{opacity:1,transform:'translateY(0)'}],window.PageMotion?.tokens.panel,'overlay');
    return r;
  }
  function openDialog(dialog,options={}){
    controls(dialog);if(dialog.open)return;
    const opener=bookmark(document.activeElement);dialog.showModal();
    const r=mount(dialog,{...options,opener,onClose:()=>{if(dialog.open)dialog.close()}});
    const cancel=e=>{e.preventDefault();void requestClose(r)};dialog.addEventListener('cancel',cancel);
    dialog.addEventListener('close',()=>dialog.removeEventListener('cancel',cancel),{once:true});
    dialog.addEventListener('close',()=>{if(!r.closed)finish(r,'native-close')},{once:true});
    return r;
  }
  function closeDialog(dialog){const r=records.find(r=>r.root===dialog);if(r)finish(r,'native-close');else if(dialog?.open)dialog.close();}
  function render(host,markup,onClose) {
    if(!host)return;
    const previous=hosts.get(host);
    if(previous?.markup===markup&&previous.record?.root.isConnected&&!previous.record.closed)return previous.record;
    const opener=previous?.record&&!previous.record.closed?previous.record.opener:origin||bookmark(document.activeElement);
    if(previous?.record)detach(previous.record,false);
    host.innerHTML=markup;controls(host);
    const root=host.querySelector('[data-fo-overlay]');
    const record=root?mount(root,{opener,onClose}):null;
    hosts.set(host,{markup,record});effects();
    if(!record&&previous?.record)queueMicrotask(()=>restore(opener));
    return record;
  }
  function beforeRender(container) {
    container?.querySelectorAll('.native-history,.ai-messages').forEach(history=>history.__foTail?.destroy());
    if(!records.length&&!origin)remember(document.activeElement);
    pending.clear();
    for(const r of [...records])if(!r.modal){
      if(container?.contains(r.anchor))pending.set(r.key,{focus:bookmark(document.activeElement),opener:r.opener});
      detach(r,false);if(!container?.contains(r.anchor))r.onClose?.('navigation');
    }
    effects();
  }
  function popup(panel,anchor,options={}) {
    if(!anchor||!visible(anchor))return;
    const existing=records.find(r=>r.panel===panel);if(existing)return existing;
    const key=panel.dataset.foAnchor||anchor.id||anchor.getAttribute('aria-label'),saved=pending.get(key);pending.delete(key);
    panel.classList.add('fo-popover','fo-scope');
    const placeholder=document.createComment('fo-popover');panel.before(placeholder);
    const root=document.createElement('div');root.className='fo-popover-layer fo-scope';if(anchor.closest('.fo-admin-workspace,.fo-admin-controls'))root.classList.add('fo-admin-controls');const owner=[...records].reverse().find(r=>r.modal&&r.panel.contains(anchor));(owner?.root||anchor.closest('dialog[open]')||anchor.closest('#app')||document.body).append(root);root.append(panel);
    const r={root,panel,anchor,placeholder,modal:false,kind:options.kind||'popover',details:options.details,key,onClose:options.onClose,opener:saved?.opener||bookmark(anchor)};
    panel.id ||= 'fo-popup-'+(++sequence);panel.tabIndex=-1;anchor.setAttribute('aria-controls',panel.id);anchor.setAttribute('aria-expanded','true');
    if(r.kind==='menu')anchor.setAttribute('aria-haspopup','menu');else anchor.removeAttribute('aria-haspopup');
    panel.setAttribute('role',r.kind==='menu'?'menu':'region');panel.setAttribute('aria-label',anchor.getAttribute('aria-label')||anchor.textContent.trim());
    if(r.kind==='menu'){
      r.menuTabs=[...panel.querySelectorAll('button,a[href]')].map(n=>[n,n.getAttribute('tabindex')]);
      for(const [n]of r.menuTabs){n.setAttribute('role','menuitem');n.tabIndex=-1;}
    }
    records.push(r);effects();
    r.animation=window.PageMotion?.animate(panel,[{opacity:0,translate:'0 4px'},{opacity:1,translate:'0 0'}],window.PageMotion?.tokens.panel,'overlay');
    focus(resolve(saved?.focus,panel)||tabs(panel)[0]||panel.querySelector('[role=menuitem]:not(:disabled)')||panel);
    return r;
  }
  function leavePopup(r,backward) {
    const nodes=tabs(document.body).filter(n=>!records.some(x=>!x.modal&&x.panel.contains(n))),index=nodes.indexOf(r.anchor);
    const destination=bookmark(backward?r.anchor:nodes[index+1]||r.anchor);
    finish(r,'tab',false);queueMicrotask(()=>restore(destination));
  }
  function sync(scope,onDismiss) {
    controls(scope);
    for(const panel of scope.querySelectorAll('[data-fo-popup]')){
      if(panel.hidden)continue;
      const details=panel.closest('details[data-fo-details]');if(details&&!details.open)continue;
      const anchor=details?.querySelector('summary')||scope.querySelector(panel.dataset.foAnchor);
      popup(panel,anchor,{details,kind:panel.dataset.foPopup,onClose:details?undefined:reason=>onDismiss?.(panel,reason)});
    }
    pending.clear();
  }
  function confirm(options) {
    if(typeof options==='string')options={title:'Подтвердите действие',message:options};
    return new Promise(resolve=>{
      const root=document.createElement('div');root.className='fo-overlay fo-scope';root.dataset.foOverlay='confirm';
      const panel=document.createElement('section');panel.className='fo-dialog';panel.dataset.foPanel='';
      const header=document.createElement('header');header.className='fo-dialog-header';
      const title=document.createElement('h2');title.id='fo-confirm-'+(++sequence);title.textContent=options.title;header.append(title);panel.setAttribute('aria-labelledby',title.id);
      const message=document.createElement('p');message.id='fo-confirm-description-'+sequence;message.className='fo-dialog-description';message.textContent=options.message;panel.setAttribute('aria-describedby',message.id);
      const footer=document.createElement('footer');footer.className='fo-dialog-actions';
      const no=document.createElement('button');no.className='fo-button';no.dataset.foConfirm='no';no.textContent=options.cancel||'Отмена';
      const yes=document.createElement('button');yes.className='fo-button '+(options.danger?'fo-button--danger':'fo-button--primary');yes.dataset.foConfirm='yes';yes.textContent=options.accept||'Подтвердить';footer.append(no,yes);
      panel.append(header,message,footer);root.append(panel);document.body.append(root);
      const r=mount(root,{opener:bookmark(options.opener||document.activeElement),onClose:()=>root.remove()});r.confirmResolve=resolve;focus(no);
    });
  }
  function inform(message) {
    return confirm({title:'Freeman ONE',message:String(message),accept:'Понятно',cancel:'Закрыть'});
  }
  function prompt(message,value='') {
    return new Promise(resolve=>{
      const root=document.createElement('div');root.className='fo-overlay fo-scope';
      root.innerHTML='<form class="fo-dialog" data-fo-panel><header class="fo-dialog-header"><h2>Введите данные</h2></header><label class="fo-field"><span></span><input class="fo-input" name="value" autocomplete="off" data-fo-initial-focus></label><footer class="fo-dialog-actions"><button type="button" class="fo-button" data-fo-dismiss>Отмена</button><button type="submit" class="fo-button fo-button--primary">Продолжить</button></footer></form>';
      const form=root.querySelector('form'),input=form.elements.value;
      form.querySelector('label span').textContent=String(message);input.value=String(value);
      const title=form.querySelector('h2');title.id='fo-prompt-'+(++sequence);form.setAttribute('aria-labelledby',title.id);
      document.body.append(root);let answered=false;
      const r=mount(root,{onClose:()=>{root.remove();if(!answered)resolve(null)}});
      form.addEventListener('submit',e=>{e.preventDefault();if(answered||!form.reportValidity())return;answered=true;resolve(input.value);finish(r,'answer')});
    });
  }
  // Three delegated opt-in entry points. Active-only focus/viewport/disconnect
  // subscriptions are installed by effects() and removed with the final overlay.
  document.addEventListener('pointerdown',()=>{modality='pointer';document.documentElement.dataset.foModality=modality;},{capture:true,passive:true});
  document.addEventListener('click',e=>{
    if(e.detail>0){modality='pointer';document.documentElement.dataset.foModality=modality;}
    const node=e.target.closest('button,summary,a,input,select');
    if(node?.closest('[data-fo-surface]')&&!find(node))remember(node);
    const r=top();if(!r)return;
    if(!r.modal&&r.details&&r.anchor.contains(e.target)){e.preventDefault();finish(r,'toggle');return;}
    const answer=e.target.closest('[data-fo-confirm]');
    if(answer&&r.confirmResolve&&r.panel.contains(answer)){
      e.preventDefault();e.stopImmediatePropagation();if(r.resolved)return;r.resolved=true;
      r.confirmResolve(answer.dataset.foConfirm==='yes');finish(r,'answer');return;
    }
    const dismiss=e.target.closest('[data-fo-dismiss],[data-close-modal],[data-checkout-edit],[data-crm-sheet-close]');
    if(r.modal&&(e.target===r.root||dismiss&&r.panel.contains(dismiss))){e.preventDefault();e.stopImmediatePropagation();void requestClose(r);return;}
    if(!r.modal&&!r.panel.contains(e.target)&&!r.anchor.contains(e.target)){void requestClose(r,'outside',false);}
    else if(!r.modal&&r.panel.contains(e.target)&&e.target.closest('[role=menuitem],[data-fo-menu-action]'))setTimeout(()=>{if(!r.closed)void requestClose(r,'select',false);},0);
  },true);
  document.addEventListener('keydown',e=>{
    if(['Tab','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','Escape'].includes(e.key)||!e.target.matches('input,textarea,[contenteditable]')&&['Enter',' '].includes(e.key)){modality='keyboard';document.documentElement.dataset.foModality=modality;}
    if(e.defaultPrevented||e.isComposing)return;
    if(e.target.matches('.fo-tab')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
      const buttons=[...e.target.closest('.fo-tabs').querySelectorAll(':scope>button.fo-tab')].filter(n=>!n.disabled),index=buttons.indexOf(e.target);
      e.preventDefault();const next=e.key==='Home'?buttons[0]:e.key==='End'?buttons.at(-1):buttons[(index+(e.key==='ArrowLeft'?-1:1)+buttons.length)%buttons.length];focus(next);next?.scrollIntoView({block:'nearest',inline:'nearest',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});return;
    }
    const trigger=e.target.closest('[data-fo-menu-trigger]');
    if(trigger&&['ArrowDown','ArrowUp'].includes(e.key)&&!records.some(r=>r.anchor===trigger)) {e.preventDefault();trigger.click();if(e.key==='ArrowUp')queueMicrotask(()=>{const r=top();if(r?.anchor===trigger)focus([...r.panel.querySelectorAll('[role=menuitem]')].filter(n=>!n.disabled).at(-1));});return;}
    const r=top();if(!r)return;
    if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();void requestClose(r);return;}
    if(r.kind==='menu'&&['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
      e.preventDefault();const nodes=[...r.panel.querySelectorAll('[role=menuitem]')].filter(n=>visible(n)&&!n.disabled),index=nodes.indexOf(document.activeElement);
      focus(e.key==='Home'?nodes[0]:e.key==='End'?nodes.at(-1):nodes[(index+(e.key==='ArrowUp'?-1:1)+nodes.length)%nodes.length]);return;
    }
    if(r.kind==='menu'&&e.key.length===1&&!e.ctrlKey&&!e.metaKey&&!e.altKey){
      const nodes=[...r.panel.querySelectorAll('[role=menuitem]')].filter(n=>visible(n)&&!n.disabled),index=nodes.indexOf(document.activeElement);
      const ordered=nodes.slice(index+1).concat(nodes.slice(0,index+1)),letter=e.key.toLocaleLowerCase();
      const next=ordered.find(n=>n.textContent.trim().toLocaleLowerCase().startsWith(letter));
      if(next){e.preventDefault();focus(next);}return;
    }
    if(e.key==='Tab'){
      if(r.kind==='menu'){e.preventDefault();leavePopup(r,e.shiftKey);return;}
      const nodes=tabs(r.panel),first=nodes[0],last=nodes.at(-1);
      if(r.modal){if(!nodes.length){e.preventDefault();focus(r.panel);}else if(e.shiftKey&&document.activeElement===first||!e.shiftKey&&document.activeElement===last||!r.panel.contains(document.activeElement)){e.preventDefault();focus(e.shiftKey?last:first);}}
      else if(e.shiftKey&&document.activeElement===first||!e.shiftKey&&document.activeElement===last){e.preventDefault();leavePopup(r,e.shiftKey);}
    }
  },true);
  document.addEventListener('toggle',e=>{
    const details=e.target;if(!details.matches?.('details[data-fo-details]'))return;
    const r=records.find(r=>r.details===details);
    if(details.open&&!r){const panel=details.querySelector('[data-fo-popup]');if(panel)popup(panel,details.querySelector('summary'),{details,kind:panel.dataset.foPopup});}
    else if(!details.open&&r){detach(r);effects();}
  },true);
  // Explicit render-boundary adoption, never a DOM observer or a second event lifecycle.
  const controls=root=>window.FreemanComponents.adopt(root);
  function markup(html,decorate){const t=document.createElement('template');t.innerHTML=html;controls(t.content);decorate?.(t.content);return t.innerHTML;}
  function enhance(fn,getRoot){return function(...args){const done=()=>controls(typeof getRoot==='function'?getRoot():getRoot);try{const result=fn.apply(this,args);if(result?.then)return result.then(value=>{done();return value},error=>{done();throw error});done();return result}catch(error){done();throw error}};}


  // Support photos stay on the authenticated same-origin route, inside the chat.
  document.addEventListener('click',event=>{
    const opener=event.target.closest('[data-chat-photo]');if(!opener)return;
    event.preventDefault();event.stopPropagation();
    let url;try{url=new URL(opener.dataset.chatPhoto,location.origin)}catch{return}
    if(url.origin!==location.origin||!/^\/api\/web\/support\/(?:attachments\/\d+|messages\/\d+\/media\/photo)$/.test(url.pathname)||url.search||url.hash)return;
    if(document.querySelector('.fo-chat-photo-overlay'))return;
    const layer=document.createElement('div');layer.className='fo-overlay fo-scope fo-chat-photo-overlay';layer.dataset.foOverlay='';
    layer.innerHTML='<section class="fo-dialog fo-chat-photo-dialog" data-fo-panel aria-label="Просмотр фотографии"><header><span>Фотография</span><button type="button" class="fo-button fo-icon-button" data-photo-download aria-label="Скачать оригинал">'+FreemanComponents.icon('download')+'</button><button type="button" class="fo-button fo-icon-button" data-photo-out aria-label="Уменьшить">−</button><button type="button" class="fo-button" data-photo-reset aria-label="Масштаб 100%">100%</button><button type="button" class="fo-button fo-icon-button" data-photo-in aria-label="Увеличить">＋</button><button type="button" class="fo-button fo-icon-button" data-fo-dismiss data-fo-initial-focus aria-label="Закрыть фото">×</button></header><div class="fo-chat-photo-stage"><img draggable="false" alt=""></div><p role="status">Загружаем фотографию…</p></section>';
    const image=layer.querySelector('img'),stage=layer.querySelector('.fo-chat-photo-stage'),status=layer.querySelector('[role=status]'),reset=layer.querySelector('[data-photo-reset]');let zoom=1;
    function localPoint(touch){const box=stage.getBoundingClientRect();return {x:touch.clientX-box.left,y:touch.clientY-box.top}}
    function scale(value,point={x:stage.clientWidth/2,y:stage.clientHeight/2},anchor){
      const previous=zoom;anchor=anchor||{x:(stage.scrollLeft+point.x)/previous,y:(stage.scrollTop+point.y)/previous};
      zoom=Math.max(1,Math.min(4,value));image.style.width=zoom*100+'%';image.style.height=zoom*100+'%';
      stage.scrollLeft=zoom===1?0:anchor.x*zoom-point.x;stage.scrollTop=zoom===1?0:anchor.y*zoom-point.y;
      const percent=Math.round(zoom*100);reset.textContent=percent+'%';reset.setAttribute('aria-label','Масштаб '+percent+'%');layer.querySelector('[data-photo-out]').disabled=zoom===1;layer.querySelector('[data-photo-in]').disabled=zoom===4;
    }
    let gesture=null,lastTap=0,tap=null;
    function begin(touches){
      tap=null;
      if(touches.length>=2){const a=localPoint(touches[0]),b=localPoint(touches[1]),point={x:(a.x+b.x)/2,y:(a.y+b.y)/2};gesture={kind:'pinch',distance:Math.max(1,Math.hypot(a.x-b.x,a.y-b.y)),zoom,anchor:{x:(stage.scrollLeft+point.x)/zoom,y:(stage.scrollTop+point.y)/zoom}};}
      else if(touches.length===1){const p=localPoint(touches[0]);gesture={kind:'pan',point:p,left:stage.scrollLeft,top:stage.scrollTop};tap={point:p,at:performance.now()};}
      else gesture=null;
    }
    stage.addEventListener('touchstart',e=>{if(stage.dataset.photoReady!=='true')return;if(e.touches.length>1||zoom>1)e.preventDefault();begin(e.touches)},{passive:false});
    stage.addEventListener('touchmove',e=>{
      if(!gesture)return;e.preventDefault();tap=null;
      if(e.touches.length>=2){if(gesture.kind!=='pinch'){begin(e.touches);return}const a=localPoint(e.touches[0]),b=localPoint(e.touches[1]);scale(gesture.zoom*Math.hypot(a.x-b.x,a.y-b.y)/gesture.distance,{x:(a.x+b.x)/2,y:(a.y+b.y)/2},gesture.anchor);}
      else if(e.touches.length===1&&gesture.kind==='pan'){const p=localPoint(e.touches[0]);stage.scrollLeft=gesture.left+gesture.point.x-p.x;stage.scrollTop=gesture.top+gesture.point.y-p.y;}
    },{passive:false});
    stage.addEventListener('touchend',e=>{if(e.touches.length){begin(e.touches);return}if(tap&&performance.now()-tap.at<250){const time=performance.now();if(time-lastTap<300){e.preventDefault();scale(zoom===1?2:1,tap.point);lastTap=0}else lastTap=time}tap=null;gesture=null},{passive:false});
    stage.addEventListener('touchcancel',()=>{gesture=null;tap=null;lastTap=0});
    // Older Safari exposes gesture events in addition to touch events. Scope
    // cancellation to the image canvas, never the page or viewport metadata.
    for(const type of ['gesturestart','gesturechange','gestureend'])stage.addEventListener(type,e=>e.preventDefault(),{passive:false});
    image.onload=()=>{if(!status.dataset.photoViewState)status.textContent='';scale(1);stage.dataset.photoReady='true'};image.onerror=()=>{if(!status.dataset.photoViewState)status.textContent='Не удалось загрузить фото. Закройте его и попробуйте ещё раз.'};
    image.alt=opener.querySelector('img')?.alt||'Фото из переписки';
    const disposePhoto=window.FreemanChatMedia?.viewer({image,stage,status,url:url.pathname,download:layer.querySelector('[data-photo-download]'),preview:opener.querySelector('img')?.src});
    if(!disposePhoto)image.src=url.pathname;
    layer.addEventListener('click',e=>{if(e.target.closest('[data-photo-in]'))scale(zoom+.5);if(e.target.closest('[data-photo-out]'))scale(zoom-.5);if(e.target.closest('[data-photo-reset]')){scale(1);stage.scrollTo(0,0)}});
    stage.addEventListener('dblclick',()=>scale(zoom===1?2:1));
    document.body.append(layer);controls(layer);const record=mount(layer,{opener:bookmark(opener),onClose:()=>{disposePhoto?.();layer.remove()}});
    if(!matchMedia('(prefers-reduced-motion: reduce)').matches){const panel=layer.querySelector('[data-fo-panel]'),from=opener.getBoundingClientRect(),to=panel.getBoundingClientRect();record.animation?.cancel();record.animation=panel.animate([{opacity:.4,transform:`translate(${Math.max(-160,Math.min(160,from.x+from.width/2-to.x-to.width/2))}px,${Math.max(-160,Math.min(160,from.y+from.height/2-to.y-to.height/2))}px) scale(.72)`},{opacity:1,transform:'translate(0,0) scale(1)'}],{duration:280,easing:'cubic-bezier(.2,.75,.25,1)'});}
  },true);
  function chatSheet({title,content,kind='actions',classes='',opener}){
    let host=document.getElementById('fo-chat-sheet-host');if(!host){host=document.createElement('div');host.id='fo-chat-sheet-host';document.body.append(host)}
    const previous=hosts.get(host)?.record;if(previous&&!previous.closed)finish(previous,'replace',false);
    if(opener)remember(opener);
    const E=FreemanComponents.escape;
    const record=render(host,`<div class="fo-overlay fo-overlay--sheet fo-chat-sheet-overlay fo-scope" data-fo-overlay><section class="fo-dialog fo-chat-sheet ${E(classes)}" data-fo-panel data-sheet-kind="${E(kind)}" aria-labelledby="fo-chat-sheet-title"><span class="fo-chat-sheet-handle" aria-hidden="true"></span><header class="fo-dialog-header"><h2 id="fo-chat-sheet-title">${E(title)}</h2><button type="button" class="fo-button fo-icon-button fo-button--flat" data-fo-dismiss aria-label="Закрыть">${FreemanComponents.icon('close')}</button></header><div class="fo-chat-sheet-content">${content}</div></section></div>`,()=>record.root.remove());
    if(kind==='replies') enhanceQuickSheet(record);
    return record;
  }
  function enhanceQuickSheet(r){
    const panel=r.panel,body=panel.querySelector('.fo-chat-sheet-content'),list=body.querySelector('.ai-quick-list'),reduce=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!list)return;
    let search=body.querySelector('input[type=search]');
    if(!search){const label=document.createElement('label');label.className='fo-chat-sheet-search';label.innerHTML=FreemanComponents.icon('search')+'<input class="fo-input" type="search" placeholder="Поиск ответа" aria-label="Поиск быстрого ответа">';body.prepend(label);search=label.querySelector('input')}
    search.closest('label')?.classList.add('fo-chat-sheet-search');
    const empty=document.createElement('p');empty.className='fo-quick-empty';empty.setAttribute('role','status');empty.hidden=true;empty.textContent='Ничего не найдено';body.append(empty);
    search.addEventListener('input',()=>{const query=search.value.trim().toLocaleLowerCase();let visible=0;for(const row of list.querySelectorAll('.fo-quick-reply')){row.hidden=!row.textContent.toLocaleLowerCase().includes(query);if(!row.hidden)visible++}empty.hidden=visible>0;list.scrollTop=0});
    const grab=panel.querySelector('.fo-chat-sheet-handle');grab.dataset.quickGrab='';grab.setAttribute('role','button');grab.tabIndex=0;grab.setAttribute('aria-label','Закрыть быстрые ответы');grab.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();void requestClose(r)}});
    let motion,drag=null;
    const animate=(from,to,duration)=>{motion?.cancel();panel.style.transform='';if(reduce()){panel.style.transform=to;return null}motion=panel.animate([{transform:from},{transform:to}],{duration,easing:'cubic-bezier(.22,.8,.25,1)',fill:'forwards'});return motion};
    r.animation?.cancel();animate('translateY(70px)','translateY(0)',340);
    const prior=r.onClose;r.onClose=(...args)=>{r.root.style.pointerEvents='none';const from=getComputedStyle(panel).transform;motion?.cancel();if(reduce()){prior?.(...args);return}const exit=animate(from,'translateY(calc(100% + 24px))',230);exit.finished.then(()=>prior?.(...args),()=>prior?.(...args))};
    const header=panel.querySelector('.fo-dialog-header');
    const down=e=>{if(e.button!==0||e.target.closest('button,input'))return;const matrix=new DOMMatrixReadOnly(getComputedStyle(panel).transform);motion?.cancel();drag={id:e.pointerId,y:e.clientY,start:matrix.m42,at:performance.now(),last:e.clientY,lastAt:performance.now(),v:0,delta:matrix.m42};e.currentTarget.setPointerCapture(e.pointerId);panel.style.transform='translateY('+matrix.m42+'px)'};
    const move=e=>{if(!drag||e.pointerId!==drag.id)return;const at=performance.now();drag.v=(e.clientY-drag.last)/Math.max(1,at-drag.lastAt);drag.last=e.clientY;drag.lastAt=at;drag.travel=Math.max(0,e.clientY-drag.y);drag.delta=Math.max(0,drag.start+e.clientY-drag.y);panel.style.transform='translateY('+drag.delta+'px)'};
    const end=e=>{if(!drag||e.pointerId!==drag.id)return;const d=drag;drag=null;if(e.type!=='pointercancel'&&(d.travel>85||(d.travel>24&&d.v>.55)))void requestClose(r);else animate('translateY('+d.delta+'px)','translateY(0)',230)};
    for(const node of [grab,header]){node.addEventListener('pointerdown',down);node.addEventListener('pointermove',move);node.addEventListener('pointerup',end);node.addEventListener('pointercancel',end)}
    list.querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{if(!img.dataset.fallback){img.dataset.fallback='1';img.src='/assets/freeman-mark-20261029.png'}},{once:true}));
  }
  function replyPreview(reply,{onUse,onBack,label='Вставить ответ'}={}){
    const E=FreemanComponents.escape;
    const record=chatSheet({title:reply.title||'Быстрый ответ',kind:'reply-preview',content:'<article class="cm-reply-preview">'+FreemanComponents.messageBody({source:reply.text||'',media:(reply.attachments||[]).map(FreemanComponents.media).join('')})+'</article><footer class="cm-preview-actions"><button type="button" class="fo-button" data-preview-back>Назад</button><button type="button" class="fo-button" data-preview-use>'+E(label)+'</button></footer>'});
    record.root.addEventListener('click',e=>{if(e.target.closest('[data-preview-back]')){FreemanUI.close(record.root,{force:true});onBack?.()}if(e.target.closest('[data-preview-use]')){FreemanUI.close(record.root,{force:true});onUse?.()}});return record;
  }
  function attachments({files,camera,opener}){
    if(!files?.isConnected||files.disabled)return;
    const icon=FreemanComponents.icon;
    const record=chatSheet({title:'Добавить вложение',kind:'attachments',opener,content:`<div class="fo-chat-attachment-actions"><button type="button" class="fo-row-action fo-chat-sheet-action" data-attachment-pick="files">${icon('image')}<span>Фото и видео<small>Из галереи или файлов</small></span>${icon('chevron-right')}</button>${camera?`<button type="button" class="fo-row-action fo-chat-sheet-action" data-attachment-pick="camera">${icon('camera')}<span>Сделать фото<small>Открыть камеру</small></span>${icon('chevron-right')}</button>`:''}</div><p class="fo-chat-sheet-note">До 6 файлов · до 20 МБ каждый</p>`});
    record.root.addEventListener('click',event=>{const choice=event.target.closest('[data-attachment-pick]');if(!choice)return;const input=choice.dataset.attachmentPick==='camera'?camera:files;if(!input?.isConnected||input.disabled)return;window.FreemanUI.close(record.root,{force:true});input.click()});
    return record;
  }
  window.FreemanUI={replyPreview,popover:popup,anchorPanel,chatSheet,attachments,openDialog,closeDialog,keyboard:()=>modality==='keyboard',controls,markup,enhance,remember,beforeRender,render,mount,sync,confirm,inform,prompt,busy,setBusy,
    dismiss:node=>requestClose(find(node)),
    close:(node,{force=false,restoreFocus=true}={})=>{const r=find(node);if(force&&r)finish(r,'commit',restoreFocus);else return requestClose(r,'dismiss',restoreFocus);},
    diagnostics:()=>({overlays:records.length,locked:!!locked,observing:!!observer})};
})();
