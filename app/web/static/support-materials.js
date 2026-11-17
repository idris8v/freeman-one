/* Shared, read-only support guide presentation. Trusted data; DOM text is escaped. */
const E=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const styleURL='/assets/support-materials.css?v=922390b5af6c';
function paragraph(line){return E(line).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')}
export function markup(data){
 const video=['/api/web/support/materials/appstore-region/video','/api/support-agent/materials/appstore-region/video'].includes(data.video?.url)?FreemanComponents.media({kind:'video',url:data.video.url,name:data.video.name||'Как сменить регион App Store'}):'';
 return '<article class="sm-article cm-reply-preview cm-guide-bubble">'+FreemanComponents.messageBody({source:data.text||'',media:video})+'<p class="sm-video-error" role="status" hidden>Видео временно недоступно. Текстовая инструкция ниже поможет пройти те же шаги.</p><p class="sm-status" aria-live="polite"></p></article>';
}
export async function open({back,base='/api/web/support/materials/appstore-region'}={}){
 if(!/^\/api\/(?:web\/support|support-agent)\/materials\/(?:appstore-region|key-install)$/.test(base))throw Error('Недоступная инструкция');
 if(!document.querySelector('link[data-sm-style]')){const link=document.createElement('link');link.rel='stylesheet';link.href=styleURL;link.dataset.smStyle='';document.head.append(link)}
 const record=FreemanUI.chatSheet({title:base.endsWith('/key-install')?'Как добавить подписку':'Как скачать приложение на iPhone',kind:'guide',classes:'sm-sheet',content:'<p class="sm-loading" role="status">Открываем инструкцию…</p>'}),host=record.root.querySelector('.fo-chat-sheet-content');let controller;
 const close=()=>{record.root.querySelectorAll('video').forEach(v=>{v.pause();v.removeAttribute('src');v.load()});controller?.abort();FreemanUI.close(record.root,{force:true})};
 // Both the header close control and framework dismissal stop media on removal.
 const oldClose=record.onClose;record.onClose=(...args)=>{controller?.abort();record.root.querySelectorAll('video').forEach(v=>{v.pause();v.removeAttribute('src');v.load()});oldClose?.(...args)};
 async function load(){controller?.abort();controller=new AbortController();host.innerHTML='<p class="sm-loading" role="status">Открываем инструкцию…</p>';
 try{const response=await fetch(base,{credentials:'same-origin',cache:'no-store',signal:controller.signal});if(!response.ok)throw Error(response.status===401?'Войдите в аккаунт, чтобы открыть инструкцию.':'Инструкция временно недоступна. Попробуйте ещё раз.');const data=await response.json();if(!record.root.isConnected)return;
 host.innerHTML=markup(data)+`<footer class="sm-footer"><button type="button" class="fo-button" data-sm-back>К вопросам</button></footer>`;FreemanUI.controls(host);host.querySelector('video')?.addEventListener('error',()=>{const err=host.querySelector('.sm-video-error');if(err)err.hidden=false});
 }catch(error){if(error.name==='AbortError'||!record.root.isConnected)return;host.innerHTML=`<p class="sm-error" role="alert">${E(error.message)}</p><button type="button" class="fo-button" data-sm-retry>Повторить</button>`}}
 record.root.addEventListener('click',async e=>{if(e.target.closest('[data-sm-retry]'))return load();if(e.target.closest('[data-sm-back]')){close();back?.();return}const button=e.target.closest('[data-sm-copy]');if(!button||button.disabled)return;button.disabled=true;const status=host.querySelector('.sm-status');try{await navigator.clipboard.writeText(button.dataset.smCopy);button.querySelector('[data-sm-copy-icon]').textContent='✓';status.textContent='Скопировано';setTimeout(()=>{if(button.isConnected)button.querySelector('[data-sm-copy-icon]').textContent='⧉'},1600)}catch{status.textContent='Не удалось скопировать автоматически. Выделите нужное значение и скопируйте его.'}finally{button.disabled=false}});
 await load();return record;
}
