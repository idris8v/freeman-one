"""Launch controls shared by the website and Telegram adapters."""
CUTOFF_KEY = 'agent_v21_lifecycle_after'
MUTATION_KEY = 'agent_v21_key_mutations_enabled'

def fresh_telegram_message(message, cutoff):
    if not cutoff:
        return True
    timestamp=message.get('date')
    return type(timestamp) is int and timestamp >= cutoff

async def mutations_enabled(session):
    from app.database.repositories import SQLRepository
    return await SQLRepository(session).setting(MUTATION_KEY, '0') == '1'
