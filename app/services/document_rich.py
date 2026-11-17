"""Native Rich Message document layout, preserving the source wording."""
import re
from aiogram.types import InputRichMessage

def document_layout(title,body):
    # Keep paragraphs in one RichText block with explicit blank lines: some
    # mobile clients pack adjacent paragraph blocks without visible spacing.
    paragraphs=[part.strip() for part in re.split(r'\n+',body) if part.strip()]
    formatted='\n\n'.join(paragraphs)
    return InputRichMessage(blocks=[
        dict(type='heading',text=title,size=1),
        dict(type='divider'),
        dict(type='paragraph',text=formatted),
    ],skip_entity_detection=True)
