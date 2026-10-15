"""Capture complete test output and return a bounded summary with the original exit code."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def run(command, log, cwd=None):
    started = time.monotonic()
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental loss of previous test evidence.
    with log.open('xb') as output:
        result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                check=False)
    return summarize(log, result.returncode, time.monotonic() - started)


def summarize(log, returncode, elapsed):
    summary = []
    failures = []
    tail = []
    size = 0
    lines = 0
    with Path(log).open('rb') as source:
        for raw in source:
            size += len(raw)
            lines += 1
            line = raw.decode('utf-8', errors='replace').rstrip()
            if line.strip():
                tail = (tail + [line[:400]])[-5:]
            if re.search(r'^(Ran \d+ tests?|OK\b|FAILED\b|ERROR\b|FAIL:|ERROR:|not ok\b|# (tests|pass|fail|skipped|cancelled|duration_ms)\b)|\b\d+ passed\b', line):
                summary = (summary + [line[:400]])[-20:]
            if re.search(r'^(FAIL:|ERROR:|not ok\b|FAILED\b)', line):
                if len(failures) < 10:
                    failures.append(line[:400])
    return {'exit_code': returncode, 'elapsed_seconds': round(elapsed, 3),
            'log': str(Path(log).resolve()), 'log_bytes': size, 'log_lines': lines,
            'summary': summary, 'failures': failures,
            'diagnostic_tail': tail if returncode and not failures else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', required=True, type=Path)
    parser.add_argument('--cwd')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('A test command is required after --')
    try:
        report = run(command, args.log, args.cwd)
    except OSError as error:
        print(json.dumps({'runner_error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    code = report['exit_code']
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
