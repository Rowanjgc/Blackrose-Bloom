"""Translate Go's native APIs to the chat controller's streaming tool contract."""
from __future__ import annotations

import json
import re

from .effort import cloud_fields


def protocol_for(model):
    if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9._-]+', model):
        return None
    if model.startswith(('gpt-', 'grok-', 'muse-')):
        return 'responses'
    if model.startswith(('claude-', 'minimax-', 'qwen')):
        return 'messages'
    if model.startswith('gemini-'):
        return 'google'
    return 'chat'


def content_parts(content, protocol):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if protocol == 'messages' else [{'text': content}]
    parts = []
    for part in content or []:
        if part['type'] == 'text':
            parts.append({'type': 'text', 'text': part['text']} if protocol == 'messages'
                         else {'text': part['text']})
        elif part['type'] == 'image_url':
            url = part['image_url']['url']
            if not url.startswith('data:'):
                raise ValueError('Only embedded conversation images are supported')
            mime, data = url[5:].split(';base64,', 1)
            parts.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}}
                         if protocol == 'messages' else {'inlineData': {'mimeType': mime, 'data': data}})
    return parts


def prepare(protocol, model, messages, tools, effort='default'):
    path, body = _prepare(protocol, model, messages, tools)
    body.update(cloud_fields(protocol, effort))
    return path, body


def _prepare(protocol, model, messages, tools):
    functions = [tool['function'] for tool in tools]
    if protocol == 'chat':
        body = {'model': model, 'stream': True, 'stream_options': {'include_usage': True},
                'messages': [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages]}
        if tools:
            body.update(tools=list(tools), tool_choice='auto')
        return '/chat/completions', body
    system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
    if protocol == 'messages':
        converted = []
        for message in messages:
            role = message['role']
            if role == 'system':
                continue
            if role == 'tool':
                role, parts = 'user', [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                                        'content': message['content']}]
            elif message.get('_provider_content', {}).get('protocol') == protocol:
                parts = message['_provider_content']['content']
            else:
                parts = content_parts(message.get('content'), protocol)
                parts += [{'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                           'input': json.loads(call['function']['arguments'])}
                          for call in message.get('tool_calls', [])]
            if converted and converted[-1]['role'] == role:
                converted[-1]['content'].extend(parts)
            else:
                converted.append({'role': role, 'content': parts})
        body = {'model': model, 'system': system, 'messages': converted, 'stream': True, 'max_tokens': 16384}
        if functions:
            body['tools'] = [{'name': f['name'], 'description': f.get('description', ''),
                              'input_schema': f['parameters']} for f in functions]
        return '/messages', body
    if protocol == 'responses':
        items = []
        for message in messages:
            role = message['role']
            if role == 'system':
                continue
            if role == 'tool':
                items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                              'output': message['content']})
            elif message.get('_provider_content', {}).get('protocol') == protocol:
                items.extend(message['_provider_content']['content'])
            else:
                content = message.get('content')
                if content:
                    if isinstance(content, list):
                        content = [({'type': 'input_image', 'image_url': p['image_url']['url']}
                                    if p['type'] == 'image_url' else
                                    {'type': 'input_text' if role == 'user' else 'output_text', 'text': p['text']})
                                   for p in content]
                    items.append({'role': role, 'content': content})
                items.extend({'type': 'function_call', 'call_id': call['id'],
                              'name': call['function']['name'], 'arguments': call['function']['arguments']}
                             for call in message.get('tool_calls', []))
        body = {'model': model, 'instructions': system, 'input': items, 'stream': True,
                'store': False, 'include': ['reasoning.encrypted_content']}
        if functions:
            body['tools'] = [{'type': 'function', **f, 'strict': False} for f in functions]
        return '/responses', body
    converted = []
    names = {call['id']: call['function']['name'] for m in messages for call in m.get('tool_calls', [])}
    for message in messages:
        role = message['role']
        if role == 'system':
            continue
        role = 'model' if role == 'assistant' else 'user'
        if message['role'] == 'tool':
            response = {'name': names[message['tool_call_id']], 'response': {'result': message['content']}}
            for previous in messages:
                for part in previous.get('_provider_content', {}).get('content', []):
                    call = part.get('functionCall', {})
                    if call.get('id') == message['tool_call_id']:
                        response['id'] = call['id']
            parts = [{'functionResponse': response}]
        elif message.get('_provider_content', {}).get('protocol') == protocol:
            parts = message['_provider_content']['content']
        else:
            parts = content_parts(message.get('content'), protocol)
            parts += [{'functionCall': {'name': c['function']['name'],
                        'args': json.loads(c['function']['arguments'])}} for c in message.get('tool_calls', [])]
        if converted and converted[-1]['role'] == role:
            converted[-1]['parts'].extend(parts)
        else:
            converted.append({'role': role, 'parts': parts})
    body = {'systemInstruction': {'parts': [{'text': system}]}, 'contents': converted}
    if functions:
        body['tools'] = [{'functionDeclarations': [
            {'name': f['name'], 'description': f.get('description', ''),
             'parametersJsonSchema': f['parameters']} for f in functions]}]
    return f'/models/{model}:streamGenerateContent?alt=sse', body


