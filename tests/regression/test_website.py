import hashlib
import hmac
import time
import base64

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from test_bot import database
from app.config.settings import Settings
from app.database.repositories import SQLRepository
from app.database.models import (User, AccessKey, Subscription, Device, Payment, Referral,
                                 BalanceTransaction, Withdrawal, now)
from app.services.domain import seed
from app.web.routes import website_router














def telegram_values(settings,telegram_id,name,username):
    values={'id':str(telegram_id),'first_name':name,'username':username,'auth_date':str(int(time.time()))}
    check='\n'.join(f'{key}={value}' for key,value in sorted(values.items()))
    values['hash']=hmac.new(hashlib.sha256(settings.bot_token.encode()).digest(),check.encode(),hashlib.sha256).hexdigest()
    return values
