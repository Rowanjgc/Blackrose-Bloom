"""Per-request context occupancy and decode speed, independent of UI transport."""
from __future__ import annotations

import math
import time

from .prompts import context_budget, count_prompt


class GenerationMetrics:
    def __init__(self, model, messages, tools, callback, *, clock=time.monotonic, counts=None):
        self.callback = callback
        self.clock = clock
        self.limit, self.output_reserve = context_budget(model)
        counts = counts or (count_prompt(messages, tools) if callback else None)
        self.admission_input_bound = counts.estimate() if counts else 0
        self.prompt = counts.estimate(4) if counts else 0
        self.output_bytes = 0
        self.output = 0
        self.prompt_exact = False
        self.output_exact = False
        self.first_output = None
        self.last_output = None
        self.speed = None
        self.speed_exact = False
        self.last_publish = float('-inf')
        self.publish(force=True)

    def update(self, choice):
        if not self.callback:
            return
        was_exact = self.prompt_exact and self.output_exact
        had_speed = self.speed is not None
        now = self.clock()
        delta = choice.get('delta') or {}
        text = (delta.get('content') or '') + (delta.get('reasoning_content') or '')
        text += ''.join(c.get('function', {}).get('name', '') + c.get('function', {}).get('arguments', '')
                        for c in delta.get('tool_calls') or [])
        if text:
            self.output_bytes += len(text.encode('utf-8'))
            if self.first_output is None:
                self.first_output = now
            self.last_output = now
        usage = choice.get('_usage') or {}
        for key, attr in [('prompt_tokens', 'prompt'), ('completion_tokens', 'output')]:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                setattr(self, attr, value)
                setattr(self, attr + '_exact', True)
        if not self.output_exact:
            self.output = math.ceil(self.output_bytes / 4)
        if not self.speed_exact and self.first_output is not None and self.last_output is not None:
            elapsed = self.last_output - self.first_output
            if elapsed >= 0.25:
                self.speed = self.output / elapsed
        timings = choice.get('_timings') or {}
        if type(timings.get('predicted_n')) is int and timings['predicted_n'] >= 0:
            self.output, self.output_exact = timings['predicted_n'], True
        if all(type(timings.get(k)) is int and timings[k] >= 0 for k in ('prompt_n', 'cache_n')):
            self.prompt = timings['prompt_n'] + timings['cache_n']
            self.prompt_exact = True
        rate = timings.get('predicted_per_second')
        if isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0:
            self.speed, self.speed_exact = rate, True
        self.publish(force=(not was_exact and self.prompt_exact and self.output_exact)
                     or (not had_speed and self.speed is not None))

    def publish(self, *, force=False):
        if not self.callback:
            return
        now = self.clock()
        if not force and now - self.last_publish < 0.2:
            return
        self.last_publish = now
        self.callback({'context_used': self.prompt + self.output, 'context_window': self.limit,
                       'admission_input_bound': self.admission_input_bound,
                       'output_reserve': self.output_reserve,
                       'context_estimated': not (self.prompt_exact and self.output_exact),
                       'tokens_per_second': self.speed, 'speed_estimated': not self.speed_exact})
