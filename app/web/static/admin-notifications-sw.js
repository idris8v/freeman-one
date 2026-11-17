/* Notification-only worker: never intercepts or caches account/payment requests. */
self.addEventListener('push',event=>{event.waitUntil((async()=>{
 let data;try{data=event.data.json()}catch{return}
 if(!data?.id||!data.title)return;
 await self.registration.showNotification(data.title,{body:String(data.text||''),icon:'/assets/freeman-icon-20261029.png',badge:'/assets/freeman-icon-20261029.png',tag:'freeman-'+(data.kind==='publication'?'publication-':'daily-')+data.id,renotify:false,silent:false,data:{url:typeof data.url==='string'?data.url:'/'}});
 if(self.navigator?.setAppBadge)await self.navigator.setAppBadge(1).catch(()=>{});
 const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});for(const client of windows)client.postMessage({type:data.kind==='publication'?'freeman-publication':'freeman-digest',id:data.id});
})())});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil((async()=>{
 let url=new URL(event.notification.data?.url||'/',self.location.origin);if(url.origin!==self.location.origin)url=new URL('/',self.location.origin);
 const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});const client=windows.find(c=>new URL(c.url).origin===self.location.origin);
 if(client){await client.navigate(url.href);await client.focus()}else await self.clients.openWindow(url.href);
})())});
