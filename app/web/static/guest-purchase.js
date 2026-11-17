'use strict';
const main=document.querySelector('#content'),error=document.querySelector('#error');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>window.AEGIS_ICONS?.icons?.[n]||'';
const rub=n=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:n%100?2:0}).format(n/100);
const term=n=>FreemanComponents.tariffTerm(n);
let checkoutIdentity={recognized:false};
let nonce=crypto.randomUUID()+crypto.randomUUID();
let antigSelected=false;
const selectedTotal=()=>selected.amount+(!renewal&&antigSelected&&catalog.antig?.enabled?(catalog.antig.prices?.[selected.id]||0):0);
let catalog=[],selected=null,draft=null,emailToken='',verified=false,receiptData=null,poll=null;
const renewal=location.pathname.endsWith('/renew');
async function api(path,data){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),45000);
 try{
  const response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,credentials:'same-origin',cache:'no-store',signal:controller.signal});
  let result;try{result=await response.json()}catch{throw Error('Сервис временно недоступен. Попробуйте ещё раз.')}
  if(!response.ok){const problem=Error(typeof result.detail==='string'?result.detail:'Проверьте введённые данные.');problem.status=response.status;throw problem}
  return result;
 }catch(e){if(e.name==='AbortError')throw Error('Ответ задерживается. Заказ сохранён; повторите проверку, не создавая новую покупку.');throw e}
 finally{clearTimeout(timeout)}
}
function fail(e){error.textContent=e.message||String(e);error.hidden=false;}
function values(label,value,id){return `<label for="${id}">${label}</label><div class="value"><input class="field" id="${id}" readonly value="${esc(value)}"><button class="secondary" type="button" data-copy="${id}" aria-label="Копировать ${label}">${icon('copy')}</button></div>`;}
let renewalInfo=null,busy=false;
const deviceText=d=>d===0?'Без лимита':`${d} ${d%10===1&&d%100!==11?'устройство':[2,3,4].includes(d%10)&&![12,13,14].includes(d%100)?'устройства':'устройств'}`;
const tariffOrder=(a,b)=>(a.devices||Infinity)-(b.devices||Infinity)||a.months-b.months||a.amount-b.amount||a.id-b.id;
function discountMarkup(base,total){
 if(!Number.isFinite(base)||!Number.isFinite(total)||total<0||base<=total)return '';
 const percent=Math.floor((base-total)*100/base),label=percent?'−'+percent+'%':'Скидка';
 return `<del aria-label="Цена без скидки">${rub(base)}</del><small class="tariff-saving">${label} · выгода ${rub(base-total)}</small>`;
}
function optionRows(){
 return FreemanComponents.purchaseTerms({tariffs:catalog,selectedId:selected.id,scope:'guest',discount:discountMarkup});
}
function updateTariffOptions(){
 const host=document.querySelector('.options');
 const template=document.createElement('template');template.innerHTML=optionRows();
 const previous=[...host.children],next=[...template.content.children];
 next.forEach((fresh,i)=>{
  const existing=previous[i];
  if(existing){existing.dataset.tariff=fresh.dataset.tariff;existing.innerHTML=fresh.innerHTML;existing.setAttribute('aria-checked',fresh.getAttribute('aria-checked'));existing.tabIndex=fresh.tabIndex;}
  else host.append(fresh);
 });
 previous.slice(next.length).forEach(node=>node.remove());
 sizeTariffOptions();
}
function sizeTariffOptions(){
 const counts=[...new Set(catalog.map(t=>t.devices))].map(d=>catalog.filter(t=>t.devices===d).length);
 const count=Math.max(1,...counts),host=document.querySelector('.options');
 host.style.setProperty('--term-rows',String(count));host.style.setProperty('--term-grid-rows',String(Math.ceil(count/2)));
}
function buttonLabel(){return `${renewal?'Продлить':'Оплатить'} · ${rub(selectedTotal())}`}
function updateSelection(){document.querySelectorAll('[data-device]').forEach(b=>{const yes=Number(b.dataset.device)===selected.devices;b.setAttribute('aria-checked',yes);b.tabIndex=yes?0:-1});document.querySelectorAll('[data-tariff]').forEach(b=>{const yes=Number(b.dataset.tariff)===selected.id;b.setAttribute('aria-checked',yes);b.tabIndex=yes?0:-1});document.querySelector('#selection-text').textContent=`${deviceText(selected.devices)} · ${term(selected.months)}`;document.querySelector('#total').textContent=rub(selectedTotal());const card=document.querySelector('.fo-antig-card');if(card)FreemanComponents.updateAntigOption(card,{available:catalog.antig?.enabled,selected:antigSelected,surcharge:catalog.antig?.prices?.[selected.id]||0,disabled:busy});document.querySelector('#purchase').textContent=buttonLabel();}
function draw(){
 main.innerHTML=`
 <section class="intro"><h1>${renewal?'Продлить подписку':'Купить подписку'}</h1><p>${renewal?'Ключ и Freeman ID останутся прежними.':'Ключ после оплаты. Без предварительной регистрации.'}</p></section>
 ${renewal?'<a class="back-link" href="/buy">← Новая подписка</a>':''}
 ${renewal?'<section class="card"><h2>Ссылка подписки</h2><input id="renew-key" class="field" placeholder="Ваша ссылка подключения" aria-label="Ссылка подписки" autocomplete="off"><button class="secondary" id="find-key" type="button">Найти подписку</button><div id="subscription-info" aria-live="polite"></div></section>':''}
 <div id="configuration" ${renewal?'hidden':''}>
 ${FreemanComponents.purchaseChoices({tariffs:catalog,selectedId:selected.id,scope:'guest',discount:discountMarkup})}
 ${!renewal?FreemanComponents.antigOption({available:catalog.antig?.enabled,selected:antigSelected,surcharge:catalog.antig?.prices?.[selected.id]||0}):''}
 ${!renewal?`<section class="card"><h2>Добавьте почту</h2><label for="email">Ваша почта · необязательно</label><div class="email-field fo-input-group"><span>${icon('mail')}</span><input id="email" class="field" type="email" autocomplete="email" placeholder="Ваша почта"></div><p class="note">Ключ появится здесь после оплаты — даже без почты. Все функции личного кабинета будут доступны.</p><aside class="fo-feedback" data-tone="info">На почту дополнительно пришлём ключ, инструкцию и данные для входа. Она поможет восстановить доступ. Без почты сохраните логин и пароль.</aside><div id="verification" hidden></div></section>`:''}
 <section class="card checkout"><div class="total" aria-live="polite"><div><span class="sub">Ваш выбор</span><strong id="selection-text">${deviceText(selected.devices)} · ${term(selected.months)}</strong></div><strong id="total">${rub(selectedTotal())}</strong></div><button type="button" class="primary fo-button fo-button--success fo-button--glass" id="purchase" ${!catalog.ready?'disabled':''}>${buttonLabel()}</button><p class="note">Оплата откроется здесь. После неё вы вернётесь к заказу. Ключ — после подтверждения оплаты.</p>${!catalog.ready?'<p class="warning" role="status">Приём новых платежей пока выключен. Сохранённые заказы продолжают проверяться.</p>':''}</section></div>`;
}
// Keep the original intent until the backend confirms its outcome. No credentials
// or subscription URLs are stored here; HttpOnly order cookies enforce ownership.
const intentKey='freeman-guest-checkout:'+ (renewal?'renew':'new');
function readIntent(){try{const raw=localStorage.getItem(intentKey);return raw?JSON.parse(raw):null}catch{return null}}
function saveIntent(value){try{localStorage.setItem(intentKey,JSON.stringify(value))}catch{throw Error('Разрешите сохранение данных сайта перед оплатой: это нужно для безопасного восстановления заказа.')}}
function clearIntent(oid){const saved=readIntent();if(oid?String(saved?.orderId)===String(oid):!['pay','receipt'].includes(saved?.phase))localStorage.removeItem(intentKey)}
function receiptLocation(oid){const here=new URL(location.href);here.searchParams.set('receipt',oid);history.replaceState(null,'',here)}
async function sendPayment(intent,navigate=false){
 receiptLocation(intent.orderId);
 const result=await api(`/api/buy/${intent.orderId}/pay`,{months:intent.months,without_email:intent.withoutEmail});
 let url;
 if(result.url&&!['order_paid','order_canceled','order_failed'].includes(result.status)){
  url=new URL(result.url);if(url.protocol!=='https:'||url.username||url.password)throw Error('Не удалось открыть страницу оплаты.');
  sessionStorage.setItem('freeman-order-url:'+intent.orderId,url.href);
 }
 saveIntent({...intent,phase:'receipt'});
 if(url&&navigate===true){location.assign(url.href);return}
 await showReceipt(intent.orderId);
}
async function resumeCheckout(oid=new URL(location.href).searchParams.get('receipt')){
 const intent=readIntent();
 if(String(intent?.orderId)!==String(oid)||intent.phase!=='pay')return showReceipt(oid);
 if(busy)return;busy=true;error.hidden=true;
 try{await sendPayment(intent,null)}catch(e){await showReceipt(intent.orderId);fail(e)}finally{busy=false}
}
async function purchase(){
 if(busy||!catalog.ready)return;
 busy=true;error.hidden=true;let navigateToPayment=null;
 const button=document.querySelector('#purchase');button.disabled=true;button.textContent='Создаём платёж…';
 try{
  // Only this explicit click may navigate to the bank; recovery only checks status.
  const input=document.querySelector('#email');
  if(renewal||!input?.value.trim()||verified)navigateToPayment=true;
  const submit=async()=>{
  const saved=readIntent();
  if(saved?.orderId&&['pay','receipt'].includes(saved.phase)){
   receiptLocation(saved.orderId);
   if(saved.phase==='pay')await sendPayment(saved,null);else await showReceipt(saved.orderId);
   return;
  }
  const email=document.querySelector('#email'),withoutEmail=!email?.value.trim();
  if(email&&email.value.trim()&&!email.checkValidity()){email.setAttribute('aria-invalid','true');email.focus();throw Error('Проверьте адрес электронной почты.')}
  const key=document.querySelector('#renew-key')?.value.trim()||'';
  if(renewal&&(!key||!renewalInfo))throw Error('Сначала найдите подписку по ключу.');
  if(saved?.phase==='draft'&&saved.tariffId===selected.id&&!!saved.antig===antigSelected)nonce=saved.nonce;
  saveIntent({nonce,tariffId:selected.id,months:selected.months,antig:antigSelected,phase:'draft'});
  if(!draft)draft=await api('/api/buy/draft',{months:selected.months,tariff_id:selected.id,antig:antigSelected,key,nonce});
  if(draft.amount!==selectedTotal()){selected.amount=draft.amount-(antigSelected?(catalog.antig?.prices?.[selected.id]||0):0);updateSelection();throw Error('Стоимость обновилась. Проверьте сумму и нажмите кнопку покупки ещё раз.')}
  if(!renewal&&!withoutEmail&&!verified){
   if(emailToken){document.querySelector('#code').focus();throw Error('Введите код из письма и подтвердите почту.')}
   const result=await api(`/api/buy/${draft.order_id}/email`,{email:email.value.trim()});
   emailToken=result.token;email.readOnly=true;
   const box=document.querySelector('#verification');box.hidden=false;
   box.innerHTML='<aside class="fo-feedback" data-tone="warning">Проверьте папку «Спам», если письмо с кодом не появилось.</aside><label for="code">Код из письма</label><input id="code" class="field" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="6 цифр"><button type="button" class="secondary" id="verify">Подтвердить почту</button>';
   FreemanUI.controls(box);document.querySelector('#code').focus();return;
  }
  const intent={nonce,tariffId:selected.id,months:selected.months,antig:antigSelected,orderId:draft.order_id,withoutEmail,phase:'pay'};
  saveIntent(intent);await sendPayment(intent,navigateToPayment);
  };
  // Independent tabs serialize the intent read/write, not just the HTTP call.
  if(navigator.locks)await navigator.locks.request(intentKey,submit);else await submit();
 }catch(e){const pending=readIntent();if(pending?.phase==='pay'){receiptLocation(pending.orderId);await showReceipt(pending.orderId)}fail(e)}
 finally{busy=false;if(button.isConnected){button.disabled=!catalog.ready;button.textContent=!renewal&&emailToken&&!verified?'Подтвердите почту':buttonLabel()}}
}
let guestReceiptBusy=false;
async function showReceipt(oid,attempt=0){if(guestReceiptBusy)return;guestReceiptBusy=true;clearTimeout(poll);const startedKey='freeman-order-started:'+oid;let started=Number(sessionStorage.getItem(startedKey));if(!started){started=Date.now();sessionStorage.setItem(startedKey,started)}try{const data=await api(`/api/buy/${oid}/receipt`);error.hidden=true;if(data.created_at&&Number.isFinite(Date.parse(data.created_at))){started=Date.parse(data.created_at);sessionStorage.setItem(startedKey,started)}if(data.status==='order_paid'&&data.key){receiptData=data;clearTimeout(poll);clearIntent(oid)}if(['order_canceled','order_failed'].includes(data.status))clearIntent(oid);const fingerprint=JSON.stringify(data)+String(Date.now()-started>=900000);if(main.dataset.receipt!==fingerprint){if(data.key&&main.querySelector('.pp-wait')&&!matchMedia('(prefers-reduced-motion: reduce)').matches)await main.animate([{opacity:1},{opacity:0}],{duration:120,easing:'ease-out'}).finished;main.dataset.receipt=fingerprint;main.innerHTML=PostPayment.render({...data,status:data.status==='order_created'?'order_creating':data.status,login:data.freeman_id,started,qrUrl:data.key?`/api/buy/${oid}/qr`:'',paymentUrl:data.payment_url||sessionStorage.getItem('freeman-order-url:'+oid),accountUrl:'/buy/login',enter:data.authenticated?async()=>location.assign('/'):!data.renewal&&data.password?async()=>{await api(`/api/buy/${oid}/enter`,{});location.assign('/')}:null,check:()=>resumeCheckout(oid),hideSupport:true,supportUrl:document.querySelector('#telegram').href});if(data.status==='order_paid'&&data.key){const guide=document.createElement('a');guide.className='pp-button';guide.href='/connect-guide?receipt='+encodeURIComponent(oid);guide.textContent='Инструкция по подключению';main.querySelector('.pp-subscription')?.append(guide)}}if(!receiptData&&!['order_canceled','order_failed'].includes(data.status))poll=setTimeout(()=>showReceipt(oid,attempt+1),attempt<20?3000:10000);}catch(e){if([401,403,404,410].includes(e.status)){main.dataset.receipt='access-required';main.innerHTML=PostPayment.render({status:'access_required',hideSupport:true,check:()=>showReceipt(oid),accountUrl:'/?return='+encodeURIComponent('/buy?receipt='+oid)});error.hidden=true;return}if(!receiptData){if(!main.querySelector('.pp-flow'))main.innerHTML=PostPayment.render({status:'unknown',started,hideSupport:true,check:()=>resumeCheckout(oid)});const n=main.querySelector('.pp-feedback');if(n)n.textContent='Не удалось проверить статус. Продолжаем проверку; не оплачивайте повторно.';poll=setTimeout(()=>showReceipt(oid,attempt+1),10000)}}finally{guestReceiptBusy=false}}
function loginPage(){main.innerHTML='<section class="intro"><h1>Личный кабинет</h1><p>Введите данные, полученные после покупки.</p></section><form class="card" id="login"><label for="identity">Freeman ID, почта или ключ</label><input class="field" id="identity" autocomplete="username" required><label for="password" style="margin-top:16px">Пароль</label><input class="field" id="password" type="password" autocomplete="current-password" required><button class="primary">Войти</button><div class="login-links"><a href="/">Войти по коду / восстановить</a><a href="/buy">Купить подписку</a></div></form>';document.querySelector('#login').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/buy/login',{identity:document.querySelector('#identity').value,password:document.querySelector('#password').value});location.assign('/')}catch(e){fail(e)}finally{button.disabled=false}};}
document.addEventListener('input',e=>{if(e.target.id==='renew-key'){clearIntent();draft=null;renewalInfo=null;document.querySelector('#configuration').hidden=true;nonce=crypto.randomUUID()+crypto.randomUUID()}if(e.target.id==='email'){e.target.removeAttribute('aria-invalid');clearIntent();draft=null;verified=false;emailToken='';nonce=crypto.randomUUID()+crypto.randomUUID();document.querySelector('#verification').hidden=true}});
document.addEventListener('keydown',e=>{const b=e.target.closest('[role="radio"]');if(!b||!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const rows=[...b.parentElement.querySelectorAll('[role="radio"]')],i=rows.indexOf(b),n=e.key==='Home'?0:e.key==='End'?rows.length-1:(i+(['ArrowDown','ArrowRight'].includes(e.key)?1:-1)+rows.length)%rows.length;rows[n].click();rows[n].focus()});
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.id==='forget-identity'){clearIntent();b.disabled=true;await api('/api/buy/identity/forget',{});location.assign('/buy');return}if(b.id==='find-key'){b.disabled=true;const old=b.textContent;b.textContent='Ищем подписку…';try{const info=await api('/api/buy/lookup',{key:document.querySelector('#renew-key').value.trim(),months:0});if(!info.tariffs.length)throw Error('Для этой подписки сейчас нет доступных тарифов. Обратитесь в поддержку.');const ready=catalog.ready;catalog=info.tariffs.slice().sort(tariffOrder);catalog.ready=ready;selected=catalog[0];renewalInfo=info;document.querySelector('#subscription-info').innerHTML=`<h3>${esc(info.name)}</h3><p>${deviceText(info.devices)} · ${info.status==='expired'?'Срок истёк':'Активна'}</p><p>Действует до ${new Date(info.expires_at).toLocaleDateString('ru-RU')}</p>`;updateTariffOptions();document.querySelector('.device-options').innerHTML=`<button type="button" class="option" role="radio" aria-checked="true" tabindex="0" data-device="${info.devices}"><span class="radio"></span>${deviceText(info.devices)}</button>`;document.querySelector('#configuration').hidden=false;updateSelection();error.hidden=true}finally{b.disabled=false;b.textContent=old}}if(b.hasAttribute('data-antig-toggle')){if(busy||!catalog.antig?.enabled)return;antigSelected=!antigSelected;clearIntent();draft=null;if(emailToken){emailToken='';verified=false;document.querySelector('#email').readOnly=false;document.querySelector('#verification').hidden=true;}nonce=crypto.randomUUID()+crypto.randomUUID();updateSelection();document.querySelector('[data-antig-toggle]')?.focus();return}if(b.dataset.tariff||b.dataset.device){if(busy)return;clearIntent();if(emailToken){emailToken='';verified=false;document.querySelector('#email').readOnly=false;document.querySelector('#verification').hidden=true;}const oldDevice=selected.devices;selected=catalog.find(t=>t.id===Number(b.dataset.tariff||selected.id)&&t.devices===Number(b.dataset.device||selected.devices))||catalog.find(t=>t.devices===Number(b.dataset.device)&&t.months===selected.months)||catalog.find(t=>t.devices===Number(b.dataset.device));draft=null;nonce=crypto.randomUUID()+crypto.randomUUID();if(oldDevice!==selected.devices)updateTariffOptions();updateSelection();}if(b.id==='purchase')await purchase();if(b.id==='verify'){b.disabled=true;try{await api(`/api/buy/${draft.order_id}/email/verify`,{token:emailToken,code:document.querySelector('#code').value});verified=true;document.querySelector('#verification').innerHTML='<p class="success">Почта подтверждена</p>';document.querySelector('#purchase').textContent=`Купить за ${rub(draft.amount)}`;document.querySelector('#total').textContent=rub(draft.amount)}finally{b.disabled=false}}if(b.dataset.copy){await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).value);b.title='Скопировано';b.textContent='✓';setTimeout(()=>b.innerHTML=icon('copy'),1500)}if(b.id==='check')await showReceipt(b.dataset.order);if(b.id==='enter'){await api(`/api/buy/${new URLSearchParams(location.search).get('receipt')}/enter`,{});location.assign('/')}if(b.id==='save'){const d=receiptData,text=`Freeman ONE\nЛогин: ${d.freeman_id}\nПароль: ${d.password}\nВход: ${location.origin}/buy/login\nКлюч: ${d.key}\nСохраните файл и не передавайте его посторонним.`,url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='Freeman-ONE-доступ.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}}catch(e){fail(e)}});
/* Shared control adoption after each owned render, including async state changes. */
draw=FreemanUI.enhance(draw,()=>{sizeTariffOptions();return main});
updateSelection=FreemanUI.enhance(updateSelection,()=>main);
purchase=FreemanUI.enhance(purchase,()=>main);
loginPage=FreemanUI.enhance(loginPage,()=>main);
securityPage=FreemanUI.enhance(securityPage,()=>main);
showReceipt=FreemanUI.enhance(showReceipt,()=>main);
loadOwnSubscriptions=FreemanUI.enhance(loadOwnSubscriptions,()=>main);
FreemanUI.controls(document.body);
(async()=>{document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));if(location.pathname.endsWith('/security'))return securityPage();if(location.pathname.endsWith('/login'))return loginPage();const oid=new URLSearchParams(location.search).get('receipt');if(oid&&/^\d+$/.test(oid)){if(String(readIntent()?.orderId)===oid&&readIntent()?.phase==='pay')return resumeCheckout();return showReceipt(oid)}const pending=readIntent();if(pending?.orderId&&['pay','receipt'].includes(pending.phase)){receiptLocation(pending.orderId);return pending.phase==='pay'?resumeCheckout():showReceipt(pending.orderId)}try{try{checkoutIdentity=await api('/api/buy/identity')}catch{checkoutIdentity={recognized:false}}const c=await api('/api/buy/catalog');catalog=c.tariffs.slice().sort(tariffOrder);catalog.ready=c.available;catalog.antig=c.antig;document.querySelector('#telegram').href=c.bot_url;if(!catalog.length)throw Error('Тарифы временно недоступны.');selected=catalog.find(t=>t.id===readIntent()?.tariffId)||catalog[0];antigSelected=!!readIntent()?.antig&&!!catalog.antig?.enabled&&!renewal;draw();if(renewal){const key=sessionStorage.getItem('fo-agent-renew');sessionStorage.removeItem('fo-agent-renew');if(key){document.querySelector('#renew-key').value=key;document.querySelector('#find-key').click()}else loadOwnSubscriptions()}}catch(e){main.innerHTML='<h1>Выберите подписку</h1>';fail(e)}})();

