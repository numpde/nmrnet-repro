"""Provide isolated current-launcher copies and a finite offline engine double."""

from tools.container_guard import require_container

require_container()

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import signal
import time


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent


class LauncherFixture:
    def __init__(self, root):
        self.root = root
        self.log = root / 'engine.jsonl'
        self.scratch = set()
        (root / 'tools').mkdir(parents=True)
        for name in ('container.sh', 'docker.sh', 'inference_image.sh', 'packages.sh', 'baseline.sh'):
            shutil.copyfile(ROOT / 'tools' / name, root / 'tools' / name)
        shutil.copyfile(ROOT / 'Makefile', root / 'Makefile')
        shutil.copytree(ROOT / 'containers', root / 'containers')
        for name in ('container_guard.py', 'artifacts.py', 'wheelhouse.py', 'build_unicore.py', 'package_lock.py'):
            shutil.copyfile(ROOT / 'tools' / name, root / 'tools' / name)
        upstream = root / 'NMRNetplusplus/uninmr'
        upstream.mkdir(parents=True)
        (upstream / 'infer.py').write_bytes(b'pinned_source = 1\n')
        (root / 'NMRNetplusplus/LICENSE').write_bytes(b'upstream license\n')
        (root / 'bin').mkdir()
        for name, source in (('docker', 'docker.py'), ('git', 'git.sh')):
            (root / 'bin' / name).symlink_to(FIXTURES / source)
        shutil.copyfile(ROOT / 'tools/wheelhouse.sh', root / 'tools/wheelhouse.sh')
        (root / '.work/wheelhouse/cpu').mkdir(parents=True)
        (root / '.work/wheelhouse/cpu/.complete').write_text('{}\n')
        (root / '.work/inference').mkdir()
        (root / '.work/inference/image-id').write_text('sha256:' + 'b' * 64 + '\n')
        (root / 'requirements').mkdir()
        for name in ('inference.in', 'build.in', 'unicore-source.json'):
            (root / 'requirements' / name).write_text('fixture\n')
        (root / 'requirements/inference.lock').write_text('fixture==1\n')
        self.environment = os.environ | {'PATH': str(root / 'bin') + ':' + str(Path(sys.executable).parent) + ':' + os.defpath,
            'PYTHONPATH': str(ROOT), 'NMRNET_FAKE_ENGINE_LOG': str(self.log)}

    def baseline_inputs(self):
        for name in ('baseline_prepare', 'baseline_model', 'baseline_output', 'baseline_result', 'baseline_run'):
            shutil.copyfile(ROOT / 'tools' / (name + '.py'), self.root / 'tools' / (name + '.py'))
        (self.root / 'artifacts').mkdir()
        for name in ('release.json', 'checkpoints.selection.json', 'data.selection.json', 'weight.selection.json'):
            shutil.copyfile(ROOT / 'artifacts' / name, self.root / 'artifacts' / name)
        for name in ('.work/baseline/prepared', '.work/prepared/checkpoints', '.work/prepared/data', '.work/prepared/weight'):
            directory = self.root / name
            directory.mkdir(parents=True)
            (directory / 'provenance.json').write_text('{}\n')
        return {'NMRNET_LABEL_ATOL_INPUT': '0', 'NMRNET_ATOL_INPUT': '0',
            'NMRNET_RTOL_INPUT': '0', 'NMRNET_RUN_ID_INPUT': 'fixture-run'}

    def build_evidence(self):
        return json.loads((self.root / 'build-evidence.json').read_text())

    def run(self, launcher, *arguments, additions=None, timeout=10, corrupt_worker_receipt=None):
        try:
            command = ['bash', str(self.root / 'tools' / launcher), *arguments]
            process = subprocess.Popen(command,
                env=self.environment | (additions or {}), cwd=self.root,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                if corrupt_worker_receipt is not None:
                    # Mutate only after the exact fixture attach client is
                    # running. This is a probe deadline, never operator policy.
                    ready = self.root / 'attach-ready'
                    deadline = time.monotonic() + timeout
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not ready.exists():
                        raise subprocess.TimeoutExpired(command, timeout)
                    receipt = Path((self.root / 'worker-cid-path').read_text())
                    receipt.write_bytes(corrupt_worker_receipt)
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A timed-out probe must not leave an engine double or a child
                # holding its output pipe. This group was created by this fixture.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=2)
                raise
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            for call in self.calls():
                values = call['arguments']
                if values[:1] == ['create'] and '--cidfile' in values:
                    path = Path(values[values.index('--cidfile') + 1]).parent
                    if path.parent == Path('/tmp') and path.name.startswith('nmrnet-proxy.'):
                        self.scratch.add(path)

    def make(self, *arguments, additions=None):
        environment = self.environment | (additions or {})
        if shutil.which('make', path=environment['PATH']) is None:
            raise AssertionError('Cannot prove literal GNU Make input handoff: the maintained contained image has no make executable.')
        for name in ('MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES'):
            environment.pop(name, None)
        return subprocess.run(['make', '--no-print-directory', '-C', str(self.root), *arguments],
            env=environment, text=True, capture_output=True, timeout=10, check=False)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def dispose(self):
        # Only acquisition scratch recorded by this fixture's create calls is
        # owned here; never glob /tmp or delete an unrelated retained operation.
        for scratch in self.scratch:
            shutil.rmtree(scratch, ignore_errors=True)