def delta(**values):
    return {'delta': values}


class StreamDecoder:
    def __init__(self, protocol):
        self.protocol = protocol
        self.completed = False
        self.blocks = {}
        self.arguments = {}
        self.native = []
        self.finish = None
        self.calls = 0
        self.usage = {}

    def end(self, reason=None):
        reason = reason or self.finish
        if reason is None:
            raise RuntimeError('OpenCode Go stream ended without a finish reason.')
        self.completed = True
        return {'delta': {}, 'finish_reason': reason, '_usage': dict(self.usage),
                '_provider_content': {'protocol': self.protocol, 'content': self.native}}

    def feed(self, data):
        if data == '[DONE]':
            if not self.completed:
                raise RuntimeError('OpenCode Go stream ended without a finish reason.')
            return []
        packet = json.loads(data)
        if packet.get('error') or packet.get('type') == 'error':
            # Provider errors may echo credentials or request content.
            raise RuntimeError('OpenCode Go reported a stream error; the request was not retried.')
        if self.protocol == 'chat':
            choices = packet.get('choices', [])
            if any(c.get('finish_reason') for c in choices):
                self.completed = True
            if packet.get('usage'):
                choices = [*choices, {'delta': {}, '_usage': packet['usage']}]
            return choices
        if self.protocol == 'messages':
            return self.anthropic(packet)
        if self.protocol == 'responses':
            return self.responses(packet)
        usage = packet.get('usageMetadata', {})
        if usage:
            self.usage = {'prompt_tokens': usage.get('promptTokenCount', 0),
                          'completion_tokens': usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0)}
        result = [{'delta': {}, '_usage': dict(self.usage)}] if usage else []
        for candidate in packet.get('candidates', []):
            for part in candidate.get('content', {}).get('parts', []):
                self.native.append(part)
                if part.get('text'):
                    result.append(delta(**{'reasoning_content' if part.get('thought') else 'content': part['text']}))
                if part.get('functionCall'):
                    call = part['functionCall']
                    result.append(delta(tool_calls=[{'index': self.calls, 'id': call.get('id') or f'go_call_{self.calls}',
                        'function': {'name': call['name'], 'arguments': json.dumps(call.get('args', {}))}}]))
                    self.calls += 1
            if candidate.get('finishReason'):
                reason = candidate['finishReason']
                normalized = {'STOP': 'tool_calls' if self.calls else 'stop', 'MAX_TOKENS': 'length',
                    'SAFETY': 'content_filter', 'BLOCKLIST': 'content_filter',
                    'PROHIBITED_CONTENT': 'content_filter', 'SPII': 'content_filter',
                    'RECITATION': 'content_filter', 'IMAGE_SAFETY': 'content_filter',
                    'MALFORMED_FUNCTION_CALL': 'invalid_tool_calls',
                    'UNEXPECTED_TOOL_CALL': 'invalid_tool_calls'}.get(reason, 'unsupported')
                result.append(self.end(normalized))
        if packet.get('promptFeedback', {}).get('blockReason'):
            raise RuntimeError('OpenCode Go could not process this request.')
        return result

    def anthropic(self, packet):
        kind = packet.get('type')
        index = packet.get('index', 0)
        if self.finish is not None and kind in ('content_block_start', 'content_block_delta'):
            raise RuntimeError('OpenCode Go sent output after its stop reason.')
        if kind == 'message_start':
            usage = packet.get('message', {}).get('usage', {})
            self.usage['prompt_tokens'] = sum(usage.get(k, 0) for k in
                ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
            return [{'delta': {}, '_usage': dict(self.usage)}]
        if kind == 'content_block_start':
            if index in self.blocks:
                raise RuntimeError('OpenCode Go started a content block twice.')
            block = dict(packet['content_block'])
            self.blocks[index] = block
            if block['type'] == 'tool_use':
                self.arguments[index] = ''
                return [delta(tool_calls=[{'index': index, 'id': block['id'],
                               'function': {'name': block['name'], 'arguments': ''}}])]
            if block.get('text'):
                return [delta(content=block['text'])]
        elif kind == 'content_block_delta':
            change = packet['delta']
            block = self.blocks[index]
            dtype = change['type']
            if dtype == 'text_delta':
                block['text'] = block.get('text', '') + change['text']
                return [delta(content=change['text'])]
            if dtype == 'thinking_delta':
                block['thinking'] = block.get('thinking', '') + change['thinking']
                return [delta(reasoning_content=change['thinking'])]
            if dtype == 'signature_delta':
                block['signature'] = block.get('signature', '') + change['signature']
            if dtype == 'input_json_delta':
                self.arguments[index] += change['partial_json']
                return [delta(tool_calls=[{'index': index, 'function': {'arguments': change['partial_json']}}])]
        elif kind == 'content_block_stop':
            if index not in self.blocks:
                raise RuntimeError('OpenCode Go stopped an unknown content block.')
            block = self.blocks.pop(index)
            if index in self.arguments:
                block['input'] = json.loads(self.arguments.pop(index) or '{}')
            self.native.append(block)
        elif kind == 'message_delta':
            if 'output_tokens' in packet.get('usage', {}):
                self.usage['completion_tokens'] = packet['usage']['output_tokens']
            reason = packet.get('delta', {}).get('stop_reason')
            if reason is not None:
                normalized = {'end_turn': 'stop', 'stop_sequence': 'stop', 'tool_use': 'tool_calls',
                    'max_tokens': 'length', 'refusal': 'refusal', 'pause_turn': 'pause_turn',
                    'model_context_window_exceeded': 'context_length'}.get(reason, 'unsupported')
                if self.finish is not None and self.finish != normalized:
                    raise RuntimeError('OpenCode Go returned conflicting stop reasons.')
                self.finish = normalized
        elif kind == 'message_stop':
            return [self.end()]
        return []

    def responses(self, packet):
        kind = packet.get('type', '')
        if kind == 'response.output_text.delta':
            return [delta(content=packet['delta'])]
        if kind == 'response.reasoning_summary_text.delta':
            return [delta(reasoning_content=packet['delta'])]
        if kind == 'response.output_item.added' and packet['item']['type'] == 'function_call':
            item = packet['item']
            self.calls += 1
            return [delta(tool_calls=[{'index': packet['output_index'], 'id': item['call_id'],
                           'function': {'name': item['name'], 'arguments': ''}}])]
        if kind == 'response.function_call_arguments.delta':
            return [delta(tool_calls=[{'index': packet['output_index'],
                           'function': {'arguments': packet['delta']}}])]
        if kind == 'response.output_item.done':
            self.native.append(packet['item'])
        if kind in ('response.completed', 'response.incomplete'):
            usage = packet.get('response', {}).get('usage', {})
            if usage:
                self.usage = {'prompt_tokens': usage.get('input_tokens', 0),
                              'completion_tokens': usage.get('output_tokens', 0)}
            response = packet.get('response', {})
            reason = (response.get('incomplete_details') or {}).get('reason')
            if (kind == 'response.incomplete' or response.get('incomplete_details')
                    or response.get('status', 'completed') != 'completed' or response.get('error')):
                return [self.end({'max_output_tokens': 'length', 'content_filter': 'content_filter'}
                                 .get(reason, 'incomplete'))]
            return [self.end('tool_calls' if self.calls else 'stop')]
        if kind == 'response.failed':
            raise RuntimeError('OpenCode Go could not complete this response.')
        return []
