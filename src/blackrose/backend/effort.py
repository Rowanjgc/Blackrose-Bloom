"""Per-request reasoning effort. 'default' sends nothing, so presets stay authoritative."""
from __future__ import annotations

LEVELS = ('default', 'off', 'low', 'medium', 'high', 'xhigh')
LABELS = {'default': 'Default', 'off': 'Off', 'low': 'Low', 'medium': 'Medium', 'high': 'High', 'xhigh': 'XHigh'}
DEFAULT_BUDGETS = {'low': 1024, 'medium': 4096, 'high': 16384, 'xhigh': 32768}
DEFAULT_ONLY = ('default',)

# Only families verified against their wire protocol; everything else keeps the preset.
_CLOUD = (
    ('gpt-', LEVELS),
    ('claude-', ('default', 'low', 'medium', 'high', 'xhigh')),  # current Claude cannot disable thinking
)


def local_levels(caps):
    """Templates that honour enable_thinking can be switched off and budgeted per request."""
    return LEVELS if caps and caps.get('thinking') else DEFAULT_ONLY


def cloud_levels(model):
    return next((levels for prefix, levels in _CLOUD if model.startswith(prefix)), DEFAULT_ONLY)


def clamp(level, supported):
    return level if level in supported else 'default'


def local_fields(level, caps, budgets=None):
    if level == 'default' or not caps or not caps.get('thinking'):
        return {}
    if level == 'off':
        # A zero budget alone leaks the reasoning into content; the template switch does not.
        return {'chat_template_kwargs': {'enable_thinking': False}}
    fields = {'thinking_budget_tokens': (budgets or DEFAULT_BUDGETS)[level]}
    if caps.get('reasoning_effort'):
        fields['reasoning_effort'] = level
    return fields


def cloud_fields(protocol, level):
    if level == 'default':
        return {}
    if protocol == 'responses':
        return {'reasoning': {'effort': 'none' if level == 'off' else level}}
    if protocol == 'messages' and level != 'off':
        return {'output_config': {'effort': level}}
    return {}
