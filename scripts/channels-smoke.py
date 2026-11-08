"""Offline acceptance rehearsal. Defaults to synthetic and never sends."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic', action='store_true', default=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    calls = []
    def forbidden(*unused, **kwargs):
        calls.append('network')
        raise AssertionError('synthetic smoke forbids network')
    with tempfile.TemporaryDirectory(prefix='orchd-channels-smoke-') as directory:
        root = Path(directory)
        env = {k:v for k,v in os.environ.items() if not k.startswith('ORCHD_')}
        for key in ('HOME','USERPROFILE','ORCHD_HOME','ORCHD_ROOT','APPDATA','LOCALAPPDATA'):
            folder = root / key
            folder.mkdir()
            env[key] = str(folder)
        env['UV_OFFLINE'] = '1'
        output = io.StringIO()
        # These public-boundary tests exercise real local install, upgrade,
        # fences, receipt replay, and abandoned-claim recovery with fake identity.
        with patch.dict(os.environ, env, clear=True), patch('socket.socket', forbidden), patch('socket.create_connection', forbidden):
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                'tests.test_channels_cutover', 'tests.test_channels_e2e', 'tests.test_channels_health'])
            result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
        passed = result.wasSuccessful() and not calls and result.testsRun >= 8
        report = dict(status='passed' if passed else 'failed', synthetic=True,
                      network_calls=len(calls), platform_sends=0,
                      duplicate_work=0 if passed else None, unsafe_retries=0 if passed else None,
                      tests_run=result.testsRun)
        if not passed:
            print(output.getvalue(), file=sys.stderr)
        print(json.dumps(report) if args.json else str(report))
        return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
