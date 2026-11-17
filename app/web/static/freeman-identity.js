/* Normalize public identities once at the JSON API boundary. Stored IDs remain compatible. */
(()=>{
 const format=value=>{const m=/^(?:FO-|FRM-|Freeman\s+)(\d+)$/i.exec(String(value||''));return m?'FO-'+Number(m[1]):value;};
 const normalize=value=>{
  if(Array.isArray(value))value.forEach(normalize);
  else if(value&&typeof value==='object')for(const key of Object.keys(value)){
   if(key==='freeman_id')value[key]=format(value[key]);else normalize(value[key]);
  }
  return value;
 };
 const original=window.fetch;
 window.fetch=async function(input,options){
  const response=await original.call(this,input,options);
  const url=new URL(typeof input==='string'||input instanceof URL?input:input.url,location.href);
  if(url.origin===location.origin&&url.pathname.startsWith('/api/')){
   const json=response.json.bind(response);response.json=async()=>normalize(await json());
  }
  return response;
 };
})();
