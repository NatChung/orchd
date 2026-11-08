"""Apply committed instance settings without starting sessions or granting trust."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from . import bootstrap, paths, workflows as wf

RECEIPT = '.orchd-instance-receipt.json'
START = b'<!-- orchd:instance:start -->\n'
END = b'<!-- orchd:instance:end -->\n'


@contextmanager
def profile(env):
    keys = (*paths.PROFILE_PATH_VARS, 'ORCHD_PROJECTS')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(env)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def blob(repo, commit, file):
    wf.relative(file)
    entry = wf.git(repo, 'ls-tree', '-z', commit, '--', file).split(b'\0')[0]
    if not entry:
        raise ValueError('Instance source file is missing')
    info, actual = entry.split(b'\t', 1)
    mode, kind, oid = info.decode().split()
    if actual.decode() != file or mode not in ('100644', '100755') or kind != 'blob':
        raise ValueError('Instance sources must be regular committed files')
    if int(wf.git(repo, 'cat-file', '-s', oid)) > 1024 * 1024:
        raise ValueError('Instance source exceeds 1 MiB')
    return wf.git(repo, 'cat-file', 'blob', oid)


def source(repo, commit):
    workflow, files = wf.source_files(repo, commit)
    raw = blob(repo, commit, 'orchd-instance.json')
    manifest = wf.load(raw)
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'name', 'workflow', 'agents_file', 'projects_file'}
            or manifest['schema_version'] != 'orchd-instance/v1'):
        raise ValueError('Invalid instance manifest')
    wf.name(manifest['name'])
    if manifest['workflow'] != workflow['name']:
        raise ValueError('Instance workflow must match the workflow in the same committed repo')
    documents = {}
    files = dict(files, **{'orchd-instance.json': raw})
    for key in ('agents_file', 'projects_file'):
        file = wf.relative(manifest[key])
        data = blob(repo, commit, file)
        if not file.endswith('.md') or not data.decode('utf-8').strip():
            raise ValueError('Instance documents must be nonempty UTF-8 Markdown')
        if b'orchd:managed:' in data or b'orchd:instance:' in data:
            raise ValueError('Instance documents may not inject managed markers')
        documents[key] = data
        files[file] = data
    _, installed = wf.install_record(workflow, {key: value for key, value in files.items()
                                               if key in wf.schema(workflow)}, commit)
    return manifest, documents, {key: wf.digest(value) for key, value in files.items()}, installed


def regular(path):
    wf.safe(path)
    if path.exists() and not path.is_file():
        raise ValueError('Instance target must be a regular file')
    return path.read_bytes() if path.exists() else None


def read_receipt(root):
    raw = regular(Path(root).absolute() / RECEIPT)
    if raw is None:
        raise ValueError('Instance has no successful apply receipt')
    receipt = wf.load(raw)
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 'orchd-instance-receipt/v1':
        raise ValueError('Invalid instance receipt')
    return receipt, wf.digest(raw)


def block(data):
    if data.count(START) != 1 or data.count(END) != 1:
        raise ValueError('Instance AGENTS marker is missing or ambiguous')
    start, end = data.index(START), data.index(END) + len(END)
    if end <= start or b'orchd:managed:start' in data[start:end] or b'orchd:managed:end' in data[start:end]:
        raise ValueError('Instance section overlaps the managed block')
    managed_start, managed_end = data.find(b'<!-- orchd:managed:start'), data.find(b'<!-- orchd:managed:end')
    if managed_start >= 0 and managed_start < start < managed_end:
        raise ValueError('Instance section must be outside the managed block')
    return start, end


def plan(repo, commit, root, projects):
    root, projects = root.absolute(), projects.absolute()
    wf.safe(root)
    wf.safe(projects)
    if '..' in root.parts or '..' in projects.parts or not projects.is_dir():
        raise ValueError('Use physical instance and existing projects directory paths')
    repo = wf.safe(repo.absolute()).resolve()
    metadata = [Path(raw.decode().strip()).resolve() for raw in
                (wf.git(repo, 'rev-parse', '--absolute-git-dir'),
                 wf.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))]
    if any(root.is_relative_to(folder) for folder in metadata):
        raise ValueError('Instance root must not be inside Git metadata')
    if repo.is_relative_to(root):
        raise ValueError('Instance root must not overlap its source repository')
    if root.is_relative_to(repo):
        relative_root = root.relative_to(repo).as_posix()
        if wf.git(repo, 'ls-files', '--', relative_root):
            raise ValueError('Source-local instance root contains tracked files')
        try:
            wf.git(repo, 'check-ignore', '-q', '--', relative_root + '/')
        except ValueError:
            raise ValueError('Source-local instance root must be an entirely ignored subtree') from None
    for folder in ('state', 'config', 'home', 'home/groups', 'interface', 'channel', 'gpt-computer-use-worker'):
        path = wf.safe(root / folder)
        if path.exists() and not path.is_dir():
            raise ValueError('Instance profile location must be a directory')
    manifest, documents, hashes, installed = source(repo, commit)
    env = dict(ORCHD_ROOT=str(root), ORCHD_HOME=str(root / 'state'), ORCHD_CONFIG_DIR=str(root / 'config'),
               ORCHD_PROJECTS=str(projects), ORCHD_EXECUTABLE=str(paths.orchd_executable()))
    regular(root / 'config/init.toml')
    regular(root / 'config/config.toml')
    existing = regular(root / RECEIPT)
    receipt = wf.load(existing) if existing is not None else None
    if receipt is not None and (not isinstance(receipt, dict) or receipt.get('schema_version') != 'orchd-instance-receipt/v1'
                                or receipt.get('name') != manifest['name'] or receipt.get('source_repo') != str(repo)
                                or not isinstance(receipt.get('targets'), dict)):
        raise ValueError('Instance receipt identity or structure mismatch')
    planned = bootstrap.planned_files(Path.home(), env, orchd_bin=env['ORCHD_EXECUTABLE'])
    overlay = START + documents['agents_file'].rstrip(b'\n') + b'\n' + END
    desired, target_hashes = {}, {}
    for path, template in planned.items():
        relative = path.relative_to(root).as_posix()
        old, new = regular(path), template.encode('utf-8')
        if relative == 'home/AGENTS.md':
            if receipt is not None:
                if old is None:
                    raise ValueError('Instance AGENTS is missing')
                start, end = block(old)
                if wf.digest(old[start:end]) != receipt['targets'].get(relative):
                    raise ValueError('Instance AGENTS section drift; preserve local changes')
                new = old[:start] + overlay + old[end:]
            else:
                candidate = new + (b'' if new.endswith(b'\n') else b'\n') + overlay
                if old is not None and old not in (new, candidate):
                    raise ValueError('Cannot safely adopt existing AGENTS without a receipt')
                new = candidate
            target_hashes[relative] = wf.digest(overlay)
        else:
            if receipt is not None:
                if old is None or wf.digest(old) != receipt['targets'].get(relative):
                    raise ValueError('Instance target drift or missing file: ' + relative)
            elif old is not None and old not in (new, documents['projects_file'] if relative == 'home/PROJECTS.md' else new):
                raise ValueError('Cannot safely adopt an existing instance target: ' + relative)
            if relative == 'home/PROJECTS.md':
                new = documents['projects_file']
            target_hashes[relative] = wf.digest(new)
        if old != new:
            desired[path] = new
    if receipt is not None and set(receipt['targets']) != set(target_hashes):
        raise ValueError('Instance template target set changed; use a separately reviewed migration')
    with profile(env):
        registry, binding = wf.registry(), wf.bindings()
        previous = receipt.get('workflow') if receipt else dict(name=manifest['workflow'], **installed)
        active = registry.get(previous['name'])
        if (active is not None and active != {key: previous[key] for key in ('commit', 'digest')}) or (receipt and active is None):
            raise ValueError('Installed workflow differs from the instance receipt')
        if active is not None:
            wf.resolve(previous['name'])
        if binding['default'] not in (None, previous['name']) or (receipt and binding['default'] is None):
            raise ValueError('Instance default workflow binding drift')
        target = wf.root() / manifest['workflow'] / commit
        if target.exists() or target.is_symlink():
            wf.cached(target, installed['digest'])
        wf.safe(wf.root() / '.registry.lock')
        if (wf.root() / '.registry.lock').exists():
            raise ValueError('Workflow registry is locked; do not steal the lock')
    state = dict(schema_version='orchd-instance-receipt/v1', name=manifest['name'], source_commit=commit,
                 source_repo=str(repo), source_hashes=hashes, targets=target_hashes, profile=env,
                 workflow=dict(name=manifest['workflow'], **installed))
    return root, env, desired, state, receipt


def write(path, raw):
    wf.safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.instance-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(repo, commit, root, projects, check=False, expected_receipt=None):
    repo, root, projects = Path(repo), Path(root), Path(projects)
    actual, env, changes, state, previous = plan(repo, commit, root, projects)
    changed = [str(path.relative_to(actual)) for path in changes]
    restart = [role for role, prefix in (('orch', 'home/'), ('interface', 'interface/'))
               if any(path.startswith(prefix) for path in changed)]
    result = dict(status='instance_checked' if check else 'instance_applied', root=str(actual),
                  name=state['name'], source_commit=commit, changed_files=changed, restart_required=restart)
    current = regular(actual / RECEIPT)
    result['current_receipt_digest'] = wf.digest(current) if current is not None else None
    if expected_receipt is not None and result['current_receipt_digest'] != expected_receipt:
        raise ValueError('Instance receipt changed; inspect current settings before applying')
    if check:
        return result
    actual.mkdir(parents=True, exist_ok=True)
    lock = wf.safe(actual / '.instance-apply.lock')
    with lock.open('x'):
        try:
            if expected_receipt is not None and read_receipt(actual)[1] != expected_receipt:
                raise ValueError('Instance receipt changed during preflight')
            _, _, current_changes, current_state, current_previous = plan(repo, commit, root, projects)
            if (current_changes, current_state, current_previous) != (changes, state, previous):
                raise ValueError('Instance changed during preflight; retry after inspection')
            with profile(env):
                wf.install(repo, commit)
                wf.bind(None, state['workflow']['name'], replace=previous is not None)
            for path, raw in changes.items():
                write(path, raw)
            (actual / 'home/groups').mkdir(exist_ok=True)
            old_basis = {key: value for key, value in (previous or {}).items() if key not in ('changed_files', 'restart_required')}
            if state != old_basis:
                write(actual / RECEIPT, wf.canonical(dict(state, changed_files=changed, restart_required=restart)))
            result['receipt_digest'] = read_receipt(actual)[1]
        finally:
            lock.unlink()
    return result
