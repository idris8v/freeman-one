/* User-initiated payment navigates this tab; recovery never reopens the bank. */
function cleanPaymentReturn(){const u=new URL(location.href);u.searchParams.delete('payment');u.searchParams.delete('payment_id');history.replaceState(null,'',u);}
async function startExternalPayment(result,navigate=false,context={}){
 const id=Number(result.payment_id),url=new URL(result.redirect_url);
 if(!Number.isSafeInteger(id)||id<1||url.protocol!=='https:'||url.username||url.password)throw Error('Платёжная система не вернула корректную ссылку');
 const data={...context,url:url.href,started:Date.now()};
 sessionStorage.setItem('freeman-payment',String(id));sessionStorage.setItem('freeman-purchase-context',JSON.stringify(data));
 const u=new URL(location.href);u.searchParams.set('payment','waiting');u.searchParams.set('payment_id',id);history.replaceState(null,'',u);
 state.modal=null;const drawer=document.getElementById('px-drawer-root');if(drawer)drawer.innerHTML='';
 state.purchase={...data,paymentId:id,stage:'pending'};state.view='purchase';render();
 if(navigate===true){CabinetNavigation.save();location.assign(url.href);return}
 await checkReturnedPayment(id);
}
const paymentReturnId=new URL(location.href).searchParams.get('payment_id');if(paymentReturnId&&!new URL(location.href).searchParams.has('gift_payment')&&/^\d+$/.test(paymentReturnId))sessionStorage.setItem('freeman-payment',paymentReturnId);
const paymentPurchasePage=purchasePage;
purchasePage=function(){const r=state.purchase;if(!r||r.stage==='success')return paymentPurchasePage();const warning=r.error||r.connectionError||(r.stage==='error'?'Результат платежа пока неизвестен. Повторно оплачивать не нужно.':'');return PostPayment.render({status:r.stage==='canceled'?'order_canceled':r.stage==='pending'?'pending':'unknown',confirmed:r.stage==='processing',started:r.started||purchaseContext().started,paymentUrl:r.url,selectionUrl:'/',check:()=>r.checkoutKey?CheckoutRecovery.synchronize():checkReturnedPayment(r.paymentId||sessionStorage.getItem('freeman-payment')),hideSupport:true,supportUrl:state.data?.bot_url})+(warning?`<p class="purchase-demo" role="status">${esc(warning)}</p>`:'');};
function resumeExternalPayment(){
  if(!state.data||document.hidden||state.view==='subscriptions')return;
  CheckoutRecovery.synchronize().then(handled=>{const id=sessionStorage.getItem('freeman-payment');if(!handled&&id)checkReturnedPayment(id)}).catch(()=>{});
}
document.addEventListener('visibilitychange',resumeExternalPayment);window.addEventListener('focus',resumeExternalPayment);window.addEventListener('pageshow',resumeExternalPayment);window.addEventListener('online',resumeExternalPayment);
