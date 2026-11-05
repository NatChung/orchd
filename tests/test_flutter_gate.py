import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class FlutterGateTest(unittest.TestCase):
    def test_existing_failures_pass_and_new_failures_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'app'
            repo.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            (repo / 'state.json').write_text(json.dumps({'diagnostics': ['old'], 'tests': ['old']}))
            git('add', '.')
            git('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            binary = root / 'bin'
            binary.mkdir()
            flutter = binary / 'flutter'
            flutter.write_text('''#!''' + sys.executable + '''
import json, pathlib, sys
state = json.loads(pathlib.Path('state.json').read_text())
if sys.argv[1] == 'pub':
    sys.exit(0)
if sys.argv[1] == 'analyze':
    for i, item in enumerate(state['diagnostics']):
        print(f'warning • {item} • {pathlib.Path.cwd()}/lib/app.dart:{i + 1}:2 • code')
    sys.exit(bool(state['diagnostics']))
for i, item in enumerate(state['tests']):
    print(json.dumps({'type': 'testStart', 'test': {'id': i, 'name': item}}))
    print(json.dumps({'type': 'testDone', 'testID': i, 'result': 'failure'}))
print(json.dumps({'type': 'done', 'success': not bool(state['tests'])}))
sys.exit(bool(state['tests']))
''')
            flutter.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'], ORCHD_BASE_SHA=base,
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            def check(state):
                (repo / 'state.json').write_text(json.dumps(state))
                return subprocess.run([sys.executable, '-m', 'orchd.flutter_gate'], cwd=repo, env=env,
                                      capture_output=True, text=True)
            good = check({'diagnostics': ['old'], 'tests': ['old']})
            self.assertEqual(good.returncode, 0, good.stderr + good.stdout)
            for state, message in [({'diagnostics': ['old', 'new'], 'tests': ['old']}, 'new diagnostic'),
                                   ({'diagnostics': ['old'], 'tests': ['old', 'new']}, 'new test failure')]:
                with self.subTest(state=state):
                    result = check(state)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stdout)
