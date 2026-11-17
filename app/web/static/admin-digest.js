/* Daily owner notifications: one poller; no cached private API responses. */
function insightsPopular(tariffs){
 const rows=(tariffs||[]).map(x=>({...x}));
 for(const r of rows.filter(x=>!x.tariff_id)){const matches=rows.filter(x=>x.tariff_id&&x.kind===r.kind&&x.months===r.months&&x.devices===r.devices);if(matches.length===1){matches[0].sales+=r.sales;matches[0].revenue+=r.revenue;r.merged=true}}
 return rows.filter(x=>!x.merged).sort((a,b)=>b.sales-a.sales||b.revenue-a.revenue);
}
function insightsHighlights(d){const top=insightsPopular(d.tariffs)[0],renewals=d.stats.sales?Math.round(d.stats.renewals*100/d.stats.sales):0;
 return `<section class="insights-highlights" aria-label="Главное за период"><article class="analytics-card"><small>Самый популярный тариф</small><strong>${top?esc(top.name):'Пока нет продаж'}</strong><span>${top?`${top.sales} продаж · ${Math.round(top.sales/Math.max(1,d.stats.sales)*100)}% от всех`:'Появится после первой продажи'}</span></article><article class="analytics-card"><small>Клиенты и продления</small><strong>${d.stats.buyers} покупателей</strong><span>${d.stats.renewals} продлений · ${renewals}% продаж</span></article><article class="analytics-card"><small>Финансы отдельно</small><strong>Выручка ≠ баланс</strong><span>Здесь — динамика бизнеса. Деньги и операции — в балансе кабинета.</span></article></section>`;
}
function digestSettingsMarkup(){const supported='Notification'in window&&'serviceWorker'in navigator&&'PushManager'in window,permission=supported?Notification.permission:'unsupported';return `<section class="digest-settings analytics-card"><div><strong>Ежедневный отчёт · 00:00 МСК</strong><p>Итоги завершившегося дня сохраняются в уведомлениях. На iPhone для push добавьте сайт на экран «Домой». Переключатель управляет звуком на сайте; звук push — в настройках устройства.</p></div><div><button class="fo-button" data-digest-sound role="switch" aria-label="Звук уведомлений на сайте" aria-checked="${DigestUI.soundOn()}"><span class="fo-switch" aria-hidden="true"><b></b></span><span data-digest-sound-label>Звук: ${DigestUI.soundOn()?'включён':'выключен'}</span></button><button class="fo-button fo-button--primary" data-digest-push ${!supported||permission==='denied'?'disabled':''}>${DigestUI.pushOn?'Push подключён':permission==='denied'?'Push запрещён браузером':supported?'Включить push':'Push: откройте приложение с экрана «Домой»'}</button></div><p class="digest-message" role="status" data-digest-message></p></section>`}
const DigestUI=(()=>{
 const shown=new Set();
 let audio,resuming,pending=false,pushOn=false,lastAlert=0,timer,stopped=false;
 const account=()=>typeof state!=='undefined'&&state.data?.profile?.is_admin?String(state.data.profile.freeman_id||state.data.profile.id):'';
 const soundOn=()=>NoticeCenter.soundOn();
 function unlock(){
  if(!account()||!soundOn())return Promise.resolve(false);
  try{const A=window.AudioContext||window.webkitAudioContext;if(!A)return Promise.resolve(false);
   if(!audio||audio.state==='closed'){audio=new A();resuming=null}
   if(audio.state==='running')return Promise.resolve(true);
   if(!resuming){const current=audio;resuming=Promise.resolve(current.resume()).then(()=>audio===current&&current.state==='running').catch(()=>false).finally(()=>{resuming=null})}
   return resuming;
  }catch{return Promise.resolve(false)}
 }
 async function sound(){
  if(!audio||document.hidden||!soundOn())return false;
  const owner=account();if(!await unlock()||account()!==owner||document.hidden||!soundOn())return false;
  try{const at=audio.currentTime;[880,1174.66,1567.98].forEach((frequency,i)=>{const o=audio.createOscillator(),g=audio.createGain();o.type='sine';o.frequency.value=frequency;g.gain.setValueAtTime(0,at+i*.08);g.gain.linearRampToValueAtTime(.045,at+i*.08+.015);g.gain.exponentialRampToValueAtTime(.001,at+i*.08+.25);o.connect(g);g.connect(audio.destination);o.start(at+i*.08);o.stop(at+i*.08+.27)});return true}catch{return false}
 }
 function badge(){NoticeCenter.badge()}
 async function open(day){if(!account())return;const n=state.extra.dailyDigests?.find(x=>x.id===day);if(n){try{await fetch('/api/web/admin/digests/'+encodeURIComponent(day)+'/read',{method:'POST'});n.read=true}catch{}}FreemanComponents.dismissNoticeBanner(document.querySelector('.owner-digest-toast'));if(n?.kind==='test'){state.modal=null;render();badge();return}if(/^payout-\d+$/.test(day)){state.modal=null;state.extra.adminReferralTab='payouts';await loadView('referrals');await adminPayoutOpen(Number(day.slice(7)));badge();return}state.modal=null;state.extra.reportFilters={};state.extra.reportKind='all';state.extra.reportPage=1;state.extra.reportPeriod='custom';state.extra.reportStart=day;state.extra.reportEnd=day;await loadView('admin-reports');badge()}
 function shownKey(n){return 'freeman-digest-shown:'+account()+':'+n.id}
 function alreadyShown(n){const key=shownKey(n);try{return !!localStorage.getItem(key)}catch{return shown.has(key)}}
 async function popup(n){if(!account()||document.hidden||noticeIsRead(n))return;
  const key=shownKey(n);
  const show=()=>{if(alreadyShown(n))return;try{localStorage.setItem(key,'1')}catch{shown.add(key)}
   FreemanComponents.showNoticeBanner({
    className:'owner-digest-toast',iconName:n.kind==='withdrawal'?'wallet':'chart',
    label:n.kind==='withdrawal'?'Заявка на выплату':n.kind==='test'?'Проверка уведомлений':'Итоги дня',
    title:n.title,text:noticePlain(n.text),openAttributes:`data-digest-open="${esc(n.id)}"`,dismissAttributes:'data-digest-dismiss'
   });
   if(Date.now()-lastAlert>5000)sound();
  };
  if(navigator.locks)await navigator.locks.request('freeman-digest:'+key,show);else show();
 }
 function schedule(ms){clearTimeout(timer);if(!stopped)timer=setTimeout(poll,ms)}
 async function poll(){clearTimeout(timer);const current=account();if(stopped)return;if(!current){FreemanComponents.dismissNoticeBanner(document.querySelector('.owner-digest-toast'));schedule(20000);return}
  if(pending){schedule(1000);return}pending=true;
  try{await NoticeCenter.load();const r=await fetch('/api/web/admin/digests',{cache:'no-store'});if(!r.ok)return;const d=await r.json();if(account()!==current)return;state.extra.dailyDigests=d.items||[];badge();const n=state.extra.dailyDigests.find(x=>!noticeIsRead(x)&&!alreadyShown(x));if(n)await popup(n);const payout=new URL(location.href).searchParams.get('referral_payout');if(payout&&/^\d+$/.test(payout)){const url=new URL(location.href);url.searchParams.delete('referral_payout');history.replaceState(history.state,'',url.pathname+url.search+url.hash);await open('payout-'+payout)}const day=new URL(location.href).searchParams.get('daily_report');if(day&&/^\d{4}-\d{2}-\d{2}$/.test(day)){const url=new URL(location.href);url.searchParams.delete('daily_report');history.replaceState(history.state,'',url.pathname+url.search+url.hash);await open(day)}}catch{}finally{pending=false;if(!stopped)schedule(20000)}
 }
 async function enablePush(){unlock();const message=document.querySelector('[data-digest-message]'),button=document.querySelector('[data-digest-push]');if(button)button.disabled=true;
  try{
   if(!('serviceWorker'in navigator)||!('PushManager'in window)||!('Notification'in window))throw Error('На iPhone добавьте сайт на экран «Домой» и откройте его оттуда.');
   const permission=await Notification.requestPermission();if(permission!=='granted')throw Error('Разрешите уведомления в настройках браузера.');
   const r=await fetch('/api/web/admin/push/config',{cache:'no-store'});if(!r.ok)throw Error('Не удалось подключить push');const cfg=await r.json();if(!cfg.available)throw Error('Сервис push пока не готов. Отчёты доступны в колокольчике.');
   const registration=await navigator.serviceWorker.register('/admin-notifications-sw.js',{scope:'/',updateViaCache:'none'});await navigator.serviceWorker.ready;
   const encoded=cfg.public_key.replace(/-/g,'+').replace(/_/g,'/'),key=Uint8Array.from(atob(encoded+'='.repeat((4-encoded.length%4)%4)),c=>c.charCodeAt(0));
   const existing=await registration.pushManager.getSubscription();const subscription=existing||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
   const result=await fetch('/api/web/admin/push/subscription',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(subscription.toJSON())});if(!result.ok){const detail=await result.json().catch(()=>({}));throw Error(typeof detail.detail==='string'?detail.detail:'Не удалось сохранить push-подписку. Повторите подключение.');}pushOn=true;if(button)button.textContent='Push подключён';if(message)message.textContent='Готово. Отчёт придёт в 00:00 МСК; звук системного push управляется настройками устройства.';
  }catch(e){if(message)message.textContent=e.message}finally{if(button)button.disabled=false}
 }
 document.addEventListener('pointerdown',unlock,{passive:true});document.addEventListener('keydown',unlock,{passive:true});
 document.addEventListener('click',e=>{const button=e.target.closest('[data-digest-push],[data-digest-sound],[data-digest-open],[data-digest-dismiss]');if(!button)return;if(button.hasAttribute('data-digest-push'))enablePush();if(button.hasAttribute('data-digest-sound')){try{NoticeCenter.setSound(!soundOn())}catch{}unlock();button.setAttribute('aria-checked',String(soundOn()));button.querySelector('[data-digest-sound-label]').textContent=soundOn()?'Звук: включён':'Звук: выключен';if(soundOn())sound()}if(button.dataset.digestOpen)open(button.dataset.digestOpen);if(button.hasAttribute('data-digest-dismiss'))FreemanComponents.dismissNoticeBanner(document.querySelector('.owner-digest-toast'))});
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});window.addEventListener('focus',poll);window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer)});window.addEventListener('pageshow',()=>{stopped=false;poll()});window.addEventListener('storage',e=>{if(e.key?.startsWith('freeman-notices-read:'))badge()});
 navigator.serviceWorker?.addEventListener('message',e=>{if(e.data?.type==='freeman-digest'){lastAlert=Date.now();poll()}});
 setTimeout(poll,1000);
 return{poll,soundOn,get pushOn(){return pushOn}};
})();
