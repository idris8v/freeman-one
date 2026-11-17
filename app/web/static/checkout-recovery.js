/* A purchase intent survives response loss, reloads and other same-account tabs.
 * Only the backend decides whether money/access changed. No keys or credentials
 * are stored here. Storage failures stop checkout before sending a request. */
const CheckoutRecovery=(()=>{
  let synchronization=null,again=!!window.FreemanCabinetBoot?.checkoutChanged,settledKey=null,retryTimer=null;
  const storageKey=()=>{
    const account=state.data?.profile?.freeman_id;
    if(!account)throw Error('Обновите страницу перед оплатой.');
    return 'freeman-checkout:'+account;
  };
  function read(){
    const raw=localStorage.getItem(storageKey());
    if(!raw)return null;
    const intent=JSON.parse(raw);
    if(!intent?.key||!intent.parameters||intent.account!==state.data.profile.freeman_id)throw Error('Не удалось прочитать сохранённый заказ. Обратитесь в поддержку.');
    return intent;
  }
  function save(intent){
    if(intent.account!==state.data?.profile?.freeman_id)throw Error('Аккаунт изменился. Войдите в аккаунт, где была начата покупка.');
    const key=storageKey(),value=JSON.stringify(intent);
    localStorage.setItem(key,value);
    if(localStorage.getItem(key)!==value)throw Error('Не удалось сохранить заказ. Разрешите хранение данных сайта.');
  }
  function isCurrent(context){
    if(context.account&&context.account!==state.data?.profile?.freeman_id)return false;
    try{const intent=read();return !context.checkoutKey||!intent||intent.key===context.checkoutKey}catch{return false}
  }
  const purchaseContextFor=intent=>({months:intent.parameters.months,renew:!!intent.parameters.subscription_id,kind:'vpn',account:intent.account,checkoutKey:intent.key});
  const parameters=p=>({months:Number(p.months),...(p.tariff_id!=null?{tariff_id:Number(p.tariff_id)}:{}),method:p.method,...(p.antig?{antig:true,expected_amount:Number(p.expected_amount)}:{}),use_balance:!!p.use_balance,subscription_id:p.subscription_id||null});
  const same=(a,b)=>JSON.stringify(parameters(a))===JSON.stringify(parameters(b));
  async function request(url,options){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    try{
      const response=await fetch(url,{...options,signal:controller.signal}),data=await response.json();
      if(!response.ok){const error=Error(typeof data.detail==='string'?data.detail:'Не удалось проверить заказ.');error.status=response.status;throw error}
      return data;
    }catch(error){
      if(controller.signal.aborted)throw Error('Сервер не ответил вовремя. Результат заказа ещё проверяется; повторно оплачивать не нужно.');
      throw error;
    }finally{clearTimeout(timer)}
  }
  const post=(url,intent)=>request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...intent.parameters,idempotency_key:intent.key})});
  async function result(intent){
    if(intent.result)return intent.result;
    let operation;
    try{operation=await request('/api/web/checkout?idempotency_key='+encodeURIComponent(intent.key))}
    catch(error){if(error.status!==404)throw error}
    if(operation?.request&&!same(operation.request,intent.parameters))throw Error('Параметры сохранённого заказа отличаются. Обратитесь в поддержку.');
    intent.result=operation?.result||await post('/api/web/checkout',intent);
    save(intent);
    return intent.result;
  }
  async function display(intent,tab,data){
    const context=purchaseContextFor(intent);
    if(!isCurrent(context)){return}
    if(data.status==='canceled'){
      await confirm(data,context);say('Заказ отменён. Можно выбрать новые параметры.');return;
    }
    if(data.redirect_url)await startExternalPayment(data,tab,context);
    else {await completePurchase(data,context)}
  }
  function locked(action){
    const key=storageKey();
    return navigator.locks?.request?navigator.locks.request(key,action):action();
  }
  async function submit(p,tab){
    const started=Date.now();
    try{const prepared=await locked(async()=>{
      const desired=parameters(p);let intent=read();
      if(intent?.confirmedAt&&intent.confirmedAt<started)intent=null;
      if(intent&&!same(intent.parameters,desired)&&!intent.confirmedAt){
        // Changing the plan explicitly abandons only an unstarted operation.
        // The server lock also protects against a delayed original POST.
        const operation=await post('/api/web/checkout/cancel',intent);
        if(operation.result?.status==='canceled')intent=null;
        else {intent.result=operation.result||null;save(intent);return {intent,data:await result(intent)}}
      }
      if(!intent){intent={key:crypto.randomUUID(),parameters:desired,account:state.data.profile.freeman_id};save(intent)}
      return {intent,data:await result(intent)};
    });await display(prepared.intent,tab,prepared.data)}finally{if(again)setTimeout(()=>synchronize(),0)}
  }
  async function confirm(data,context={}){
    const acknowledge=()=>{
      if(!isCurrent(context))return;
      const intent=read();if(!intent)return;
      const paid=intent.result||{};
      if(context.checkoutKey===intent.key||
         (paid.payment_id&&Number(paid.payment_id)===Number(data.payment_id||data.paymentId))||
         (!paid.payment_id&&paid.subscription_id&&Number(paid.subscription_id)===Number(data.subscription_id))){
        settledKey=intent.key;
        if(!intent.confirmedAt){intent.confirmedAt=Date.now();save(intent)}
      }
    };
    await locked(acknowledge);
  }
  // Tab events only observe an existing operation. They must never submit or
  // cancel checkout, even when the original POST/result is still uncertain.
  async function observe(){
    const intent=read();if(!intent)return false;
    const activePayment=sessionStorage.getItem('freeman-payment'),activeContext=purchaseContext();
    if(activePayment&&activeContext.kind&&activeContext.kind!=='vpn')return false;
    if(intent.confirmedAt&&!activePayment)return false;
    const context=purchaseContextFor(intent);
    try{
      const operation=await request('/api/web/checkout?idempotency_key='+encodeURIComponent(intent.key),{cache:'no-store'});
      if(!isCurrent(context))return false;
      if(operation.request&&!same(operation.request,intent.parameters))throw Error('Параметры сохранённого заказа отличаются. Обратитесь в поддержку.');
      const data=operation.result;
      const id=operation.payment_id||data?.payment_id;
      if(activePayment&&!activeContext.checkoutKey&&String(id)!==activePayment)return false;
      if(id){
        if(!Number.isSafeInteger(Number(id))||Number(id)<1)throw Error('Не удалось проверить номер платежа.');
        sessionStorage.setItem('freeman-payment',String(id));
        sessionStorage.setItem('freeman-purchase-context',JSON.stringify({...activeContext,...context,url:data?.redirect_url||activeContext.url,started:activeContext.started||Date.now()}));
        await checkReturnedPayment(id);
      }else if(data&&paymentPhase(data.status)==='success'){
        if(!await completePurchase(data,context)&&isCurrent(context))retryTimer=setTimeout(()=>synchronize(),3000);
      }else if(data&&paymentPhase(data.status)==='canceled'){
        await confirm(data,context);if(!isCurrent(context))return false;
        state.purchase={...context,stage:'canceled'};state.view='purchase';render();
      }else throw Error('Результат заказа пока не подтверждён. Повторно оплачивать не нужно.');
    }catch(error){
      if(!isCurrent(context))return false;
      state.purchase={...activeContext,...context,stage:'error',error:error.message};state.view='purchase';render();
      retryTimer=setTimeout(()=>{if(state.view==='purchase')synchronize()},3000);
    }
    return true;
  }
  function synchronize(force=false){
    again=true;
    if(!state.data||isPreview()||(!force&&document.hidden))return Promise.resolve(false);
    if(synchronization)return synchronization;
    clearTimeout(retryTimer);
    synchronization=(async()=>{let handled=false;do{again=false;handled=await observe()||handled}while(again&&!document.hidden);return handled})()
      .catch(()=>false).finally(()=>{synchronization=null});
    return synchronization;
  }
  async function resume(){
    let intent;
    try{intent=read();if(!intent)return false;if(intent.confirmedAt&&!again){settledKey=intent.key;return false}}
    catch(error){state.checkoutNotice=error.message;state.view='plans';render();return true}
    const activePayment=sessionStorage.getItem('freeman-payment'),activeContext=purchaseContext();
    if(activePayment&&!activeContext.checkoutKey){
      if(activeContext.kind&&activeContext.kind!=='vpn')return false;
      if(intent.result?.payment_id&&String(intent.result.payment_id)!==activePayment)return false;
      return synchronize(true);
    }
    const p=intent.parameters;
    state.selectedPlan=(p.tariff_id!=null?state.data.tariffs.find(t=>t.id===p.tariff_id):state.data.tariffs.filter(t=>t.months===p.months).length===1?state.data.tariffs.find(t=>t.months===p.months):null)?.id;
    state.paymentMethod=p.method;
    state.antig=!!p.antig;
    state.renewKeyId=state.data.keys.find(k=>k.subscription_id===p.subscription_id)?.id||null;
    let handled=false,reconcile=false;
    try{
      const prepared=await locked(async()=>{const current=read();if(!current)return;if(current.confirmedAt){reconcile=true;return}return {intent:current,data:await result(current)}});
      // Receipt reads/confirmation run after releasing the checkout claim lock.
      // A slow payment check must not block another tab or wait on its own lock.
      if(reconcile)handled=await synchronize(true);
      else if(prepared){await display(prepared.intent,null,prepared.data);handled=true}
    }catch(error){
      state.checkoutNotice='Результат покупки пока не подтверждён. Повторная попытка проверит тот же заказ. '+error.message;
      state.view='plans';render();
      handled=true;
    }
    return handled;
  }
  function pending(){try{const intent=read();return !!intent&&!intent.confirmedAt}catch{return true}}
  window.addEventListener('storage',event=>{
    if(typeof state==='undefined'||!state.data){again=true;return}
    if(event.key===null||event.key==='freeman-checkout:'+state.data.profile.freeman_id)synchronize();
  });
  return {submit,resume,confirm,pending,synchronize,isCurrent,fetchResult:url=>request(url,{cache:'no-store'})};
})();
