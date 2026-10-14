#!/usr/bin/env python3
"""Model only the engine calls exercised by contained repository-check fixtures.

No Docker socket is accessed. Unexpected commands fail with status 97, following
Magnet's fake-engine rejection contract at commit 2d4ee50b0359e71900fce9289ea01f46d0f41249.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import sys
import stat


arguments = sys.argv[1:]
configuration = None
global_options = set()
while arguments and arguments[0] in {'--host', '--config'}:
    option = arguments[0]
    if len(arguments) < 2 or option in global_options:
        print('unexpected fake docker invocation: missing or repeated global option', file=sys.stderr)
        raise SystemExit(97)
    global_options.add(option)
    if option == '--config':
        path = arguments[1]
        if not os.path.isabs(path):
            print('unexpected fake docker invocation: client config must be absolute', file=sys.stderr)
            raise SystemExit(97)
        try:
            metadata = os.lstat(path)
        except OSError:
            print('unexpected fake docker invocation: client config cannot be inspected', file=sys.stderr)
            raise SystemExit(97)
        configuration = {'path': path, 'uid': metadata.st_uid,
                         'mode': stat.S_IMODE(metadata.st_mode),
                         'kind': 'directory' if stat.S_ISDIR(metadata.st_mode) else 'other',
                         'entries': sorted(os.listdir(path)) if stat.S_ISDIR(metadata.st_mode) else []}
    elif arguments[1] != 'unix:///var/run/docker.sock':
        print('unexpected fake docker invocation: unsupported engine endpoint', file=sys.stderr)
        raise SystemExit(97)
    arguments = arguments[2:]
log_path = os.environ['NMRNET_FAKE_ENGINE_LOG']
with open(log_path, 'a') as output:
    output.write(json.dumps({'arguments': arguments, 'client_config': configuration, 'environment': {
        name: value for name, value in os.environ.items()
        if name.startswith(('DOCKER_', 'BUILDX_')) or name.lower().endswith('_proxy')
    }}) + '\n')
command = arguments[0] if arguments else ''
if command in {'build', 'create', 'start', 'rm'}:
    from pathlib import Path

    log = Path(log_path)
if command == 'build':
    import hashlib
    import shlex

    context = Path(arguments[-1])
    named_contexts = [arguments[index + 1] for index, value in enumerate(arguments) if value == '--build-context']
    if len(named_contexts) != 1 or not named_contexts[0].startswith('wheelhouse='):
        print('unexpected fake docker invocation: one wheelhouse context is required', file=sys.stderr)
        raise SystemExit(97)
    wheelhouse = Path(named_contexts[0].removeprefix('wheelhouse='))
    dockerfile = Path(arguments[arguments.index('--file') + 1])
    if not context.is_dir() or not dockerfile.is_file() or not dockerfile.is_relative_to(context):
        print('unexpected fake docker invocation: build requires an existing owned context and Dockerfile', file=sys.stderr)
        raise SystemExit(97)
    mutation = os.environ.get('NMRNET_FAKE_MUTATE_INPUT')
    if mutation:
        Path(mutation).write_bytes(b'concurrent worktree mutation\n')
        Path(mutation).chmod(0o755)
    manifest = {}
    for instruction in dockerfile.read_text().splitlines():
        if not instruction.startswith('COPY ') or '--from=' in instruction:
            continue
        sources = shlex.split(instruction)[1:-1]
        sources = [source for source in sources if not source.startswith('--')]
        for source in sources:
            path = context / source
            if not path.exists() or path.is_symlink():
                print('unexpected fake docker invocation: required COPY input is absent or symlinked', file=sys.stderr)
                raise SystemExit(97)
            files = sorted(path.rglob('*')) if path.is_dir() else [path]
            for file in files:
                if file.is_file():
                    manifest[str(file.relative_to(context))] = {
                        'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                        'mode': stat.S_IMODE(file.stat().st_mode)}
    wheelhouse_manifest = {file.name: {'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                                      'mode': stat.S_IMODE(file.stat().st_mode)} for file in wheelhouse.iterdir()}
    (log.parent / 'build-evidence.json').write_text(json.dumps({'context': str(context), 'wheelhouse': str(wheelhouse),
        'capture_mode': stat.S_IMODE(context.parent.stat().st_mode), 'capture_uid': context.parent.stat().st_uid,
        'manifest': manifest, 'wheelhouse_manifest': wheelhouse_manifest,
        'content_identity': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}))
    print('x' * int(os.environ['NMRNET_FAKE_BUILD_BYTES']) if os.environ.get('NMRNET_FAKE_BUILD_BYTES')
        else os.environ.get('NMRNET_FAKE_BUILD_RESULT', 'sha256:' + 'b' * 64))
    if os.environ.get('NMRNET_FAKE_BUILD_STATUS'):
        raise SystemExit(int(os.environ['NMRNET_FAKE_BUILD_STATUS']))
elif arguments[:2] == ['image', 'inspect']:
    # Model exact projections requested by tools/baseline.sh metadata admission
    # and tools/inference_image.sh identity admission. A substring mentioning
    # the label cannot authorize a different format or extra argv.
    if len(arguments) == 3 and arguments[2] and not arguments[2].startswith('-'):
        projection = '{{.Id}}'
    elif (len(arguments) == 5 and arguments[2] == '--format'
          and arguments[4] and not arguments[4].startswith('-')):
        projection = arguments[3]
    else:
        sys.stderr.write('unexpected fake docker invocation: unmodeled image inspect argv\n')
        raise SystemExit(97)
    label_projection = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
    if projection not in {'{{.Id}}', label_projection, '{{.Id}}|' + label_projection}:
        sys.stderr.write('unexpected fake docker invocation: unmodeled image inspect format\n')
        raise SystemExit(97)
    if os.environ.get('NMRNET_FAKE_INSPECT_PRIVATE_ERROR'):
        print(os.environ['NMRNET_FAKE_INSPECT_PRIVATE_ERROR'], file=sys.stderr)
    if os.environ.get('NMRNET_FAKE_INSPECT_STATUS'):
        raise SystemExit(int(os.environ['NMRNET_FAKE_INSPECT_STATUS']))
    identity = os.environ.get('NMRNET_FAKE_IMAGE_ID', 'sha256:' + 'b' * 64)
    revision = os.environ.get('NMRNET_FAKE_IMAGE_REVISION', 'a' * 40)
    if projection == '{{.Id}}':
        response = identity
    elif projection == label_projection:
        response = revision
    else:
        response = identity + '|' + revision
    print(response)
elif command == 'create':
    import re

    proxy = any(argument.startswith('type=bind,src=') and argument.endswith(',dst=/ready') for argument in arguments)
    identifier = os.environ.get('NMRNET_FAKE_CID', ('c' if proxy else 'd') * 64)
    receipt = Path(arguments[arguments.index('--cidfile') + 1])
    role = 'proxy' if proxy else 'worker'
    if os.environ.get('NMRNET_FAKE_NUL_RECEIPT_ROLE') == role:
        # Environment strings cannot carry NUL. The fixture emits the exact
        # formerly admitted 65-byte receipt at the file-writing boundary.
        receipt.write_bytes(identifier.encode('ascii') + b'\0')
    else:
        receipt.write_text(identifier + '\n')
    (log.parent / (role + '-cid-path')).write_text(str(receipt))
    state = log.parent / 'containers.json'
    identities = json.loads(state.read_text()) if state.exists() else {}
    if re.fullmatch('[0-9a-f]{64}', identifier):
        identities[identifier] = 'proxy' if proxy else 'worker'
    state.write_text(json.dumps(identities))
    for argument in arguments:
        if argument.startswith('type=bind,src=') and argument.endswith(',dst=/ready'):
            (log.parent / 'ready-path').write_text(argument.split('src=', 1)[1].split(',dst=', 1)[0])
    print(identifier)
elif command == 'start':
    identities = json.loads((log.parent / 'containers.json').read_text())
    expected_role = 'worker' if '--attach' in arguments else 'proxy'
    if identities.get(arguments[-1]) != expected_role:
        print('unexpected fake docker invocation: unknown container identity', file=sys.stderr)
        raise SystemExit(97)
    if '--attach' not in arguments:
        path = Path((log.parent / 'ready-path').read_text())
        (path / 'port').write_text('12345\n')
    else:
        if os.environ.get('NMRNET_FAKE_HOLD_ATTACH') == '1':
            import signal

            def retire_attach(signum, frame):
                (log.parent / 'attach-retired').write_text(str(os.getpid()))
                raise SystemExit(128 + signum)

            signal.signal(signal.SIGTERM, retire_attach)
            (log.parent / 'attach-ready').write_text(str(os.getpid()))
            while True:
                signal.pause()
        raise SystemExit(int(os.environ.get('NMRNET_FAKE_WORKER_STATUS', '0')))
elif command == 'inspect':
    print('true')
elif command == 'rm':
    state = log.parent / 'containers.json'
    identities = json.loads(state.read_text()) if state.exists() else {}
    if arguments[-1] not in identities:
        print('unexpected fake docker invocation: unknown container identity', file=sys.stderr)
        raise SystemExit(97)
    if os.environ.get('NMRNET_FAKE_REMOVE_STATUS', '0') == '0':
        identities.pop(arguments[-1])
        state.write_text(json.dumps(identities))
    raise SystemExit(int(os.environ.get('NMRNET_FAKE_REMOVE_STATUS', '0')))
elif command == 'logs':
    print('private native fixture output', file=sys.stderr)
elif command == 'run':
    if os.environ.get('NMRNET_FAKE_VERIFY_WHEELHOUSE') == '1' and 'tools.wheelhouse' in arguments:
        import subprocess

        mounts = {}
        for index, argument in enumerate(arguments):
            if argument != '--mount':
                continue
            fields = dict(field.split('=', 1) for field in arguments[index + 1].split(',') if '=' in field)
            mounts[fields['dst']] = fields['src']
        frozen_context = os.path.dirname(os.path.dirname(mounts['/workspace/tools/wheelhouse.py']))
        selected = arguments[arguments.index('python3'):]
        selected = [mounts.get(value, value) for value in selected]
        selected[0] = sys.executable
        raise SystemExit(subprocess.run(selected, cwd=frozen_context,
                         env=os.environ | {'PYTHONPATH': frozen_context}, check=False).returncode)
    if os.environ.get('NMRNET_FAKE_HOLD_MODE'):
        import subprocess
        import time
        from pathlib import Path

        mode = os.environ['NMRNET_FAKE_HOLD_MODE']
        if mode == 'eof':
            os.close(1)
            os.close(2)
            # Publish only after entering the fault. A deadline during Python
            # startup must not be mistaken for proof of EOF handling.
            (Path(log_path).parent / 'fault-entered').write_text('eof\n')
            time.sleep(60)
        if mode in {'failed-leader', 'held-pipe'}:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                stdout=subprocess.DEVNULL if mode == 'failed-leader' else None,
                stderr=subprocess.DEVNULL if mode == 'failed-leader' else None)
            with open(os.path.join(os.path.dirname(log_path), 'descendant.pid'), 'w') as receipt:
                receipt.write(str(child.pid))
            # Both the child and its exact PID receipt precede this marker;
            # status 7 alone cannot establish that retirement was exercised.
            (Path(log_path).parent / 'fault-entered').write_text(mode + '\n')
            raise SystemExit(7)
        time.sleep(60)
    if os.environ.get('NMRNET_FAKE_OUTPUT'):
        print(os.environ['NMRNET_FAKE_OUTPUT'])
    if os.environ.get('NMRNET_FAKE_OUTPUT_BYTES'):
        sys.stdout.write('x' * int(os.environ['NMRNET_FAKE_OUTPUT_BYTES']))
    if os.environ.get('NMRNET_FAKE_EMPTY_DISCOVERY') == '1':
        import re
        import shlex
        import subprocess
        import tempfile
        from pathlib import Path

        # This process is already contained. Model the selected container's
        # private /tmp so its wrapper cannot collide with the outer checker's
        # HOME; discovery alone receives the empty proof directory. No other
        # Python or shell command is admitted by this execution adapter.
        empty = os.environ['NMRNET_FAKE_EMPTY_DIRECTORY']
        evidence = Path(log_path).parent / 'discovery-evidence.jsonl'
        # Identify the command after the actual image selector, never an argv
        # token inside another command. Only this launcher's two-token run flags
        # and standalone switches are modeled; a new grant needs explicit review.
        switches = {'--rm', '--read-only', '--init'}
        valued = {'--network', '--pull', '--env', '--user', '--cap-drop', '--security-opt',
                  '--cpus', '--memory', '--memory-swap', '--pids-limit', '--tmpfs', '--workdir', '--mount'}
        index = 1
        while index < len(arguments) and arguments[index].startswith('--'):
            if arguments[index] in switches:
                index += 1
            elif (arguments[index] in valued and index + 1 < len(arguments)
                  and arguments[index + 1] and not arguments[index + 1].startswith('--')):
                index += 2
            else:
                print('unexpected fake docker invocation: unmodeled discovery run flag', file=sys.stderr)
                raise SystemExit(97)
        python_base = 'docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
        if (index >= len(arguments) or (arguments[index] != python_base
                and re.fullmatch(r'sha256:[0-9a-f]{64}', arguments[index]) is None)):
            print('unexpected fake docker invocation: unmodeled discovery image selector', file=sys.stderr)
            raise SystemExit(97)
        original = arguments[index + 1:]
        if original[:1] == ['python3']:
            if original != ['python3', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v']:
                print('unexpected fake docker invocation: unmodeled Python discovery argv', file=sys.stderr)
                raise SystemExit(97)
            mapped = original.copy()
            mapped[6] = empty
            completed = subprocess.run(mapped, check=False)
            record = {'original': original, 'mapped': mapped, 'status': completed.returncode}
        elif original[:1] == ['bash']:
            wrapper = ('mkdir -m 700 /tmp/checker-home; printf "%s\\n" "[safe]" '
                       '"directory = /workspace" "directory = /workspace/NMRNetplusplus" '
                       '> /tmp/checker-home/.gitconfig; exec python3 -B -m unittest discover -s repository_checks -v')
            homes = [arguments[index + 1] for index, value in enumerate(arguments[:-1])
                     if value == '--env' and arguments[index + 1].startswith('HOME=')]
            if original != ['bash', '-eu', '-c', wrapper] or homes != ['HOME=/tmp/checker-home']:
                print('unexpected fake docker invocation: unmodeled checker discovery wrapper', file=sys.stderr)
                raise SystemExit(97)
            with tempfile.TemporaryDirectory(prefix='fake-container-home-', dir=evidence.parent) as directory:
                home = Path(directory) / 'checker-home'
                mapped = original.copy()
                mapped[3] = wrapper.replace('/tmp/checker-home', shlex.quote(str(home)))
                mapped[3] = mapped[3].removesuffix('repository_checks -v') + shlex.quote(empty) + ' -v'
                completed = subprocess.run(mapped, env=os.environ | {'HOME': str(home)}, check=False)
                record = {'original': original, 'mapped': mapped, 'status': completed.returncode,
                          'home': str(home), 'home_mode': stat.S_IMODE(home.stat().st_mode),
                          'home_config': (home / '.gitconfig').read_text()}
        else:
            print('unexpected fake docker invocation: no modeled discovery command', file=sys.stderr)
            raise SystemExit(97)
        with evidence.open('a') as output:
            output.write(json.dumps(record) + '\n')
        raise SystemExit(completed.returncode)
else:
    print('unexpected fake docker invocation: ' + ' '.join(arguments), file=sys.stderr)
    raise SystemExit(97)
raise SystemExit(int(os.environ.get('NMRNET_FAKE_ENGINE_STATUS', '0')))
