"""Summarize measured provider work without equating schemas with usefulness."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean, median


def summarize(rows):
    result = {}
    for mode in ('markdown', 'ui'):
        chosen = [row for row in rows if row['mode'] == mode]
        if not chosen:
            continue
        inputs, outputs, caches = [], [], []
        for row in chosen:
            usages = [request.get('usage') or {} for request in row.get('requests', [])]
            inputs.append(sum(u.get('prompt_tokens', 0) for u in usages))
            outputs.append(sum(u.get('completion_tokens', 0) for u in usages))
            caches.append(sum((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) for u in usages))
        paints = [r['first_accepted_block_seconds'] for r in chosen if r.get('first_accepted_block_seconds') is not None]
        result[mode] = {'turns': len(chosen), 'errors': sum('error' in r for r in chosen),
            'schema_valid': sum(r.get('schema_valid', False) for r in chosen) if mode == 'ui' else None,
            'requested_profile_present': sum(any(b.get('document', {}).get('type') == r['profile']
                for b in r.get('blocks', []) if b.get('document')) for r in chosen) if mode == 'ui' else None,
            'median_seconds': median(r['total_seconds'] for r in chosen),
            'mean_seconds': mean(r['total_seconds'] for r in chosen),
            'median_first_accepted_block_seconds': median(paints) if paints else None,
            'mean_total_input_tokens': mean(inputs), 'mean_total_completion_tokens': mean(outputs),
            'mean_cached_input_tokens': mean(caches),
            'actual_usage_rounds': sum(bool(q.get('usage')) for r in chosen for q in r.get('requests', []))}
    result['ui_by_profile'] = {profile: {'turns': len(group), 'schema_valid':sum(r.get('schema_valid',False) for r in group)}
        for profile in Counter(r['profile'] for r in rows) if (group := [r for r in rows if r['profile']==profile and r['mode']=='ui'])}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    args=parser.parse_args()
    print(json.dumps(summarize(json.loads(args.results.read_text())),indent=2))


if __name__=='__main__':
    main()
