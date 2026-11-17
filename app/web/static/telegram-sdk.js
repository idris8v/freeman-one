/* Telegram SDK is optional and must never block cabinet startup. */
(()=>{
 if(!/(?:[?#&])tgWebAppData=/.test(location.hash+location.search))return;
 const script=document.createElement('script');script.src='https://telegram.org/js/telegram-web-app.js';script.async=true;
 script.onload=()=>{try{window.Telegram?.WebApp?.ready();window.Telegram?.WebApp?.expand()}catch{}};
 document.head.append(script);
})();
