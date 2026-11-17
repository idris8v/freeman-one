/* Existing dialog/button components; all prices and access checks are server-owned. */
(()=>{'use strict';let flow=null;
const current=f=>flow===f&&state.data?.profile?.freeman_id===f.user&&document.querySelector('[data-antig-dialog]');
const storage=f=>'freeman-antig:'+f.user+':'+f.key;
async function request(f,path,body){const r=await fetch('/api/web/keys/'+f.key+'/antig/'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(60000),headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const x=await r.json().catch(()=>({}));if(!r.ok)throw Error(typeof x.detail==='string'?x.detail:'Сервер не подтвердил подключение. Повторите проверку.');return x}
function draw(f){const x=f.data,s=x?.status;let body='<p role="status">Рассчитываем доплату…</p>',buttons='';
 if(x){if(s==='done')body='<p class="fo-feedback" role="status">Антиглушилка подключена. Ссылка, устройства и дата окончания сохранены.</p>';
 else{body=`<p class="fo-dialog-description">Антиглушилка добавляет в вашу подписку дополнительный сервер для подключения при ограничениях интернета.</p><section class="fo-antig-feature"><span aria-hidden="true">${actionIcon('server')}</span><div><h3>Дополнительный сервер</h3><p>Для подключения при ограничениях интернета, в том числе во время беспилотной опасности.</p></div></section><section class="fo-antig-duration"><span aria-hidden="true">${actionIcon('calendar')}</span><div>Антиглушилка действует до окончания основной подписки. Доплата рассчитывается только за оставшийся срок.<small>До ${esc(shortDate(x.expires_at))} · срок основной подписки не меняется</small></div></section><section class="fo-antig-total"><div><span>ДОПЛАТА ЗА ОСТАВШИЙСЯ СРОК</span><strong>${x.admin?'Бесплатно':planRub(x.amount)}</strong></div><span class="fo-checkout-confirm-art" aria-hidden="true"><i></i><b>${actionIcon('bolt')}</b></span></section>`;
 if(s==='pending')body+='<p class="fo-feedback" role="status">Подключение ещё проверяется. Оплата зарезервирована; повторного списания не будет.</p>';
 if(s==='failed')body+='<p class="fo-feedback" role="alert">Панель не подтвердила подключение. Резерв возвращён на баланс. Можно повторить с новым расчётом.</p>';


 if(['ready','pending'].includes(s))buttons=`<button type="button" class="fo-button fo-button--success fo-button--compact" data-antig-apply>${s==='pending'?'Проверить подключение':x.admin?'Подключить бесплатно':'Оплатить →'}</button>`;
 else if(s==='funds')buttons='<button type="button" class="fo-button fo-button--success fo-button--compact" data-antig-fund>Оплатить →</button>';
 else buttons='<button type="button" class="fo-button fo-button--compact" data-antig-quote>Повторить</button>';
 }}
 state.modal=foDialogMarkup(`<div class="modal-backdrop"><section class="form-modal fo-transfer-dialog fo-upgrade-dialog fo-antig-dialog" data-antig-dialog role="dialog" aria-modal="true" aria-labelledby="antig-dialog-title"><header class="fo-antig-header"><span class="fo-antig-symbol" aria-hidden="true">${actionIcon('shield-check')}</span><div><h2 id="antig-dialog-title">${s==='done'?'Антиглушилка подключена':'Добавить антиглушилку'}</h2><p>${esc(x?.devices?x.devices+' '+deviceWord(x.devices)+(x.months?' · '+monthLabel(x.months):''):x?.name||'Ключ #'+f.key)}</p></div><button type="button" class="fo-button fo-icon-button" data-close-modal aria-label="Закрыть">${actionIcon('close')}</button></header><div class="fo-upgrade-body">${body}</div><p class="fo-feedback" data-antig-error role="alert"></p><footer><button type="button" class="fo-button fo-button--compact" data-close-modal>${s==='done'?'Готово':'Закрыть'}</button>${buttons}</footer></section></div>`);render();
 if(x?.token)try{if(s==='done'||s==='failed')sessionStorage.removeItem(storage(f));else sessionStorage.setItem(storage(f),x.token)}catch{}
}
async function open(key){const f={key,user:state.data.profile.freeman_id,busy:true};flow=f;draw(f);try{let token;try{token=sessionStorage.getItem(storage(f))}catch{}let x;if(token){try{x=await request(f,'status?token='+encodeURIComponent(token))}catch{try{sessionStorage.removeItem(storage(f))}catch{}}}if(!x||['expired','failed'].includes(x.status))x=await request(f,'quote',{});if(current(f)){f.data=x;draw(f)}}catch(e){if(current(f)){document.querySelector('[data-antig-error]').textContent=e.message;const retry=document.createElement('button');retry.className='fo-button fo-button--compact';retry.dataset.antigQuote='';retry.textContent='Повторить';document.querySelector('[data-antig-dialog] footer').append(retry)}}finally{f.busy=false}}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-key-antig],[data-antig-apply],[data-antig-quote],[data-antig-check],[data-antig-fund]');if(!b||b.disabled)return;if(b.dataset.keyAntig){e.preventDefault();await open(Number(b.dataset.keyAntig));return}const f=flow;if(!f||f.busy||!current(f))return;f.busy=true;b.disabled=true;const panel=document.querySelector('[data-antig-dialog]');FreemanUI.setBusy(panel,true);try{if(b.hasAttribute('data-antig-fund')){const bill=await request(f,'fund',{token:f.data.token,confirmed:true});if(current(f)&&/^https:\/\//.test(bill.url))location.assign(bill.url);return}const x=await request(f,b.hasAttribute('data-antig-apply')?'apply':b.hasAttribute('data-antig-check')?'check':'quote',b.hasAttribute('data-antig-quote')?{}:{token:f.data.token,confirmed:true});if(!current(f))return;f.data=x;if(x.status==='done'){if(x.admin){await loadAdminKeys();if(state.view==='admin-key'&&Number(state.extra.adminKey?.key?.id)===f.key)state.extra.adminKey.key.antig_active=true}else await refresh()}if(current(f))draw(f)}catch(e){if(current(f)&&/Расчёт устарел|Цена дополнения изменилась/.test(e.message)){try{f.data=await request(f,'quote',{});if(current(f)){draw(f);document.querySelector('[data-antig-error]').textContent='Цена обновлена. Проверьте сумму и подтвердите оплату.'}return}catch(problem){e=problem}}if(current(f)&&(e.name==='TimeoutError'||e instanceof TypeError)&&b.hasAttribute('data-antig-apply')){f.data.status='pending';draw(f)}if(current(f))document.querySelector('[data-antig-error]').textContent=e.name==='TimeoutError'?'Ответ задерживается. Нажмите «Проверить подключение» — повторной оплаты не будет.':e.message}finally{f.busy=false;if(panel.isConnected)FreemanUI.setBusy(panel,false);if(b.isConnected)b.disabled=false}});
const resume=Number(new URLSearchParams(location.search).get('antig_key'));if(Number.isSafeInteger(resume)&&resume>0){let observer;const restore=()=>{if(!state.data?.profile?.freeman_id)return;observer?.disconnect();const url=new URL(location.href);url.searchParams.delete('antig_key');history.replaceState(history.state,'',url);open(resume)};observer=new MutationObserver(restore);observer.observe(document.documentElement,{childList:true,subtree:true});restore();setTimeout(()=>observer.disconnect(),30000)}
let availabilityBusy=false;
async function syncAvailability(){
 if(availabilityBusy||document.hidden||!state.data?.profile||!['keys','purchase','plans','admin-keys','admin-key','admin-servers','admin-customer'].includes(state.view))return;
 availabilityBusy=true;const account=state.data.profile.freeman_id;
 try{const r=await fetch('/api/web/antig/availability',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)return;const x=await r.json();if(typeof x.enabled!=='boolean'||state.data?.profile?.freeman_id!==account)return;
 if(state.data.antig_addon_available!==x.enabled){state.data.antig_addon_available=x.enabled;if(state.extra.antig)state.extra.antig.enabled=x.enabled;if(!x.enabled)state.antig=false;
 if(!x.enabled&&document.querySelector('[data-antig-dialog]')&&flow?.data?.status!=='pending'){state.modal=null;flow=null}
 if(typeof renderInPlace==='function')renderInPlace();else render();}
 }catch{}finally{availabilityBusy=false}
}
setInterval(syncAvailability,15000);window.addEventListener('focus',syncAvailability);document.addEventListener('visibilitychange',syncAvailability);
})();

document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-admin-antig-disable]');
 if(!button||button.disabled)return;
 const id=Number(button.dataset.adminAntigDisable);
 button.disabled=true;
 try{
  if(!await FreemanUI.confirm({opener:button,title:'Отключить антиглушилку?',message:'Дополнительный сервер исчезнет. Срок подписки и лимит устройств сохранятся.',accept:'Отключить',danger:true}))return;
  const response=await fetch('/api/web/admin/keys/'+id+'/antig/disable',{
   method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true}),signal:AbortSignal.timeout(60000)
  });
  const data=await response.json();
  if(!response.ok||data.enabled!==false)throw Error(data.detail||'Панель не подтвердила отключение');
  if(state.view==='admin-key'&&Number(state.extra.adminKey?.key?.id)===id){
   state.extra.adminKey.key.antig_active=false;render();
  }
  say('Антиглушилка отключена');
 }catch(error){say(error.name==='TimeoutError'?'Отключение ещё не подтверждено. Повторите проверку в карточке.':error.message)}
 finally{if(button.isConnected)button.disabled=false}
});
