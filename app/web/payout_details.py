import re
from fastapi import HTTPException
from pydantic import BaseModel, Field

class PayoutDetailsPayload(BaseModel):
    owner_name: str = Field(min_length=3, max_length=120)
    bank: str = Field(min_length=2, max_length=100)
    method: str = Field(pattern='^(phone|card)$')
    account: str = Field(min_length=10, max_length=32)

def normalize_details(payload):
    data = payload.model_dump()
    for field in ('owner_name', 'bank'):
        data[field] = data[field].strip()
    if len(data['owner_name']) < 3 or len(data['bank']) < 2:
        raise HTTPException(422, 'Укажите имя получателя и банк')
    if not re.fullmatch(r'[+\d\s()-]+', data['account']):
        raise HTTPException(422, 'Проверьте номер карты или телефона')
    digits = re.sub(r'\D', '', data['account'])
    if data['method'] == 'card':
        if len(digits) not in (16, 18, 19):
            raise HTTPException(422, 'Проверьте номер карты')
        data['account'] = digits
    else:
        if len(digits) == 10:
            digits = '7' + digits
        elif len(digits) == 11 and digits.startswith('8'):
            digits = '7' + digits[1:]
        if len(digits) != 11 or not digits.startswith('7'):
            raise HTTPException(422, 'Укажите российский номер телефона для СБП')
        data['account'] = '+' + digits
    return data

def masked_details(data):
    if not data:
        return None
    digits = re.sub(r'\D', '', data['account'])
    return {**data, 'account': '•••• ' + digits[-4:]}
