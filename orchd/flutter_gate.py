"""Compare Flutter diagnostics and test failures against the task's recorded base."""
import collections
import json
import os
import time
import psutil
from pathlib import Path
import re
import subprocess
import sys
import tempfile


DEADLINE_SECONDS = 3300


def run(argv, cwd, deadline=None):
    timeout = max(0.001, deadline - time.monotonic()) if deadline else 30
    proc = subprocess.Popen(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
        proc.kill()
        proc.communicate()
        raise ValueError('Flutter comparison exceeded its overall time limit') from exc
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def normalize(text, root):
    return re.sub(r':\d+(?::\d+)?', ':#', text.replace(str(root.resolve()), '<root>').replace(str(root), '<root>'))


def analyze(root, deadline=None):
    dependencies = run(['flutter', 'pub', 'get'], root, deadline)
    if dependencies.returncode:
        raise ValueError('flutter pub get failed: ' + (dependencies.stderr or dependencies.stdout)[-2000:])
    result = run(['flutter', 'analyze', '--no-pub', '--no-preamble', '--no-congratulate'], root, deadline)
    if result.returncode not in (0, 1):
        raise ValueError('flutter analyze could not complete: ' + result.stderr[-2000:])
    diagnostics = collections.Counter()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*(error|warning|info)\s+[•-]\s+(.+?)\s+[•-]\s+(.+?):(\d+):(\d+)\s+[•-]\s+([^\s]+)\s*', line)
        if not match:
            raise ValueError('unrecognized flutter analyze output: ' + line[:300])
        severity, message, file, _line, _column, code = match.groups()
        diagnostics[(severity, code, normalize(file, root), normalize(message, root))] += 1
    if result.returncode and not diagnostics:
        raise ValueError('flutter analyze failed without diagnostics: ' + result.stderr[-2000:])
    return diagnostics


def tests(root, paths=(), deadline=None):
    result = run(['flutter', 'test', '--no-pub', '--machine', '--timeout', '60s', *paths], root, deadline)
    tests_by_id, suites, failures, errors, done = {}, {}, collections.Counter(), collections.Counter(), None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ValueError('unrecognized flutter test output: ' + line[:300]) from exc
        if isinstance(event, list) and all(isinstance(item, dict) and item.get('event') == 'test.startedProcess' for item in event):
            continue
        if not isinstance(event, dict):
            raise ValueError('unrecognized flutter test event')
        kind = event.get('type')
        if kind == 'suite':
            suite = event['suite']
            suites[suite['id']] = normalize(suite.get('path', ''), root)
        elif kind == 'testStart':
            test = event['test']
            tests_by_id[test['id']] = (suites.get(test.get('suiteID'), ''), normalize(test.get('name', ''), root))
        elif kind == 'error':
            name = tests_by_id.get(event.get('testID'), ('', ''))
            message = normalize(event.get('error', ''), root)
            errors[(name, message)] += 1
        elif kind == 'testDone' and event.get('result') in ('failure', 'error'):
            failures[tests_by_id.get(event['testID'], ('', ''))] += 1
        elif kind == 'done':
            done = event.get('success')
    if done is None or (result.returncode != 0 and done is True) or (done is False and not (failures or errors)):
        raise ValueError('flutter test did not produce a complete machine-readable result: ' + result.stderr[-2000:])
    return failures, errors


def main(paths=()):
    base = os.environ.get('ORCHD_BASE_SHA')
    if not base:
        raise ValueError('ORCHD_BASE_SHA is required')
    head = Path.cwd().resolve()
    deadline = time.monotonic() + DEADLINE_SECONDS
    with tempfile.TemporaryDirectory(prefix='orchd-flutter-base-') as temp:
        checkout = Path(temp) / 'base'
        added = run(['git', 'worktree', 'add', '--detach', str(checkout), base], head)
        if added.returncode:
            raise ValueError('cannot check out base: ' + added.stderr[-2000:])
        try:
            base_analyze, head_analyze = analyze(checkout, deadline), analyze(head, deadline)
            base_tests, base_errors = tests(checkout, paths, deadline)
            head_tests, head_errors = tests(head, paths, deadline)
            new = [('diagnostic', head_analyze - base_analyze), ('test failure', head_tests - base_tests),
                   ('test error', head_errors - base_errors)]
            for label, items in new:
                for item, count in items.items():
                    print(f'new {label} ({count}): {item}')
            if any(items for _, items in new):
                return 1
            print('No new Flutter diagnostics or test failures compared with the recorded base.')
            return 0
        finally:
            removed = run(['git', 'worktree', 'remove', '--force', str(checkout)], head)
            if removed.returncode:
                raise ValueError('cannot remove base checkout: ' + removed.stderr[-2000:])


def cli():
    try:
        return main(sys.argv[1:])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Flutter comparison failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(cli())