function securityPage(){document.title='Сменить пароль — Freeman ONE';main.innerHTML='<section class="intro"><h1>Сменить пароль</h1><p>Freeman ID и ваши ключи сохранятся.</p></section><form id="security" class="card"><label for="current">Текущий пароль</label><input id="current" class="field" type="password" autocomplete="current-password" required><label for="new-password" style="margin-top:16px">Новый пароль · от 10 символов</label><input id="new-password" class="field" type="password" autocomplete="new-password" minlength="10" required><button class="primary">Сохранить пароль</button><a href="/">Вернуться в личный кабинет</a></form>';document.querySelector('#security').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/buy/password',{current:document.querySelector('#current').value,password:document.querySelector('#new-password').value});main.innerHTML='<section class="card"><h1>Пароль изменён</h1><p>Сохраните новый пароль. Старые сеансы завершены.</p><a href="/">Открыть личный кабинет</a></section>'}catch(e){fail(e)}finally{button.disabled=false}}}

function guestResumePayment(){const oid=new URL(location.href).searchParams.get('receipt');if(oid&&!document.hidden&&!receiptData)void showReceipt(oid)}
document.addEventListener('visibilitychange',guestResumePayment);window.addEventListener('focus',guestResumePayment);window.addEventListener('pageshow',guestResumePayment);window.addEventListener('online',guestResumePayment);

async function loadOwnSubscriptions(){try{const data=await api('/api/buy/subscriptions');if(!data.subscriptions?.length)return;const host=document.createElement('div');host.className='own-subscriptions';host.innerHTML='<label for="own-subscription">Мои подписки</label><select id="own-subscription" class="field"><option value="">Выберите подписку</option>'+data.subscriptions.map(x=>`<option value="${esc(x.key)}">${esc(x.name)} · ${deviceText(x.devices)}</option>`).join('')+'</select>';document.querySelector('#renew-key').before(host);host.querySelector('select').onchange=e=>{if(!e.target.value)return;const input=document.querySelector('#renew-key');input.value=e.target.value;input.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#find-key').click()};if(data.subscriptions.length===1){host.querySelector('select').selectedIndex=1;host.querySelector('select').dispatchEvent(new Event('change'))}}catch{}}
