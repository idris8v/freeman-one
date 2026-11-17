/* One reusable decorative scene. No timers, frame loop, API calls or referral calculations. */
window.ReferralAnimatedHero = (() => {
  'use strict';
  const base = '/assets/referral-animated/';
  const files = {background:'referral_background.png?v=6855a18a893f',body:'referral-body-balanced.png?v=92df313566da',head:'referral-head-calm-up.png?v=98e564051e38',fallback:'referral-base-balanced.png?v=4f3dc8914ac7'};
  // Sprite 05 contains a clipped second coin; use the five clean single coins.
  const coins = ['coin_gold_01.png?v=f767c999056b','coin_gold_02.png?v=ee8f14a60981','coin_gold_03.png?v=7e4cf04c67f1','coin_gold_04.png?v=403759b0f7c7','coin_gold_06.png?v=143af2964b68'];
  let current = null, visitOwner = null, prepared = null, pictures = null;
  function rig() {
    const body = `<image href="${base+files.body}" width="1671" height="941"/>`;
    const lid = (cx,cy,rx,ry,angle,clip) => `<g clip-path="url(#${clip})"><g transform="rotate(${angle} ${cx} ${cy})"><ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="url(#referral-eye-shade)"/><path d="M${cx-23} ${cy}Q${cx} ${cy+8} ${cx+23} ${cy}" fill="none" stroke="#56edff" stroke-width="6" stroke-linecap="round"/></g></g>`;
    return `<svg class="referral-hero-illustration referral-hero-rig" viewBox="0 0 1671 941" width="1671" height="941" preserveAspectRatio="xMaxYMax meet" aria-hidden="true" focusable="false">
      <defs>
        <clipPath id="referral-body-trim"><rect x="0" y="315" width="1671" height="626"/></clipPath>
        <clipPath id="referral-head-trim"><rect x="490" y="180" width="700" height="525"/></clipPath>
        <clipPath id="referral-foreground-hand"><path d="M453 416Q475 389 495 396L513 394Q546 391 569 429L578 444L581 412Q593 387 609 405Q622 419 628 451L637 481Q634 508 607 528Q580 543 551 528L512 502L478 473Q451 449 453 416Z"/></clipPath>
        <clipPath id="referral-eye-left-area"><rect x="680" y="315" width="162" height="210"/></clipPath>
        <clipPath id="referral-eye-right-area"><rect x="870" y="300" width="200" height="138"/></clipPath>
        <mask id="referral-smile-guard" maskUnits="userSpaceOnUse" maskContentUnits="userSpaceOnUse" x="0" y="0" width="1671" height="941"><rect width="1671" height="941" fill="white"/><rect x="835" y="435" width="100" height="65" rx="4" fill="black"/></mask>
        <radialGradient id="referral-eye-shade"><stop offset=".72" stop-color="#071b25"/><stop offset="1" stop-color="#071b25" stop-opacity="0"/></radialGradient>
      </defs>
      <g clip-path="url(#referral-body-trim)" class="referral-hero-base">${body}</g>
      <g class="referral-hero-head"><g transform="translate(190 -5) scale(.747)" data-referral-head-art>
        <g clip-path="url(#referral-head-trim)"><image href="${base+files.head}" width="1670" height="942"/></g>
        <g class="referral-hero-eyelids" mask="url(#referral-smile-guard)">${lid(776.6,409.4,90,78,-8,'referral-eye-left-area')}${lid(955.9,390.6,87,70,-8,'referral-eye-right-area')}</g>
      </g></g>
      <g clip-path="url(#referral-foreground-hand)">${body}</g>
    </svg>`;
  }
  function render({rate}) {
    const value = Number.isFinite(Number(rate)) ? Number(rate).toLocaleString('ru-RU') : '0';
    return `<section class="referral-hero" data-referral-animated-hero data-animation-state="idle" aria-labelledby="referral-hero-title"><img class="referral-hero-background" src="${base}${files.background}" width="2048" height="768" alt="" decoding="async" aria-hidden="true"><div class="referral-hero-copy"><h2 id="referral-hero-title">Получайте до <em>${value}%</em> с покупок друзей</h2><p>Приглашайте друзей в Freeman ONE и получайте вознаграждение за их покупки.</p></div><div class="referral-hero-scene" aria-hidden="true"><div class="referral-hero-rain referral-hero-rain--back"></div>${rig()}<div class="referral-hero-rain referral-hero-rain--front"></div></div></section>`;
  }
  const decode = url => {const img = new Image();img.decoding = 'async';img.src = base+url;return img.decode();};
  function prepare() {
    if(!prepared)prepared=Promise.all(coins.map(decode)).catch(e=>{prepared=null;throw e;});
    return prepared;
  }
  function preparePictures() {
    if(!pictures)pictures=Promise.all([files.background,files.body,files.head].map(decode)).catch(e=>{pictures=null;throw e;});
    return pictures;
  }
  function mount(root) {
    if(!root.querySelector('.referral-hero-rig')){root.querySelector('.referral-hero-illustration')?.remove();root.querySelector('.referral-hero-scene').insertAdjacentHTML('beforeend',rig());root.removeAttribute('data-ready');root.removeAttribute('data-image-error');}
    const scene = root.querySelector('.referral-hero-scene'), illustration = root.querySelector('.referral-hero-illustration');
    const back = root.querySelector('.referral-hero-rain--back'), front = root.querySelector('.referral-hero-rain--front'), lids = root.querySelector('.referral-hero-eyelids');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)'), controller = new AbortController(), animations = new Set();
    let ready = false, visible = false, fraction = 0, disposed = false, generation = 0, starting = false, sizeKey = '';
    const setState = value => root.dataset.animationState = value;
    const enabled = () => !disposed && ready && visible && !document.hidden && !reduced.matches;
    function clear() {generation++;starting = false;for (const a of animations) a.cancel();animations.clear();back.replaceChildren();front.replaceChildren();setState('idle');}
    const track = a => {animations.add(a);return a;};
    function update() {
      if (disposed) return;
      if (reduced.matches) {clear();return;}
      if (!enabled()) {for (const a of animations) a.pause();setState(animations.size ? 'paused' : 'idle');return;}
      if (!animations.size && fraction >= .5) start();
      else if (animations.size) {for (const a of animations) if (a.playState !== 'running') a.play();setState('playing');}
    }
    async function start() {
      if (!enabled() || starting || animations.size) return;
      starting = true;const run = ++generation;
      try {await prepare();} catch (_) {if (run === generation) starting = false;return;}
      if (run !== generation || !enabled()) {if (run === generation) starting = false;return;}
      starting = false;
      const bounds = scene.getBoundingClientRect(), body = illustration.getBoundingClientRect(), mobile = root.getBoundingClientRect().width <= 560;
      const bodyLeft = body.left - bounds.left, bodyWidth = body.width, count = mobile ? 8 : 16;
      const clampX = (x, s) => Math.max(2, Math.min(bounds.width - s - 2, x));
      for (let i = 0; i < count; i++) {
        const lane = i % 8, near = i % 4 === 2, size = mobile ? 15 + (i%3)*3 : 22 + (i%3)*4;
        const relative = (near ? (lane === 2 ? .12 : .90) : [.24,.7,.42,.84,.54,.3,.62,.9][lane]) + (i >= 8 ? (i % 2 ? -.03 : .03) : 0);
        const x = clampX(bodyLeft + bodyWidth*relative - size/2,size), drift = (i%2?1:-1)*(mobile?6+i:12+i*1.5);
        const x0 = clampX(x-drift,size), y0 = -size-12, y = bounds.height-size-(near?7:22), spin = (i%2?-1:1)*(240+lane*37), bounce = mobile?5:8;
        const img = document.createElement('img');img.alt='';img.src=base+coins[i%coins.length];img.className=near?'referral-hero-coin':'referral-hero-coin referral-hero-coin--distant';img.width=size;img.style.width=size+'px';(near?front:back).append(img);
        const pose=(px,py,r)=>`translate3d(${px}px,${py}px,0) rotate(${r}deg)`, duration=(mobile?1300:1500)+(i%3)*90;
        track(img.animate([
          {transform:pose(x0,y0,0),opacity:0,offset:0,easing:'cubic-bezier(.45,0,.85,.45)'},
          {transform:pose(x0+(x-x0)*.08,y0+(y-y0)*.055,spin*.1),opacity:near?1:.8,offset:.12,easing:'cubic-bezier(.5,0,.85,.45)'},
          {transform:pose(x,y,spin),opacity:near?1:.8,offset:.78,easing:'cubic-bezier(.2,.7,.3,1)'},
          {transform:pose(x,y-bounce,spin+8),opacity:near?1:.74,offset:.87,easing:'cubic-bezier(.45,0,.8,.5)'},
          {transform:pose(x,y,spin+13),opacity:near?.9:.6,offset:.94},
          {transform:pose(x,y+2,spin+16),opacity:0,offset:1}
        ],{duration,delay:-duration*(i+.25)/count,iterations:Infinity,easing:'linear'}));
      }
      // The upward-looking head stays still. Only coins and a soft blink move.
      track(lids.animate([
        {opacity:0,offset:0},{opacity:0,offset:.2},{opacity:1,offset:.212},{opacity:1,offset:.23},{opacity:0,offset:.244},
        {opacity:0,offset:.7},{opacity:1,offset:.712},{opacity:1,offset:.728},{opacity:0,offset:.742},{opacity:0,offset:1}
      ],{duration:6600,iterations:Infinity,easing:'linear'}));
      setState('playing');
    }
    reduced.addEventListener('change', update);
    document.addEventListener('visibilitychange', update, {signal:controller.signal});
    // Enter/leave never allocates a second pool or resets the running timeline.
    root.addEventListener('pointerenter', update, {passive:true,signal:controller.signal});
    const observer = new IntersectionObserver(entries => {const e=entries[0];visible=e.isIntersecting;fraction=e.intersectionRatio;update();},{threshold:[0,.5,1]});observer.observe(root);
    const resize = new ResizeObserver(entries => {const b=entries[0].contentRect,key=Math.round(b.width)+':'+Math.round(b.height);if(sizeKey && key!==sizeKey){clear();update();}sizeKey=key;});resize.observe(scene);
    preparePictures().then(()=>{if(disposed)return;ready=true;root.setAttribute('data-ready','');update();}).catch(()=>{if(disposed)return;clear();root.setAttribute('data-image-error','');root.setAttribute('data-ready','');const img=new Image();img.className='referral-hero-illustration';img.alt='';img.width=1670;img.height=941;img.src=base+files.fallback;illustration.replaceWith(img);});
    return {root,destroy(){disposed=true;controller.abort();observer.disconnect();resize.disconnect();reduced.removeEventListener('change',update);clear();}};
  }
  function sync(container, owner='') {
    const root=container?.querySelector('[data-referral-animated-hero]')||null;
    if(!root){current?.destroy();current=null;visitOwner=null;return;}
    if(current?.root===root && visitOwner===owner)return;
    if(current && visitOwner===owner && !current.root.isConnected){const previous=current.root;previous.querySelector('h2').replaceChildren(...[...root.querySelector('h2').childNodes].map(n=>n.cloneNode(true)));previous.querySelector('.referral-hero-copy p').textContent=root.querySelector('.referral-hero-copy p').textContent;root.replaceWith(previous);return;}
    current?.destroy();visitOwner=owner;current=mount(root);
  }
  return {render,sync};
})();
