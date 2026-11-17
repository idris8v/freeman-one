"""Versioned dialogue state; waiting for an operator is not operator takeover."""
from __future__ import annotations
from dataclasses import dataclass,field,asdict,fields
import json
PHASES={'clarification','waiting_customer','tool_running','waiting_operator','operator_running','completed'}
@dataclass
class DialogueState:
 version:str='2.1'
 channel:str=''
 chat:str=''
 verified_customer:int|None=None
 selected_key:int|None=None
 device_model:str=''
 os:str=''
 application:str=''
 application_version:str=''
 symptoms:list=field(default_factory=list)
 network:str=''
 checks:list=field(default_factory=list)
 expected_answer:str=''
 phase:str='clarification'
 operator_reason:str=''
 financial_request:str=''
 technical_resolved:bool=False
 generation:int=0
 @property
 def can_answer(self):return self.phase not in {'operator_running','completed'}
 def new_turn(self):self.generation+=1;return self.generation
 def accepts(self,generation):return self.can_answer and generation==self.generation
 def record_check(self,name,scope,result):
  if not all(isinstance(v,str) and len(v)<=500 for v in (name,scope,result)):raise ValueError('Invalid diagnostic result')
  self.checks=[c for c in self.checks if not (c['check']==name and c['scope']==scope)][-63:]+[{'check':name,'scope':scope,'result':result}]
  if self.phase!='waiting_operator':self.phase='waiting_customer'
 def should_check(self,name,scope):return not any(c['check']==name and c['scope']==scope for c in self.checks)
 def request_operator(self,reason):self.operator_reason=reason;self.phase='waiting_operator'
 def operator_reply(self):self.generation+=1;self.phase='operator_running'
 def return_to_agent(self):self.generation+=1;self.phase='clarification'
 def resolve_technical(self):self.technical_resolved=True
 def to_json(self):return json.dumps(asdict(self),ensure_ascii=False,sort_keys=True)
 @classmethod
 def from_json(cls,value):
  data=json.loads(value)
  if not isinstance(data,dict) or set(data)-{f.name for f in fields(cls)}:raise ValueError('Unexpected state fields')
  result=cls(**data)
  if result.version!='2.1' or result.phase not in PHASES or type(result.generation) is not int or result.generation<0:raise ValueError('Invalid state version/phase')
  return result

class StateStore:
 """Use the existing settings table and transaction; no independent customer DB."""
 def __init__(self,session,settings):self.session,self.settings=session,settings
 async def load(self,conversation):
  from app.database.repositories import SQLRepository
  from cryptography.fernet import Fernet
  raw=await SQLRepository(self.session).setting('agent_v21_state:'+str(conversation.id))
  if not raw:return DialogueState(channel=conversation.channel,chat=str(conversation.id),generation=conversation.generation)
  return DialogueState.from_json(Fernet(self.settings.encryption_key.encode()).decrypt(raw.encode()).decode())
 async def save(self,conversation,state,expected_generation):
  from app.services.freeman_ai.service import lock_ticket
  from app.database.models import AIConversation
  from app.database.repositories import SQLRepository
  from cryptography.fernet import Fernet
  # Same Ticket -> Conversation order as inbound/operator and worker commit.
  await lock_ticket(self.session,conversation)
  current=await self.session.get(AIConversation,conversation.id,with_for_update=True,populate_existing=True)
  if not current or current.generation!=expected_generation:raise ValueError('Conversation superseded')
  value=Fernet(self.settings.encryption_key.encode()).encrypt(state.to_json().encode()).decode()
  await SQLRepository(self.session).set_setting('agent_v21_state:'+str(conversation.id),value)
