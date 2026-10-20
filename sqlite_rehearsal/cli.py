"""Command line interface and stable exit statuses."""
import argparse
import json
import sys
from .core import RehearsalError, rehearse


def main(argv=None):
    parser = argparse.ArgumentParser(description='Rehearse SQLite migrations on a disposable backup.')
    parser.add_argument('database', help='existing SQLite database (opened read-only)')
    parser.add_argument('migrations', nargs='+', help='UTF-8 SQL files in execution order')
    parser.add_argument('--json', action='store_true', help='emit a machine-readable report')
    parser.add_argument('--timeout', type=float, default=30, help='cooperative SQLite deadline in seconds (default: 30)')
    parser.add_argument('--fail-on-row-loss', action='store_true', help='fail when a table row count decreases')
    args = parser.parse_args(argv)
    try:
        report = rehearse(args.database, args.migrations, timeout=args.timeout, fail_on_row_loss=args.fail_on_row_loss)
    except RehearsalError as exc:
        if args.json:
            print(json.dumps({'format_version': 1, 'ok': False, 'error': str(exc)}))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print('PASS' if report['ok'] else 'FAIL')
        for step in report['steps']:
            print(f"  {'OK' if step['ok'] else 'FAIL'} {step['migration']} ({step['duration_ms']} ms)")
            if 'error' in step:
                print(f"    {step['error']}")
            for kind in ('added', 'removed', 'changed'):
                for item in step.get('changes', {}).get(kind, []):
                    obj = item.get('after', item)
                    print(f"    {kind}: {obj['type']} {obj['name']}")
            for row in step.get('changes', {}).get('rows', []):
                print(f"    rows {row['table']}: {row['before']} -> {row['after']} ({row['delta']:+d})")
        for issue in report['issues']:
            print(f'  {issue}')
    return 0 if report['ok'] else 1
