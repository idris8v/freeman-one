/* Organize admin key controls while preserving existing business handlers. */
(()=>{
 const originalDevices=keysDevices;let deviceRequest=0;
 keysDevices=async function(id){
  const request=++deviceRequest,view=state.view,tab=state.extra.infrastructureTab;
  state.modal=keyPilotComponents('<div class="modal-backdrop"><section class="form-modal keys-device-modal" role="dialog" aria-modal="true"><button data-close-modal aria-label="Закрыть">'+actionIcon('close')+'</button><h2>Подключённые устройства</h2><p role="status">Загружаем устройства…</p></section></div>');render();
  const loading=state.modal;const isCurrent=()=>request===deviceRequest&&state.view===view&&state.extra.infrastructureTab===tab&&state.modal===loading;
  try{const applied=await originalDevices(id,{isCurrent});if(applied===false)return;const item=state.extra['admin-keysList']?.items?.find(k=>k.id===id);if(item){const detail=state.modal.match(/<strong>(\d+) из/);if(detail)item.device_count=Number(detail[1]);}}
  catch(error){if(!isCurrent())return;state.modal=keyPilotComponents('<div class="modal-backdrop"><section class="form-modal" role="dialog" aria-modal="true"><button data-close-modal aria-label="Закрыть">'+actionIcon('close')+'</button><h2>Устройства недоступны</h2><p>'+esc(error.message)+'</p><button data-keys-devices="'+id+'">Повторить</button></section></div>');render();}
 };
})();
