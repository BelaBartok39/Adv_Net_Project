#!/usr/bin/env python3
"""Compare saved trial stages offline; never access Docker, Batfish, or models."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advnet.performance import compare_performance, compare_trial_artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, nargs='?', help='Existing trial directory; includes all attempts')
    for stage in ('healthy', 'impaired', 'post'):
        parser.add_argument('--'+stage, type=Path, help='Explicit saved measurement JSON')
    parser.add_argument('--output', type=Path, help='New JSON report file; defaults to stdout')
    args = parser.parse_args()
    paths = {stage: getattr(args, stage) for stage in ('healthy', 'impaired', 'post')}
    if args.directory and any(paths.values()):
        parser.error('Use a trial directory or explicit stage files, not both')
    if not args.directory and not any(paths.values()):
        parser.error('Specify a trial directory or at least one stage file')
    if args.directory:
        report = compare_trial_artifacts(args.directory)
        inputs = [args.directory / name for name in ('healthy-measurements.json', 'impaired-measurements.json', 'result.json')]
        inputs.extend(path for path in args.directory.glob('attempt-*/*.json')
                      if path.name in ('measurements.json', 'acceptance.json'))
    else:
        values = {stage: json.loads(path.read_text()) if path else None for stage, path in paths.items()}
        report = compare_performance(**values)
        inputs = [path for path in paths.values() if path]
    if args.output and args.output.resolve() in {path.resolve() for path in inputs}:
        parser.error('Output must not be an input measurement or trial result path')
    report['source_files'] = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted(inputs) if path.is_file()}
    output = json.dumps(report, indent=2, allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Do not overwrite raw measurement files or an earlier comparison.
        with args.output.open('x') as stream:
            stream.write(output)
        print('Performance comparison: '+str(args.output))
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
