"""Read-only usage from persisted provider events; never infer billing or message attribution."""
import json
from sqlalchemy import select
from app.database.models import AIEvent

async def conversation_usage(session, conversation_id):
    events = await session.scalars(select(AIEvent).where(
        AIEvent.conversation_id == conversation_id, AIEvent.kind == 'model.usage'
    ).order_by(AIEvent.id))
    total = reports = rounds = invalid = 0
    for event in events:
        try:
            data = json.loads(event.details)
            value = data.get('total_tokens') if isinstance(data, dict) else None
            if type(value) is not int or value < 0:
                invalid += 1
                continue
            total += value
            reports += 1
            count = data.get('rounds')
            if type(count) is int and count >= 0:
                rounds += count
        except (TypeError, ValueError):
            invalid += 1
    return {'total_tokens': total if reports else None, 'recorded_responses': reports,
            'recorded_rounds': rounds, 'invalid_reports': invalid,
            'scope': 'persisted_conversation_events', 'cost': None}
