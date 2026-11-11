"""Bounded, tool-free source repair shared by generated answer formats."""
from .backend.stream_limits import bounded_choices
from .prompts import Prompt


async def repair_text(chat, messages, options, builder, stream_budget):
    builder.admit(Prompt(messages=messages, instructions=messages[0]['content']), [])
    stream_options = dict(options)
    if getattr(chat.backend, 'accepts_stream_budget', False):
        stream_options['stream_budget'] = stream_budget
    text = ''
    stream = chat.backend.stream(chat.model_name, messages, [], **stream_options)
    async for choice in bounded_choices(stream, stream_budget):
        delta = choice.get('delta') or {}
        if delta.get('content'):
            stream_budget.add_output('reply', delta['content'])
            text += delta['content']
        if delta.get('reasoning_content'):
            stream_budget.add_output('reasoning', delta['reasoning_content'])
        for fragment in delta.get('tool_calls') or []:
            function = fragment.get('function') or {}
            stream_budget.add_output('reply', function.get('name', ''))
            stream_budget.add_output('reply', function.get('arguments', ''))
            stream_budget.add_output('reply', fragment.get('id') or '')
    return text
