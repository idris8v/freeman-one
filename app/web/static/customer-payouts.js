/* Customer payout observations. Financial mutations remain server-side only. */
const CustomerPayouts=(()=>{
 let pending=false,stopped=false,timer;
 const memory=new Map();
 const owner=()=>state.data?.profile&&!state.data.profile.is_admin&&!isPreview()?String(state.data.profile.freeman_id||state.data.profile.id||''):'';
 const storage=id=>'freeman-payouts-seen:'+id;
 function seen(id){try{const a=JSON.parse(localStorage.getItem(storage(id))||'[]');return [...new Set([...(Array.isArray(a)?a:[]),...(memory.get(id)||[])])]}catch{return memory.get(id)||[]}}
 function show(items,id){
  if(document.hidden||owner()!==id)return;
  const before=seen(id),fresh=items.find(n=>!before.includes(n.id)&&!noticeIsRead(n));
  if(!fresh)return;
  const ids=[...new Set([...items.map(n=>n.id),...before])].slice(0,300);memory.set(id,ids);try{localStorage.setItem(storage(id),JSON.stringify(ids))}catch{}
  document.querySelector('.payout-toast')?.remove();
  const node=document.createElement('aside');node.className='digest-toast payout-toast fo-scope';node.setAttribute('role','status');node.setAttribute('aria-live','polite');
  node.innerHTML=`<button class="digest-toast-open" data-payout-toast-open><span class="digest-toast-icon">${svg('bell')}</span><span><small>FREEMAN ONE · ВЫВОД СРЕДСТВ</small><strong>${esc(fresh.title)}</strong><span>${esc(fresh.text)}</span></span></button><button class="fo-button fo-icon-button" data-payout-toast-dismiss aria-label="Скрыть уведомление">${actionIcon('close')}</button>`;
  document.body.append(node);setTimeout(()=>node.remove(),5000);
 }
 function schedule(){clearTimeout(timer);if(!stopped)timer=setTimeout(poll,20000)}
 async function poll(){
  clearTimeout(timer);const id=owner();if(stopped)return;if(!id||document.hidden||pending){schedule();return}
  pending=true;const before=state.extra.balance,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
  try{
   const r=await fetch('/api/web/balance',{cache:'no-store',signal:controller.signal});if(!r.ok)return;
   const x=await r.json();if(owner()!==id||stopped||state.extra.balance!==before)return;
   if(!Number.isSafeInteger(x.balance)||!Array.isArray(x.withdrawals)||!Array.isArray(x.withdrawal_notifications))return;
   const notices=x.withdrawal_notifications;if(!notices.every(n=>typeof n.id==='string'&&/^payout-\d+-(paid|rejected)$/.test(n.id)&&typeof n.title==='string'&&typeof n.text==='string'&&n.target==='balance'))return;
   const changed=state.data.balance!==x.balance||JSON.stringify(state.extra.payoutNotifications)!==JSON.stringify(notices)||JSON.stringify(before?.withdrawals)!==JSON.stringify(x.withdrawals);
   state.extra.payoutNotificationsOwner=id;state.extra.payoutNotifications=notices;syncWithdrawalBalance(x);NoticeCenter.badge();
   if(changed){if(['profile','balance','referrals'].includes(state.view)&&!state.modal&&!state.busy&&!document.activeElement?.matches('input,textarea,select'))render();else updateNavigation()}
   if(navigator.locks)await navigator.locks.request(storage(id),()=>show(notices,id));else show(notices,id);
  }catch{/* Keep the last confirmed state on failed or aborted requests. */}
  finally{clearTimeout(timeout);pending=false;schedule()}
 }
 document.addEventListener('click',e=>{const b=e.target.closest('[data-payout-toast-open],[data-payout-toast-dismiss]');if(!b)return;document.querySelector('.payout-toast')?.remove();if(b.hasAttribute('data-payout-toast-open'))loadView('balance')});
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});window.addEventListener('focus',poll);
 window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer)});window.addEventListener('pageshow',()=>{stopped=false;poll()});
 setTimeout(poll,1000);return{poll};
})();
