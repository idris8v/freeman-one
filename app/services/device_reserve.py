"""Commercial allowances stay N; marked panel users carry N + one spare slot.

The marker versions only the panel transport representation. Legacy unmarked
users and persisted creation/renewal snapshots retain their original meaning.
No pricing code, tariff or application API gains a billable device.
"""
from copy import deepcopy
MARKER=' [FO:HWID_RESERVE_V1]'
NULL_MARKER=' [FO:HWID_RESERVE_V1:NULL]'

def technical(commercial):
    if type(commercial) is not int or not 0<=commercial<2**31-1:
        raise ValueError('Некорректный оплаченный лимит устройств.')
    return commercial+1 if commercial else 0

def has_reserve(row):
    return isinstance(row,dict) and isinstance(row.get('description'),str) and row['description'].endswith((MARKER,NULL_MARKER))

def description(value):
    if value is None or value==NULL_MARKER:return None
    if not isinstance(value,str):raise ValueError('Некорректное описание пользователя панели.')
    return value[:-len(MARKER)] if value.endswith(MARKER) else value

def encode(body,current=None):
    wire=deepcopy(body)
    if 'hwidDeviceLimit' not in wire:return wire
    original=wire.get('description',(current or {}).get('description'))
    original=description(original)
    value=wire['hwidDeviceLimit']
    if value is None or type(value) is int and value==0:
        if has_reserve(current) or has_reserve(wire):wire['description']=original
        return wire
    wire['hwidDeviceLimit']=technical(value)
    wire['description']=NULL_MARKER if original is None else original+MARKER
    return wire

def decode(payload):
    """Normalize marked user records, including paginated/recovery responses."""
    if isinstance(payload,list):return [decode(item) for item in payload]
    if not isinstance(payload,dict):return payload
    result={key:decode(value) for key,value in payload.items()}
    if has_reserve(payload) and 'hwidDeviceLimit' in payload:
        limit=payload.get('hwidDeviceLimit')
        if type(limit) is not int or not 2<=limit<=2**31-1:
            raise ValueError('Некорректный технический резерв в панели.')
        result['hwidDeviceLimit']=limit-1
        result['description']=description(payload['description'])
    return result
