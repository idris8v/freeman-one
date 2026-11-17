/* Private chat photos: bounded previews, explicit cancellation and original download. */
(()=>{'use strict';if(window.FreemanChatMedia)return;
 const states=new WeakMap(),live=new Set();
 const safe=value=>{try{const u=new URL(value,location.origin);return u.origin===location.origin&&!u.search&&!u.hash&&/^\/api\/web\/support\/(?:attachments\/[1-9]\d*|messages\/[1-9]\d*\/media\/photo)$/.test(u.pathname)?u.pathname:null}catch{return null}};
 const symbol=state=>state==='loading'?'<span class="fo-photo-spinner" aria-hidden="true"></span><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="8" height="8" rx="1.5" fill="currentColor"/></svg>':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 4v12m-4-4 4 4 4-4M5 19h14"/></svg>';
 function paint(s,state){s.state=state;s.frame.dataset.photoState=state;const loading=state==='loading';s.control.setAttribute('aria-label',loading?'Остановить загрузку фото':state==='error'?'Повторить загрузку фото':'Загрузить фото');s.control.innerHTML=symbol(state);s.frame.setAttribute('aria-busy',String(loading));s.label.textContent=({loading:'Загружаем фото',stopped:'Загрузка остановлена',error:'Не удалось загрузить. Повторить',ready:''})[state];}
 async function load(s){
  if(!s.frame.isConnected)return;
  s.controller?.abort();const controller=new AbortController();s.controller=controller;paint(s,'loading');
  try{const response=await fetch(s.url+'?preview=1',{credentials:'same-origin',signal:controller.signal,cache:'no-store'});if(!response.ok)throw Error('preview');const blob=await response.blob();if(blob.size>2*1024*1024)throw Error('size');if(controller.signal.aborted||!s.frame.isConnected)return;const url=URL.createObjectURL(blob);const probe=new Image();probe.src=url;try{await probe.decode()}catch{URL.revokeObjectURL(url);throw Error('decode')}
   if(controller.signal.aborted||!s.frame.isConnected){URL.revokeObjectURL(url);return}if(s.object)URL.revokeObjectURL(s.object);s.object=url;s.image.src=url;s.frame.style.aspectRatio=probe.naturalWidth+'/'+probe.naturalHeight;paint(s,'ready');
  }catch(e){if(s.controller!==controller)return;if(e.name!=='AbortError'&&s.frame.isConnected)paint(s,'error')}
 }
 function stop(s){s.controller?.abort();paint(s,'stopped')}
 const observer='IntersectionObserver'in window?new IntersectionObserver(entries=>{for(const e of entries)if(e.isIntersecting){observer.unobserve(e.target);const s=states.get(e.target);if(s&&s.state==='idle')begin(s)}},{rootMargin:'180px'}):null;
 function begin(s){if(s.image.complete){void load(s);return}paint(s,'loading');s.image.addEventListener('load',()=>{if(s.state==='loading')void load(s)},{once:true});s.image.addEventListener('error',()=>{if(s.state==='loading')void load(s)},{once:true});}
 function enhance(root=document){
  for(const s of live)if(!s.frame.isConnected){s.controller?.abort();if(s.object)URL.revokeObjectURL(s.object);observer?.unobserve(s.frame);live.delete(s)}
  for(const frame of root.querySelectorAll('[data-photo-frame]')){if(states.has(frame)||!frame.isConnected)continue;const url=safe(frame.dataset.photoFrame);if(!url)continue;const s={frame,url,image:frame.querySelector('img'),control:frame.querySelector('[data-photo-transfer]'),label:frame.querySelector('[data-photo-status]'),state:'idle',controller:null,object:null};if(!s.image||!s.control||!s.label)continue;states.set(frame,s);live.add(s);
   const ratio=()=>{if(s.image.naturalWidth)s.frame.style.aspectRatio=s.image.naturalWidth+'/'+s.image.naturalHeight};s.image.addEventListener('load',ratio);if(s.image.complete)ratio();
   s.control.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();if(s.state==='loading')stop(s);else void load(s)});if(observer)observer.observe(frame);else begin(s);
  }
 }
 function viewer({image,stage,status,url,download,preview}){
  let controller=null,object=null,disposed=false,loaded=false,pending=null;
  const text=document.createElement('span'),toggle=document.createElement('button');toggle.type='button';toggle.className='fo-button fo-photo-view-transfer';status.replaceChildren(text,toggle);image.src=preview||'';
  function state(value){status.dataset.photoViewState=value;text.textContent=({loading:'Загружаем оригинал…',stopped:'Загрузка остановлена',ready:'',error:'Не удалось загрузить оригинал'})[value];toggle.textContent=value==='loading'?'Остановить':'Повторить';toggle.hidden=value==='ready';download.disabled=value==='loading';}
  async function start(){if(loaded||disposed)return loaded;if(pending)return pending;const active=new AbortController();controller=active;state('loading');
   pending=(async()=>{try{const r=await fetch(url,{credentials:'same-origin',signal:active.signal,cache:'no-store'});if(!r.ok)throw Error('download');const b=await r.blob();if(b.size>20*1024*1024)throw Error('size');if(disposed||active.signal.aborted)return false;const next=URL.createObjectURL(b),probe=new Image();probe.src=next;try{await probe.decode()}catch{URL.revokeObjectURL(next);throw Error('decode')}if(disposed||active.signal.aborted){URL.revokeObjectURL(next);return false}object=next;image.src=object;if(!matchMedia('(prefers-reduced-motion: reduce)').matches)image.animate([{opacity:.65,filter:'blur(3px)'},{opacity:1,filter:'blur(0)'}],{duration:240,easing:'ease-out'});loaded=true;download.dataset.filename='Фото.'+(b.type==='image/png'?'png':b.type==='image/webp'?'webp':b.type==='image/gif'?'gif':'jpg');state('ready');return true}catch(e){if(!disposed&&controller===active)state(e.name==='AbortError'?'stopped':'error');return false}finally{pending=null}})();return pending;
  }
  toggle.addEventListener('click',()=>{if(pending)controller?.abort();else void start()});
  download.addEventListener('click',async()=>{if(await start()){const a=document.createElement('a');a.href=object;a.download=download.dataset.filename||'Фото';document.body.append(a);a.click();a.remove()}});
  void start();return ()=>{disposed=true;controller?.abort();if(object)URL.revokeObjectURL(object)};
 }

 window.addEventListener('pagehide',()=>{for(const s of live){s.controller?.abort();if(s.object)URL.revokeObjectURL(s.object)}live.clear()});
 window.FreemanChatMedia={enhance,viewer};enhance();
})();
