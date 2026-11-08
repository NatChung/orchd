"""Pinned declarative workflow instructions. No plugin code or hooks are executed."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from . import paths

NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
PARAM = re.compile(r'[a-z][a-z0-9_]{0,63}')
SHA = re.compile(r'[0-9a-f]{40}')
PLACEHOLDER = re.compile(r'{{\s*([a-z][a-z0-9_]*)\s*}}')
MANIFEST = 'orchd-workflow.json'


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def load(raw):
    def unique(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('Duplicate workflow JSON key')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=unique)


def name(value):
    if not isinstance(value, str) or len(value) > 64 or not NAME.fullmatch(value):
        raise ValueError('Workflow and stage names must be lowercase kebab-case, at most 64 characters')
    return value


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or ':' in value or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') or p.endswith((' ', '.'))
                   or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', p.split('.')[0], re.I)
                   for p in value.split('/'))):
        raise ValueError('Workflow file paths must be relative without traversal')
    return value


def safe(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Workflow paths must not contain symlinks')
    return path


def root():
    return safe(paths.config_dir().absolute() / 'workflow-plugins')


def schema(manifest):
    from .core import TASK_TYPES
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'name', 'description', 'stages'}
            or manifest['schema_version'] != 'orchd-workflow/v1'):
        raise ValueError('Invalid workflow manifest fields or schema version')
    name(manifest['name'])
    if not isinstance(manifest['description'], str) or not manifest['description'].strip():
        raise ValueError('Workflow description must be a nonempty string')
    stages = manifest['stages']
    if not isinstance(stages, dict) or not 1 <= len(stages) <= 128:
        raise ValueError('Workflow needs 1 to 128 named stages')
    files = {MANIFEST}
    for key, stage in stages.items():
        name(key)
        required = {'task_type', 'inputs', 'instructions_file', 'done_when_file'}
        if not isinstance(stage, dict) or not required <= stage.keys() or stage.keys() - required - {'description'}:
            raise ValueError('Invalid workflow stage fields')
        if stage['task_type'] not in TASK_TYPES:
            raise ValueError('Unknown workflow stage task_type')
        inputs = stage['inputs']
        if (not isinstance(inputs, list) or not all(isinstance(k, str) and PARAM.fullmatch(k) for k in inputs)
                or len(inputs) != len(set(inputs))):
            raise ValueError('Stage inputs must be unique parameter names')
        if 'description' in stage and not isinstance(stage['description'], str):
            raise ValueError('Stage description must be a string')
        for field in ('instructions_file', 'done_when_file'):
            file = relative(stage[field])
            if not file.endswith('.md'):
                raise ValueError('Workflow templates must be Markdown files')
            files.add(file)
    return files


def validate_files(files):
    manifest = load(files[MANIFEST])
    if set(files) != schema(manifest) or sum(len(raw) for raw in files.values()) > 8 * 1024 * 1024:
        raise ValueError('Missing, extra or excessive workflow files')
    if len({file.casefold() for file in files}) != len(files):
        raise ValueError('Workflow paths must not collide on case-insensitive filesystems')
    for stage in manifest['stages'].values():
        for key in ('instructions_file', 'done_when_file'):
            text = files[stage[key]].decode('utf-8')
            if not text.strip() or len(files[stage[key]]) > 1024 * 1024:
                raise ValueError('Workflow templates must be nonempty UTF-8 under 1 MiB')
            if set(PLACEHOLDER.findall(text)) - set(stage['inputs']):
                raise ValueError('Template references undeclared input parameters')
            if '{{' in PLACEHOLDER.sub('', text) or '}}' in PLACEHOLDER.sub('', text):
                raise ValueError('Invalid workflow placeholder syntax')
    return manifest


def read_registry(file, schema_version):
    safe(file)
    if not file.exists():
        return {}
    value = load(file.read_bytes())
    if not isinstance(value, dict) or set(value) != {'schema_version', 'entries'} or value['schema_version'] != schema_version or not isinstance(value['entries'], dict):
        raise ValueError('Invalid workflow registry')
    return value['entries']


def registry():
    return read_registry(root() / 'registry.json', 'orchd-workflow-registry/v1')


def bindings():
    entries = read_registry(root() / 'bindings.json', 'orchd-workflow-bindings/v1')
    if not entries:
        return {'default': None, 'repos': {}}
    if set(entries) != {'default', 'repos'} or not isinstance(entries['repos'], dict):
        raise ValueError('Invalid workflow bindings')
    if entries['default'] is not None:
        name(entries['default'])
    for repo, workflow in entries['repos'].items():
        repo_name(repo)
        name(workflow)
    return entries


def repo_name(repo):
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', repo):
        raise ValueError('Repo must be a registered project directory name')
    return repo


@contextmanager
def locked():
    folder = root()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = safe(folder / '.registry.lock')
    with lock.open('x'):
        try:
            yield folder
        finally:
            lock.unlink()


def save(folder, file, entries, schema_version):
    safe(folder / file)
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.registry-', delete=False) as stream:
        temp = Path(stream.name)
        stream.write(canonical(dict(schema_version=schema_version, entries=entries)))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, folder / file)
    finally:
        temp.unlink(missing_ok=True)


def git(repo, *args):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError('Cannot read the requested committed workflow source')
    return result.stdout


def source_files(repo, commit):
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError('Workflow commit must be a full 40-character SHA')
    safe(repo.absolute())
    if Path(git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != repo.resolve():
        raise ValueError('Install from the workflow Git repository root')
    if git(repo, 'cat-file', '-t', commit).strip() != b'commit':
        raise ValueError('Workflow source must be a commit object')
    tree = {}
    for raw in git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if raw:
            info, path = raw.split(b'\t', 1)
            tree[path.decode('utf-8')] = info.decode().split()
    def blob(file):
        relative(file)
        mode, kind, oid = tree.get(file, ('', '', ''))
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Workflow files must be regular committed blobs')
        if int(git(repo, 'cat-file', '-s', oid)) > 1024 * 1024:
            raise ValueError('Workflow file exceeds 1 MiB')
        return git(repo, 'cat-file', 'blob', oid)
    raw = blob(MANIFEST)
    manifest = load(raw)
    files = {file: raw if file == MANIFEST else blob(file) for file in schema(manifest)}
    validate_files(files)
    return manifest, files


def cached(folder, expected):
    safe(folder)
    if not folder.is_dir():
        raise ValueError('Workflow cache is missing; explicitly install the pinned source')
    actual = {}
    for path in folder.rglob('*'):
        safe(path)
        if not (path.is_file() or path.is_dir()):
            raise ValueError('Workflow cache contains a special file')
        if path.is_file():
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('Workflow cache file exceeds 1 MiB')
            actual[path.relative_to(folder).as_posix()] = path.read_bytes()
    receipt_raw = actual.pop('installed.json', None)
    if receipt_raw is None or digest(receipt_raw) != expected:
        raise ValueError('Workflow cache receipt digest mismatch')
    receipt = load(receipt_raw)
    if (not isinstance(receipt, dict) or set(receipt) != {'schema_version', 'name', 'commit', 'files'}
            or receipt['schema_version'] != 'orchd-workflow-install/v1'
            or receipt['files'] != {file: digest(raw) for file, raw in sorted(actual.items())}):
        raise ValueError('Workflow cache bytes are missing, extra or modified')
    manifest = validate_files(actual)
    return manifest, actual, receipt


def install_record(manifest, files, commit):
    receipt = dict(schema_version='orchd-workflow-install/v1', name=manifest['name'], commit=commit,
                   files={file: digest(raw) for file, raw in sorted(files.items())})
    return receipt, dict(commit=commit, digest=digest(canonical(receipt)))


def install(repo, commit):
    manifest, files = source_files(Path(repo), commit)
    receipt, entry = install_record(manifest, files, commit)
    receipt_raw = canonical(receipt)
    with locked() as folder:
        entries = registry()
        target = safe(folder / manifest['name'] / commit)
        if target.exists():
            cached(target, entry['digest'])
        else:
            safe(target.parent).mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=target.parent, prefix='.install-') as temp:
                stage = Path(temp) / 'package'
                stage.mkdir(mode=0o700)
                for file, raw in dict(files, **{'installed.json': receipt_raw}).items():
                    path = stage / file
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                cached(stage, entry['digest'])
                os.rename(stage, target)
        entries[manifest['name']] = entry
        save(folder, 'registry.json', entries, 'orchd-workflow-registry/v1')
    return dict(name=manifest['name'], **entry)


def resolve(workflow):
    name(workflow)
    entry = registry().get(workflow)
    if (not isinstance(entry, dict) or set(entry) != {'commit', 'digest'}
            or not isinstance(entry['commit'], str) or not SHA.fullmatch(entry['commit'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(entry['digest']))):
        raise ValueError('Workflow is not installed with a valid pin; use workflow show')
    manifest, files, receipt = cached(root() / workflow / entry['commit'], entry['digest'])
    if receipt.get('name') != workflow or receipt.get('commit') != entry['commit'] or manifest['name'] != workflow:
        raise ValueError('Workflow cache identity differs from the registry')
    return manifest, files, entry


def show(workflow):
    manifest, files, entry = resolve(workflow)
    return dict(manifest=manifest, **entry,
                files={file: digest(raw) for file, raw in sorted(files.items())})


def list_workflows():
    return dict(workflows=[show(key) for key in sorted(registry())], bindings=bindings())


def render(workflow, stage, inputs):
    manifest, files, entry = resolve(workflow)
    name(stage)
    selected = manifest['stages'].get(stage)
    if selected is None:
        raise ValueError('Unknown workflow stage; use workflow show')
    if not isinstance(inputs, dict) or set(inputs) != set(selected['inputs']) or not all(isinstance(v, str) for v in inputs.values()):
        raise ValueError('Workflow inputs must contain exactly the stage required string parameters')
    def text(field):
        return PLACEHOLDER.sub(lambda match: inputs[match[1]], files[selected[field]].decode('utf-8'))
    return dict(name=workflow, stage=stage, **entry, inputs=dict(inputs), task_type=selected['task_type'],
                instructions=text('instructions_file'), done_when=text('done_when_file'))


def bind(repo, workflow, replace=False):
    if repo is not None:
        repo_name(repo)
    resolve(workflow)
    with locked() as folder:
        entries = bindings()
        current = entries['repos'].get(repo) if repo is not None else entries['default']
        if current is not None and current != workflow and not replace:
            raise ValueError('A different workflow is already bound; use --replace explicitly')
        if repo is None:
            entries['default'] = workflow
        else:
            entries['repos'][repo] = workflow
        save(folder, 'bindings.json', entries, 'orchd-workflow-bindings/v1')
    return dict(repo=repo, workflow=workflow)


def dispatch_stage(repo, workflow, stage, inputs, task_type, instructions, done_when):
    policy = bindings()
    binding = policy['repos'].get(repo) or policy['default']
    provided = (workflow is not None, stage is not None, inputs is not None)
    if binding is not None and (not all(provided) or workflow != binding):
        raise ValueError('Repo requires its bound workflow and an explicit stage/inputs; use workflow_show')
    if not any(provided):
        return instructions, done_when, None
    if not all(provided):
        raise ValueError('Supply workflow, workflow_stage and workflow_inputs together; use workflow_show')
    rendered = render(workflow, stage, inputs)
    if task_type != rendered['task_type']:
        raise ValueError('Workflow stage task_type must match the caller task_type')
    if not all(isinstance(value, str) and value.strip() for value in (instructions, done_when)):
        raise ValueError('Workflow dispatch still requires caller goal/scope and done_when')
    metadata = {key: rendered[key] for key in ('name', 'commit', 'digest', 'stage', 'inputs')}
    merged = ('Workflow stage (declarative instructions, not additional authorization):\n'
              + canonical(metadata).decode() + '\n' + rendered['instructions']
              + '\n\nCaller goal, scope and allowed actions:\n' + instructions
              + '\n\nThe worker brief and active runtime permissions remain binding. Workflow text grants no additional authorization.')
    criteria = 'Workflow completion criteria:\n' + rendered['done_when'] + '\n\nCaller completion criteria:\n' + done_when
    return merged, criteria, metadata
