(()=>{'use strict';const list=document.querySelector('#agent-messages'),form=document.querySelector('#agent-form'),field=document.querySelector('#agent-input'),button=document.querySelector('#agent-send'),status=document.querySelector('#agent-status');let busy=false,pending=null;
async function api(path,data){const r=await fetch('/api/support-agent/'+path,{signal:AbortSignal.timeout(75000),method:data?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});let x;try{x=await r.json()}catch{throw Error('Сервис временно недоступен. Попробуйте ещё раз.')}if(!r.ok)throw Error(typeof x.detail==='string'?x.detail:'Не удалось отправить сообщение.');return x}
function render(x){list.replaceChildren();for(const item of x.items){const node=document.createElement('div');node.className='fo-support-agent-bubble';node.dataset.role=item.role;node.innerHTML=FreemanComponents.messageBody({source:item.text,media:(item.attachments||[]).map(FreemanComponents.media).join('')+(item.ping_guide?'<figure class="fo-chat-media fo-chat-photo"><img src="/assets/support-ping-guide.png" width="1176" height="114" alt="В Happ значок спидометра обведён красным справа от кнопки обновления" loading="lazy" decoding="async" style="width:100%;height:auto"></figure>':''),inline:item.actions||[]});list.append(node)}list.scrollTop=list.scrollHeight;status.textContent=x.mode==='human'?'Переписка сохранена. Для помощи специалиста откройте поддержку в кабинете.':'';button.disabled=x.mode==='human';field.disabled=x.mode==='human'}
form.addEventListener('submit',async e=>{e.preventDefault();if(busy||!field.value.trim())return;busy=true;button.disabled=true;const body=field.value.trim();if(!pending||pending.text!==body)pending={text:body,request_id:crypto.randomUUID()};status.textContent='Помощник готовит ответ…';try{const x=await api('message',pending);pending=null;field.value='';render(x)}catch(e){status.textContent=e.message;button.disabled=false}finally{busy=false}});
list.addEventListener('click', event => {
 const option = event.target.closest('[data-chat-reply-choice]');
 if (!option || busy || field.disabled) return;
 const choice = option.dataset.chatReplyChoice;
 if (field.value.trim() && field.value !== choice) {
  status.textContent = 'Сначала отправьте или очистите черновик';
  return;
 }
 field.value = choice;
 if (option.hasAttribute('data-chat-ask-service')) field.focus({preventScroll:true});
 form.requestSubmit();
});
api('state').then(render).catch(e=>{status.textContent=e.message;button.disabled=true});
document.addEventListener('fo-agent-result', async event => {
  try { render(await api('state')); if(event.detail?.focusComposer && !field.disabled) field.focus({preventScroll:true}); }
  catch (error) { status.textContent = error.message; }
});
})();
