/* Shared authentication controller. Roles remain server-owned. */
window.FreemanAuth=(()=>{
 const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const svg=name=>name==='telegram'?'<img class="telegram-logo" src="/assets/telegram-logo.webp" alt="" aria-hidden="true">':(window.AEGIS_ICONS?.icons?.[name]||'').replace('<svg ','<svg class="aegis-svg" aria-hidden="true" ');
 let step='initial',identity='',token='',destination='',deadline=0,timer,poll,attempt=0,telegramUrl='',busy=false;
 const root=()=>document.querySelector('#app');
 const pendingKey='freeman-telegram-auth';let activeCheck=null,checking=false;
 function clearPending(){try{sessionStorage.removeItem(pendingKey)}catch{}}
 const request=async(url,body)=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store',signal:AbortSignal.timeout(20000)});const x=await r.json();if(!r.ok)throw Error(typeof x.detail==='string'?x.detail:'Не удалось продолжить. Попробуйте снова.');return x};
 const finish=()=>{clearPending();step='success';clearInterval(timer);clearTimeout(poll);draw();const target=new URLSearchParams(location.search).get('return');setTimeout(()=>location.replace((['/buy/security','/buy','/'].includes(target)||/^\/buy\?receipt=[1-9]\d*$/.test(target||''))?target:'/'),200)};
 const error=message=>{const p=root().querySelector('[data-unified-error]');if(p){p.hidden=false;p.textContent=/timeout|abort|failed to fetch/i.test(message)?'Не удалось связаться с сервером. Попробуйте снова.':message;p.focus();if(!matchMedia('(prefers-reduced-motion: reduce)').matches)p.animate([{translate:'0'},{translate:'-3px'},{translate:'3px'},{translate:'0'}],{duration:220})}};
 function draw(){
  clearInterval(timer);document.body.classList.add('auth-page','auth-unified');document.documentElement.dataset.brandReveal='done';
  const initial=step==='initial',otp=step==='otp',password=step==='password',pending=step==='telegram';
  const title=initial?'Добро пожаловать':otp?'Проверьте почту':password?'Введите пароль':pending?'Подтвердите вход':'Готово';
  const description=initial?'Подписки, ключи, баланс и поддержка —<br class="unified-subtitle-break">в одном личном кабинете.':otp?`Код подтверждения отправлен на ${esc(destination)}`:password?'Для этого аккаунта не привязана почта.':pending?'Ожидаем подтверждение…':'Вы вошли в Freeman ONE';
  const field=initial?`<label class="unified-field">${svg('mail')}<input aria-label="Почта, Freeman ID или ссылка подписки" name="identity" value="${esc(identity)}" placeholder="Почта, Freeman ID или ссылка подписки" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="2048" required></label>`:password?'<label class="unified-field"><input aria-label="Пароль" type="password" name="password" autocomplete="current-password" placeholder="Пароль" required maxlength="128"></label>':otp?`<label class="unified-code"><span class="sr-only">Код подтверждения, 6 цифр</span><div aria-hidden="true">${Array.from({length:6},()=>'<span></span>').join('')}</div><input name="code" aria-label="Код подтверждения, 6 цифр" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>`:'';
  root().innerHTML=`<section class="unified-shell"><div class="unified-card"><div class="unified-brand"><img src="/assets/freeman-mark-20261029.png" alt="Freeman ONE"><span>FREEMAN</span></div><h1>${title}</h1><p class="unified-subtitle">${description}</p>${field?`<form data-unified-auth>${password?`<input type="text" name="username" autocomplete="username" value="${esc(identity)}" hidden>`:''}${field}<button class="unified-primary" type="submit"><span>${initial?'Продолжить':password?'Войти':'Подтвердить'}</span>${svg('arrow-right')}</button></form>`:''}<p data-unified-error role="alert" tabindex="-1" hidden></p>${initial?`<p class="unified-helper">Если к аккаунту привязана почта,<br>мы отправим код подтверждения.</p><div class="unified-divider"><span>или</span></div><div class="unified-alternatives"><button type="button" data-unified-telegram>${svg('telegram')}<span>Войти через Telegram</span></button><a href="/buy" class="unified-buy">${svg('cart')}<span>Купить без регистрации</span></a></div><aside class="unified-info">${svg('info')}<p>Впервые в Freeman? Просто укажите почту — аккаунт создастся автоматически.</p></aside>`:otp?'<aside class="unified-spam-note">Письмо может попасть в папку «Спам». Если код не пришёл, проверьте её.</aside><button class="unified-text" data-unified-resend></button>':pending?`<p class="unified-helper">Подтвердите вход в Telegram и вернитесь на сайт.<br>Вход завершится автоматически.</p><a class="unified-text" href="${esc(nativeTelegram(telegramUrl))}">Открыть Telegram</a><a class="unified-text" href="${esc(telegramUrl)}">Telegram не открылся? Открыть веб-страницу</a>`:''}${!initial&&step!=='success'?'<button class="unified-text" data-unified-back>Назад</button>':''}</div></section>`;
  FreemanUI.controls(root());
  if(otp){const input=root().querySelector('[name=code]');input.addEventListener('input',()=>{input.value=input.value.replace(/\D/g,'').slice(0,6);root().querySelectorAll('.unified-code span:not(.sr-only)').forEach((box,i)=>box.textContent=input.value[i]||'')});const tick=()=>{const b=root().querySelector('[data-unified-resend]');if(!b)return;const n=Math.max(0,Math.ceil((deadline-Date.now())/1000));b.disabled=n>0;b.textContent=n?`Отправить повторно через ${n} с`:'Отправить код повторно'};tick();timer=setInterval(tick,1000);input.focus()}
  else if(password)root().querySelector('[name=password]').focus();
 }
 async function identify(){const x=await request('/api/web/auth/otp/request',{identity});if(x.step==='password'){step='password'}else{token=x.token;destination=x.destination;deadline=Date.now()+x.resend_after*1000;step='otp'}draw()}
 function nativeTelegram(value){
  const url=new URL(value);const domain=url.pathname.slice(1),start=url.searchParams.get('start');
  if(url.origin!=='https://t.me'||!/^\w{5,32}$/.test(domain)||!/^auth_[A-Za-z0-9_-]{20,64}$/.test(start||''))throw Error('Некорректная ссылка Telegram');
  return 'tg://resolve?'+new URLSearchParams({domain,start});
 }
 function pending(){try{const d=JSON.parse(sessionStorage.getItem(pendingKey)||'null');if(d&&d.expires>Date.now()&&d.expires<=Date.now()+301000&&/^[A-Za-z0-9_-]{20,64}$/.test(d.token)&&new URL(d.url).searchParams.get('start')==='auth_'+d.token){nativeTelegram(d.url);return d}}catch{}clearPending();return null}
 function waitForTelegram(d){
  clearTimeout(poll);const current=++attempt;telegramUrl=d.url;step='telegram';draw();
  activeCheck=async()=>{
   if(current!==attempt||step!=='telegram'||checking)return;
   clearTimeout(poll);
   if(Date.now()>=d.expires){clearPending();error('Время ожидания истекло. Вернитесь назад и начните вход снова.');return}
   if(document.hidden){poll=setTimeout(activeCheck,2000);return}
   checking=true;
   try{const r=await request('/api/web/auth/telegram/status',{token:d.token});if(current!==attempt)return;if(r.ok){finish();return}}
   catch(e){if(current===attempt)error(e.message)}
   finally{checking=false}
   if(current===attempt&&step==='telegram')poll=setTimeout(activeCheck,2000);
  };
  poll=setTimeout(activeCheck,1000);
 }
 function launchTelegram(url){const link=document.createElement('a');link.href=nativeTelegram(url);link.hidden=true;document.body.append(link);link.click();link.remove()}
 async function telegram(){
  if(busy)return;busy=true;const current=++attempt;
  try{const x=await request('/api/web/auth/telegram/request',{});if(current!==attempt)return;nativeTelegram(x.url);const d={token:x.token,url:x.url,expires:Date.now()+Math.min(300,x.expires_in)*1000};sessionStorage.setItem(pendingKey,JSON.stringify(d));waitForTelegram(d);launchTelegram(d.url)}
  catch(e){error(e.message)}finally{busy=false}
 }
 function resumeTelegram(){if(step==='telegram'&&activeCheck)void activeCheck()}
 document.addEventListener('submit',async e=>{const f=e.target.closest('[data-unified-auth]');if(!f)return;e.preventDefault();if(busy)return;busy=true;root().querySelector('[data-unified-error]').hidden=true;const b=f.querySelector('[type=submit]'),label=b.innerHTML;b.disabled=true;b.setAttribute('aria-busy','true');b.innerHTML='<span class="unified-spinner" aria-hidden="true"></span><span>Проверяем…</span>';const values=Object.fromEntries(new FormData(f));try{if(step==='initial'){identity=values.identity.trim();await identify()}else if(step==='password'){await request('/api/buy/login',{identity,password:values.password});finish()}else if(step==='otp'){await request('/api/web/auth/otp/verify',{token,code:values.code});finish()}}catch(problem){error(problem.message)}finally{busy=false;if(b.isConnected){b.disabled=false;b.removeAttribute('aria-busy');b.innerHTML=label}}});
 document.addEventListener('click',async e=>{if(e.target.closest('[data-unified-back]')){++attempt;clearTimeout(poll);clearPending();activeCheck=null;token='';step='initial';draw();root().querySelector('[name=identity]').focus()}else if(e.target.closest('[data-unified-telegram]'))await telegram();else if(e.target.closest('[data-unified-resend]')){if(busy||Date.now()<deadline)return;busy=true;e.target.disabled=true;try{await identify()}catch(problem){error(problem.message);e.target.disabled=false}finally{busy=false}}});
 window.addEventListener('pagehide',()=>{clearTimeout(poll);clearInterval(timer)});
 window.addEventListener('focus',resumeTelegram);document.addEventListener('visibilitychange',resumeTelegram);window.addEventListener('pageshow',resumeTelegram);
 return {show:()=>{const d=pending();if(d)waitForTelegram(d);else draw()},telegram};
})();
