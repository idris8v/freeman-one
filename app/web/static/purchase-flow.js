/* Receipt UI observes confirmed server results; it never provisions access. */
function sortedKeys(keys){
  const stamp=key=>Date.parse(key.created_at)||Date.parse(key.purchased_at)||0;
  return [...keys].sort((a,b)=>stamp(b)-stamp(a)||b.id-a.id);
}
function recentlyPurchased(key,clock=Date.now()){
  const purchased=Date.parse(key.purchased_at);
  const issued=Date.parse(key.created_at);
  const age=clock-(Number.isFinite(issued)?issued:purchased);
  return Number.isFinite(purchased)&&purchased<=clock&&age>=0&&age<72*3600000;
}
function purchasedKey(keys,result){
  if(result.key_id)return keys.find(key=>key.id===Number(result.key_id))||null;
  return result.subscription_id?keys.find(key=>key.subscription_id===Number(result.subscription_id))||null:null;
}
function paymentPhase(status){
  if(['order_paid','balance_paid','stars_paid','paid'].includes(status))return 'success';
  if(['order_pending','order_created','order_processing','pending'].includes(status))return 'pending';
  if(String(status).includes('canceled')||String(status).includes('failed'))return 'canceled';
  return 'other';
}
function keyDateMetadata(key){
  const stamp=key.purchased_at||key.created_at;
  if(!stamp)return '';
  const connected=key.first_connected_at?` · Подключён ${supportDate(key.first_connected_at)}`:'';
  return `<small class="key-date">${key.purchased_at?'Куплен':'Создан'} ${esc(date(stamp))}${esc(connected)}</small>`;
}
function purchaseSuccessPage(receipt,key){
 if(state.purchase.managementKey!==key.id){state.expanded=key.id;state.purchase.managementKey=key.id}
 if(!isPreview())queueMicrotask(hydrateCustomerKeys);
 const current=state.data?.keys?.find(item=>item.id===key.id)||key;
 return `<section class="fo-scope fo-keys fo-purchase-result"><header class="fo-purchase-result-heading"><span aria-hidden="true">${actionIcon('check')}</span><div><h1>${receipt.renew?'Подписка продлена':'Подписка готова'}</h1><p>Подключение и управление — в карточке ниже.</p></div></header>${subscriptionCard(current,{afterPurchase:true})}<button type="button" class="fo-button fo-button--compact" data-view="keys">Все мои ключи</button></section>`;
}
function purchasePage(){
  const receipt=state.purchase||{stage:'pending'},key=receipt.key;
  const success=receipt.stage==='success',waiting=['pending','processing'].includes(receipt.stage);
  if(success&&key)return purchaseSuccessPage(receipt,key);
  const title=success?(receipt.renew?'Подписка продлена':'Подписка оформлена'):receipt.stage==='processing'?'Платёж принят':waiting?'Проверяем платёж':receipt.stage==='canceled'?'Платёж не завершён':'Не удалось проверить платёж';
  return `${pageHeader('Ваша подписка','Покупка')}<section class="purchase-confirmation" role="status"><span class="row-icon">${svg(success?'check':'info')}</span><div><h2>${title}</h2><p>${success?'Ключ и установка доступны ниже.':esc(receipt.error||(receipt.stage==='processing'?'Подготавливаем ключ. Покупку повторять не нужно.':waiting?'Подписка появится после подтверждения оплаты.':'Вернитесь к тарифам или проверьте статус ещё раз.'))}</p></div></section>${isPreview()?'<p class="purchase-demo">Предпросмотр: демонстрация успешной покупки, без списания средств.</p>':''}${key?`<section class="purchase-key-card"><header><span class="row-icon">${svg('keys')}</span><div><h2>${esc(key.name)}</h2><small class="purchase-active">${key.status==='active'&&daysLeft(key.expires_at)>0?'Активна':'Подключение подготавливается'}</small></div></header><dl><div><dt>Срок</dt><dd>${receipt.months||key.duration_months?monthLabel(receipt.months||key.duration_months):'По подписке'}</dd></div><div><dt>${key.purchased_at?'Куплена':'Оформлена'}</dt><dd>${esc(shortDate(receipt.created_at||key.purchased_at||key.created_at))}</dd></div><div><dt>Действует до</dt><dd>${esc(shortDate(key.expires_at))}</dd></div><div><dt>Устройства</dt><dd>${key.device_limit||1}</dd></div></dl><label class="purchase-key-label">Ключ подписки</label><code class="purchase-key-value">${key.connection_loaded?esc(key.display_key):'Получаем данные подключения…'}</code>${receipt.connectionError?`<p class="purchase-demo">${esc(receipt.connectionError)}</p>`:''}<div class="purchase-key-actions"><button data-copy-key="${key.id}" ${key.connection_loaded?'':'disabled'}>${svg('copy')}<span>Копировать</span></button><button class="subscription-install" data-key-install="${key.id}">${svg('download')}<span>Установить</span></button></div></section>`:''}<div class="purchase-flow-actions">${!success&&receipt.stage!=='canceled'?'<button data-purchase-retry>Проверить ещё раз</button>':''}<button data-view="${success?'keys':'plans'}">${success?'Мои ключи':'К тарифам'}</button></div>`;
}
let purchasePollTimer=null,purchaseChecking=null;
function purchaseContext(){try{return JSON.parse(sessionStorage.getItem('freeman-purchase-context')||'{}')}catch{return {}}}
async function completePurchase(result,context={}){
  if(!CheckoutRecovery.isCurrent(context))return false;
  clearTimeout(purchasePollTimer);
  const entering=state.view!=='purchase'||state.purchase?.stage!=='processing',receipt={...context,...result,stage:'processing',key:null};state.purchase=receipt;state.view='purchase';if(entering)render();
  const current=()=>state.purchase===receipt&&CheckoutRecovery.isCurrent(context);
  if(!isPreview()){
    const data=await CheckoutRecovery.fetchResult('/api/web/me');if(!current())return false;
    if(context.account&&data.profile?.freeman_id!==context.account)throw new Error('Аккаунт изменился. Войдите в аккаунт покупки.');
    state.data=data;
  }
  const key=purchasedKey(state.data.keys,result);
  if(!key){render();return false}
  state.purchase.key=key;
  try{
    if(!isPreview()){
      const connection=await CheckoutRecovery.fetchResult(`/api/web/keys/${key.id}/connection`);
      if(!current())return false;
      if(!connection.value)throw new Error('Ключ ещё выпускается. Проверьте статус ещё раз.');
      key.display_key=connection.value;key.connection_loaded=true;
    }
    await CheckoutRecovery.confirm(result,context);if(!current())return false;
    state.purchase.stage='success';
    sessionStorage.removeItem('freeman-payment');sessionStorage.removeItem('freeman-purchase-context');cleanPaymentReturn();
  }catch(error){if(!current())return false;state.purchase.connectionError=error.message}
  if(state.purchase.stage==='success')render();return state.purchase.stage==='success';
}
function checkReturnedPayment(paymentId,attempt=0){
  if(purchaseChecking)return purchaseChecking;
  const context=purchaseContext(),account=state.data?.profile?.freeman_id,tracked=sessionStorage.getItem('freeman-payment');
  const current=()=>account===state.data?.profile?.freeman_id&&CheckoutRecovery.isCurrent(context)&&(!tracked||sessionStorage.getItem('freeman-payment')===String(paymentId));
  clearTimeout(purchasePollTimer);
  purchaseChecking=(async()=>{
  try{
    const result=await CheckoutRecovery.fetchResult(`/api/web/payments/${paymentId}`);
    if(!current())return;
    const phase=paymentPhase(result.status);if(!context.months&&result.months)context.months=result.months;if(!context.started&&result.created_at)context.started=Date.parse(result.created_at);
    if(phase==='success'){
      const complete=await completePurchase({...result,paymentId},context);
      if(!complete)schedulePaymentCheck(paymentId,attempt);
    }else if(result.status==='topup_credited'){
      sessionStorage.removeItem('freeman-payment');sessionStorage.removeItem('freeman-purchase-context');cleanPaymentReturn();
      const plan=sessionStorage.getItem('freeman-pending-checkout');
      await refresh();
      if(plan){state.selectedPlan=Number(plan);state.renewKeyId=Number(sessionStorage.getItem('freeman-pending-renew-key'))||null;state.paymentMethod='balance';state.checkoutNotice='Баланс пополнен. Завершите покупку выбранной подписки.';sessionStorage.removeItem('freeman-pending-checkout');sessionStorage.removeItem('freeman-pending-renew-key');state.view='plans';render()}
      else {await loadView('balance');say('Баланс пополнен')}
    }else if(result.status==='proxy_order_paid'){
      sessionStorage.removeItem('freeman-payment');sessionStorage.removeItem('freeman-purchase-context');cleanPaymentReturn();await refresh();await loadView('proxy');say('Подключение оплачено');
    }else{
      const stage=phase==='canceled'?'canceled':result.confirmed?'processing':phase==='pending'?'pending':'error';const previous=state.purchase;state.purchase={...context,paymentId,stage};const changed=state.view!=='purchase'||previous?.stage!==stage||previous?.paymentId!==paymentId||(!previous?.longWait&&Date.now()-(context.started||Date.now())>=900000);state.purchase.longWait=Date.now()-(context.started||Date.now())>=900000;state.view='purchase';if(changed)render();
      if(phase==='canceled'){await CheckoutRecovery.confirm({payment_id:paymentId},context);if(!current())return;sessionStorage.removeItem('freeman-payment');sessionStorage.removeItem('freeman-purchase-context');cleanPaymentReturn();}
      else schedulePaymentCheck(paymentId,attempt);
    }
  }catch(error){if(!current())return;state.purchase={...(state.purchase||{}),...context,paymentId,stage:'error',error:error.message};state.view='purchase';render();schedulePaymentCheck(paymentId,attempt)}
  })().finally(()=>{purchaseChecking=null;const id=sessionStorage.getItem('freeman-payment');if(id&&id!==String(paymentId))purchasePollTimer=setTimeout(()=>checkReturnedPayment(id),0)});
  return purchaseChecking;
}
function schedulePaymentCheck(paymentId,attempt){purchasePollTimer=setTimeout(()=>{if(state.view==='purchase')checkReturnedPayment(paymentId,attempt+1)},attempt<10?3000:10000)}
async function previewPurchase(tariff){
  const stamp=new Date().toISOString(),renewKey=state.data.keys.find(key=>key.id===state.renewKeyId);
  const key=renewKey||{id:Math.max(0,...state.data.keys.map(key=>key.id))+1,name:'Новая подписка',devices:[],auto_renew:false,status:'active',device_limit:tariff.device_limit||1};
  key.subscription_id=key.id;key.duration_months=tariff.months;key.created_at=key.created_at||stamp;key.purchased_at=key.purchased_at||stamp;
  key.expires_at=new Date(Date.now()+tariff.months*30*86400000).toISOString();key.display_key=`DEMO-${key.id}-FREEMAN-PREVIEW`;key.connection_loaded=true;
  if(!renewKey){state.data.keys.push(key);state.data.summary.keys++;state.data.summary.keys_active++}
  await completePurchase({key_id:key.id,subscription_id:key.subscription_id,months:tariff.months,created_at:stamp},{renew:!!renewKey});
}
