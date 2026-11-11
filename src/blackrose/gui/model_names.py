"""Readable model labels without changing provider identifiers."""
import re


def model_display_name(model_id, catalog=()):
    if not model_id:
        return ''
    short_id = model_id.rsplit('/', 1)[-1]
    row = next((m for m in catalog if m['id'] == model_id), {})
    name = row.get('name')
    if name and name not in (model_id, short_id):
        return name
    if ' ' in short_id:
        return short_id
    acronyms = {'it', 'gpt', 'oss', 'glm', 'vl', 'vlm', 'moe'}
    brands = {'deepseek': 'DeepSeek', 'opencode': 'OpenCode'}
    return ' '.join(
        brands.get(part.lower()) or
        (part.upper() if part.lower() in acronyms or re.fullmatch(r'[ae]?\d+(?:\.\d+)?b', part, re.I)
         else part.capitalize())
        for part in re.split(r'[-_]+', short_id) if part)
