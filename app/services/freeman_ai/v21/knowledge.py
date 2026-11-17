"""Idempotent activation of a reviewed KB version in the existing database."""
import json
from pathlib import Path
from sqlalchemy import select,update
from app.database.models import AIKnowledge
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from .registry import articles,search
VERSION=21
PROVENANCE=json.loads(Path(__file__).with_name('provenance.json').read_text())

async def install(session, *, article_ids=None):
    """Caller owns transaction. This does not enable auto replies."""
    await lock_nonce(session,'agent-v21-knowledge-install')
    repo=SQLRepository(session)
    selected=[]
    records=articles()
    if article_ids is not None:
        records=[record for record in records if record['id'] in article_ids]
        if {record['id'] for record in records}!=set(article_ids):
            raise ValueError('unknown_article_id')
    for record in records:
        mapping='agent_v21_article:'+record['id']
        rid=await repo.setting(mapping)
        row=await session.get(AIKnowledge,int(rid)) if rid and rid.isdigit() else None
        body=json.dumps(record,ensure_ascii=False)
        if row is None:
            row=AIKnowledge(title=record['id']+' · '+record['intent'],text=body,enabled=True,version=VERSION)
            session.add(row);await session.flush()
        else:
            row.title=(record['id']+' · '+record['intent'])[:160];row.text=body;row.enabled=True;row.version=VERSION
        selected.append(row.id);await repo.set_setting(mapping,str(row.id))
    if article_ids is None:
        await session.execute(update(AIKnowledge).where(AIKnowledge.id.not_in(selected)).values(enabled=False))
        await repo.set_setting('agent_v21_knowledge_version','2.1')
        await repo.set_setting('agent_v21_spec_hash',PROVENANCE['spec_sha256'])
    return dict(version='2.1',articles=len(selected),enabled=False)

async def retrieve(session,query,*,context=""):
    repo=SQLRepository(session)
    if await repo.setting('agent_v21_spec_hash')!=PROVENANCE['spec_sha256']:
        raise ValueError('knowledge_version_not_installed')
    found=[]
    for row in await session.scalars(select(AIKnowledge).where(AIKnowledge.version==VERSION,AIKnowledge.enabled.is_(True)).order_by(AIKnowledge.id)):
        try:record=json.loads(row.text)
        except (TypeError,ValueError):continue
        if record.get('version')=='2.1' and record.get('active') and record.get('id'):
            found.append(record)
    if not found:raise ValueError('knowledge_version_empty')
    return dict(version='2.1', articles=search(query, rows=found, context=context),
                index={article['id']: article['intent'] for article in found})
