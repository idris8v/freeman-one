#!/usr/bin/env python3
"""Compare computed CSS snapshots from offline fixture pages."""
import argparse
from collections import Counter
import json
from pathlib import Path


def compare(before, after):
    counts = Counter()
    differences = []
    compared = 0
    missing = []
    for old in sorted(before.glob('*.json')):
        new = after / old.name
        if not new.exists():
            missing.append(old.name)
            continue
        left, right = json.loads(old.read_text()), json.loads(new.read_text())
        compared += 1
        if len(left['items']) != len(right['items']):
            counts['DOM-count'] += 1
            differences.append({'file': old.name, 'count': [len(left['items']), len(right['items'])]})
            continue
        for a, b in zip(left['items'], right['items']):
            if (a['tag'], a['class'], a['pseudo']) != (b['tag'], b['class'], b['pseudo']):
                counts['DOM-identity'] += 1
                continue
            changed = {key: [value, b['style'][key]] for key, value in a['style'].items() if value != b['style'][key]}
            if changed:
                counts.update(changed.keys())
                differences.append({'file': old.name, 'tag': a['tag'], 'class': a['class'], 'pseudo': a['pseudo'], 'diff': changed})
    return {'compared': compared, 'missing': missing, 'counts': dict(counts), 'differences': differences}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.before, args.after)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'differences'}, ensure_ascii=False))
