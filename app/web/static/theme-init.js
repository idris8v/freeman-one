document.documentElement.classList.add('fo-document');
// Explicit automatic surfaces follow the device; other surfaces retain preferences.
(()=>{
 const root=document.documentElement,system=matchMedia('(prefers-color-scheme: dark)');
 const saved=()=>{try{return localStorage.getItem('freeman-theme')}catch{return null}};
 const apply=()=>{const preference=(root.dataset.uiBundle==='guest'||root.dataset.themeMode==='system')?null:saved(),theme=['light','dark'].includes(preference)?preference:system.matches?'dark':'light';if(root.dataset.theme===theme)return;root.dataset.theme=theme;window.dispatchEvent(new CustomEvent('freeman-theme-change',{detail:theme}))};
 apply();system.addEventListener('change',apply);
 window.addEventListener('pageshow',apply);window.addEventListener('focus',apply);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)apply()});
 window.addEventListener('storage',event=>{if(event.key==='freeman-theme'||event.key===null)apply()});
})();
