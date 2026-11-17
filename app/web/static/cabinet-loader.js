/* Load the authenticated application once, in its existing classic-script order.
   Templates are inert: signing in never downloads account/admin/editor code. */
window.FreemanCabinetBoot=(()=>{
 let account,ready,editor;
 const checkoutChanges=new Set();
 const changed=event=>{if(event.key===null||event.key.startsWith('freeman-checkout:'))checkoutChanges.add(event.key)};
 window.addEventListener('storage',changed);
 function scripts(id){return [...document.querySelector(id).content.querySelectorAll('script[src]')];}
 function load(items){return Promise.all(items.map(source=>new Promise((resolve,reject)=>{
  const script=document.createElement('script');script.async=false;script.src=source.src;
  script.onload=resolve;script.onerror=()=>{script.remove();reject(Error('Не удалось загрузить интерфейс. Обновите страницу.'))};
  document.body.append(script);
 })));}
 function failure(){const root=document.querySelector('#app');root.innerHTML='<section class="login-shell"><div class="login-card"><h1>Не удалось открыть сайт</h1><p>Проверьте соединение и попробуйте ещё раз.</p><button type="button" class="fo-button" data-startup-retry>Повторить</button></div></section>';root.querySelector('[data-startup-retry]').onclick=()=>location.reload();}
 async function guestInvitation(){
  const token=new URL(location.href).searchParams.get('gift');
  if(!/^[A-Za-z0-9_-]{32}$/.test(token||''))return false;
  sessionStorage.setItem('freeman-gift-invitation',token);
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const response=await fetch('/api/web/gift-invitation/'+token,{cache:'no-store'});const gift=await response.json();
  if(!response.ok)sessionStorage.removeItem('freeman-gift-invitation');
  const root=document.querySelector('#app');document.body.classList.add('shop-guest');
  root.innerHTML=`<section class="fo-scope fo-card fo-gift-invitation"><h1>${response.ok?'Вам подарок!':'Подарок недоступен'}</h1><p>${response.ok?`VPN-подписка · ${escape(gift.period)}<br>Устройств: ${Number(gift.devices)}`:escape(gift.detail||'Приглашение больше не действует.')}</p>${response.ok?`<p class="shop-note">Войдите в свой аккаунт и подтвердите получение. Срок начнётся после получения.</p><button class="fo-button fo-button--success fo-button--compact" data-gift-login>Войти и получить</button><a class="fo-button fo-button--compact" href="${escape(gift.telegram_url)}" rel="noreferrer noopener">Получить в Telegram</a>`:'<button class="fo-button" data-gift-login>На главную</button>'}</section>`;
  root.querySelector('[data-gift-login]').onclick=()=>{document.body.classList.remove('shop-guest');if(!response.ok){const url=new URL(location.href);url.searchParams.delete('gift');history.replaceState(null,'',url)}FreemanAuth.show()};return true;
 }
 async function start(){
  if(['127.0.0.1','localhost'].includes(location.hostname)&&new URLSearchParams(location.search).has('preview')){
   ready=load(scripts('#cabinet-modules'));await ready;return;
  }
  const response=await fetch('/api/web/me',{cache:'no-store',signal:AbortSignal.timeout(20000)});
  if(response.status===401){if(await guestInvitation())return;FreemanAuth.show();return;}
  if(!response.ok)throw Error('profile');account=await response.json();
  ready=load(scripts('#cabinet-modules'));await ready;
 }
 // Run after this synchronous initializer returns, so app.js can await ready.
 queueMicrotask(()=>start().catch(failure).finally(()=>window.removeEventListener('storage',changed)));
 return {get account(){return account},get ready(){return ready},
 get checkoutChanged(){return checkoutChanges.has(null)||checkoutChanges.has('freeman-checkout:'+account?.profile?.freeman_id)},loadEditor(){
  if(!editor)editor=load(scripts('#publication-editor')).catch(error=>{editor=null;throw error});
  return editor;
 }};
})();
