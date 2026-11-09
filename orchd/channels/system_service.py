"""Per-user operating system service registration for channel receive."""
import os
import getpass
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from orchd import file_lock, store

NAME = 'orchd-channels'
LABEL = 'com.orchd.channels'


def _running(home):
    home.mkdir(parents=True, exist_ok=True)
    with (home / 'serve.lock').open('a+b') as handle:
        try:
            file_lock.try_lock(handle)
            return False
        except BlockingIOError:
            return True


def command(action, home, *, con=None, platform=None, runner=None, executable=None, user_home=None, uid=None, db_home=None):
    home = Path(home).absolute()
    db_home = Path(db_home or store.home()).absolute()
    serve_args = ['channels', 'serve', '--service-log', '--service-channel-home', str(home), '--service-store-home', str(db_home)]
    platform = platform or sys.platform
    runner = runner or subprocess.run
    executable = executable or shutil.which('orchd')
    user_home = Path(user_home or Path.home())
    uid = uid if uid is not None else (os.getuid() if hasattr(os, 'getuid') else None)
    def invoke(argv, *, allow_missing=False, ignored_errors=()):
        result = runner(argv, capture_output=True, text=True)
        error = (result.stderr or '').lower()
        if result.returncode and not allow_missing and not any(message in error for message in ignored_errors):
            raise OSError('service command failed: ' + (result.stderr or '').strip())
        return result
    last_received = None
    if con is not None:
        last_received = con.execute("SELECT MAX(received_at) FROM channel_messages WHERE direction='in'").fetchone()[0]
    if platform == 'win32':
        base = ['schtasks', '/TN', NAME]
        if action == 'install':
            if not executable: raise OSError('orchd executable not found')
            home.mkdir(parents=True, exist_ok=True)
            ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
            ET.register_namespace('', ns)
            task = ET.Element(f'{{{ns}}}Task', version='1.2')
            triggers = ET.SubElement(task, 'Triggers'); ET.SubElement(triggers, 'LogonTrigger')
            principals = ET.SubElement(task, 'Principals'); principal = ET.SubElement(principals, 'Principal', id='Author')
            ET.SubElement(principal, 'UserId').text = getpass.getuser()
            ET.SubElement(principal, 'LogonType').text = 'InteractiveToken'
            settings = ET.SubElement(task, 'Settings'); ET.SubElement(settings, 'MultipleInstancesPolicy').text = 'IgnoreNew'
            actions = ET.SubElement(task, 'Actions', Context='Author'); execute = ET.SubElement(actions, 'Exec')
            ET.SubElement(execute, 'Command').text = executable
            ET.SubElement(execute, 'Arguments').text = subprocess.list2cmdline(serve_args)
            xml = home / 'service-task.xml'; ET.ElementTree(task).write(xml, encoding='utf-16', xml_declaration=True)
            invoke(['schtasks', '/Create', '/TN', NAME, '/XML', str(xml), '/F'])
            invoke(['schtasks', '/Run', '/TN', NAME])
        elif action == 'status':
            result = invoke(['schtasks', '/Query', '/TN', NAME], allow_missing=True)
            if result.returncode and 'cannot find' not in (result.stderr or '').lower():
                raise OSError('service query failed: ' + (result.stderr or '').strip())
            return dict(registered=result.returncode == 0, running=_running(home), last_received=last_received, log=str(home / 'serve.log'))
        elif action == 'stop': invoke(['schtasks', '/End', '/TN', NAME], ignored_errors=('cannot find', 'not running'))
        elif action == 'uninstall': invoke(['schtasks', '/Delete', '/TN', NAME, '/F'], ignored_errors=('cannot find',))
        else: raise ValueError('unknown service action')
    elif platform == 'darwin':
        plist = user_home / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
        domain = 'gui/' + str(uid)
        if action == 'install':
            if not executable: raise OSError('orchd executable not found')
            home.mkdir(parents=True, exist_ok=True); plist.parent.mkdir(parents=True, exist_ok=True)
            invoke(['launchctl', 'bootout', domain, str(plist)], ignored_errors=('could not find', 'no such process'))
            with plist.open('wb') as stream:
                plistlib.dump(dict(Label=LABEL, ProgramArguments=[executable] + serve_args, RunAtLoad=True, StandardOutPath=str(home / 'serve.log'), StandardErrorPath=str(home / 'serve.log')), stream)
            invoke(['launchctl', 'bootstrap', domain, str(plist)])
        elif action == 'status':
            result = invoke(['launchctl', 'print', domain + '/' + LABEL], allow_missing=True)
            if result.returncode and not any(x in (result.stderr or '').lower() for x in ('could not find', 'no such process')):
                raise OSError('service query failed: ' + (result.stderr or '').strip())
            return dict(registered=result.returncode == 0, running=_running(home), last_received=last_received, log=str(home / 'serve.log'))
        elif action == 'stop': invoke(['launchctl', 'kill', 'SIGTERM', domain + '/' + LABEL], ignored_errors=('could not find', 'no such process'))
        elif action == 'uninstall':
            invoke(['launchctl', 'bootout', domain, str(plist)], ignored_errors=('could not find', 'no such process')); plist.unlink(missing_ok=True)
        else: raise ValueError('unknown service action')
    else: raise OSError('service supported only on Windows and macOS')
    return dict(status='ok', action=action)
