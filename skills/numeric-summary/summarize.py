"""Summarize supplied finite numbers without external dependencies."""
import json
import math
import statistics
import sys

values = json.load(sys.stdin)['values']
if (not isinstance(values, list) or not 1 <= len(values) <= 1000
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
    raise ValueError('Supply between 1 and 1000 finite numbers')
summary = {'count': len(values), 'minimum': min(values), 'maximum': max(values),
           'mean': statistics.mean(values)}
report = '\n'.join(f'{key.capitalize()}: {value}' for key, value in summary.items()) + '\n'
summary['artifact'] = {'filename': 'numeric-summary.txt', 'content': report}
print(json.dumps(summary, allow_nan=False))
