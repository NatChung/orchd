"""Execute the real detached launcher from a wheel, outside the source checkout."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class PackagedWorkflowRunnerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('uv'), 'uv is required to build an isolated wheel')
    def test_installed_wheel_launcher_reconciles_once_without_home_scripts(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / 'build-source'
            project.mkdir()
            for name in ('pyproject.toml', 'README.md', 'LICENSE'):
                shutil.copy2(source / name, project / name)
            shutil.copytree(source / 'orchd', project / 'orchd', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
            wheels = root / 'wheels'
            built = subprocess.run([shutil.which('uv'), 'build', '--wheel', '--out-dir', str(wheels), str(project)],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(built.returncode, 0, built.stderr)
            installed = root / 'site-packages'
            with zipfile.ZipFile(next(wheels.glob('orchd-*.whl'))) as wheel:
                wheel.extractall(installed)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('ORCHD_', 'ORCH_INSTANCE_'))}
            profile = root / 'profile'
            env.update(PYTHONPATH=str(installed), ORCHD_ROOT=str(profile), ORCHD_HOME=str(profile / 'state'),
                       ORCHD_CONFIG_DIR=str(profile / 'config'), ORCHD_PROJECTS=str(root / 'projects'),
                       ORCHD_ORCH_HOME=str(profile / 'home'), ORCHD_INTERFACE_HOME=str(profile / 'interface'))
            driver = r'''
import json, subprocess, time
from pathlib import Path
from unittest.mock import patch
import orchd
from orchd import store, workflow_runs
con = store.connect()
plan = dict(workflow=dict(name='example-workflow', commit='a'*40, digest='sha256:'+'b'*64), steps=[])
now = time.time()
# Deliberately unavailable origin: a real one-pass reconciliation must block safely,
# never launch a worker, contact a provider, or write an Orch home.
con.execute('INSERT INTO workflow_runs(id,request_id,orch_thread,idempotency_key,plan,plan_digest,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('package-probe', 1, 'example-orch', 'package-probe', json.dumps(plan).encode(), 'sha256:'+'c'*64, '{}', 'active', now, now))
children = []
original = subprocess.Popen
def one_pass(command, **kwargs):
    child = original([*command, '--once'], **kwargs)
    children.append(child)
    return child
try:
    with patch.object(subprocess, 'Popen', side_effect=one_pass):
        launch = workflow_runs.ensure_runner(con, object())
    child = children[0]
    code = child.wait(timeout=20)
    log = Path(con.execute('PRAGMA database_list').fetchone()['file']).with_suffix('.workflow-runner.log').read_text()
    print(json.dumps(dict(module=orchd.__file__, launch=launch['state'], exit_code=code,
        state=con.execute('SELECT state FROM workflow_runs WHERE id=?', ('package-probe',)).fetchone()[0],
        tasks=con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], log=log)))
finally:
    for child in children:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
    con.close()
'''
            result = subprocess.run([sys.executable, '-P', '-c', driver], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(Path(observed['module']).is_relative_to(installed))
            self.assertEqual(observed['exit_code'], 0, observed['log'])
            self.assertEqual(observed['state'], 'blocked')
            self.assertEqual(observed['tasks'], 0)
            self.assertFalse((profile / 'home').exists())
