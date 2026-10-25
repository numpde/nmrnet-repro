"""Exercise maintained Compose commands with a finite recording Engine.

The real host entrypoint owns selection, literal argv and helper retirement.
No Engine, network, API or scientific execution is granted to these fixtures.
"""
from tools.container_guard import require_container
require_container()

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'sha256:' + 'a' * 64
CID = 'b' * 64


class DeploymentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'checkout'
        self.root.mkdir(mode=0o700)
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir(mode=0o700)
        tools = self.root / 'tools'
        tools.mkdir()
        for name in ('compose_deployment.sh', 'provider_deployment.sh', 'delivery_deployment.sh'):
            shutil.copyfile(ROOT / 'tools' / name, tools / name)
        (tools / 'docker_client.sh').write_text('''docker_client_prepare() { engine=(bash "$repository_root/tools/record-engine.sh" "$repository_root"); }
docker_client_cleanup() { printf 'client-cleanup\\n' >> "$repository_root/cleanup"; }
''')
        (tools / 'inference_native.sh').write_text('''inference_native() {
    local operation=$1 seconds=$2 result=$3 status=0; shift 3
    inference_diagnostic=
    printf '%s|%s|%s\\n' "$operation" "$seconds" "$result" >> "$repository_root/budgets"
    "$@" || status=$?
    inference_retirement_confirmed=${TEST_RETIRED-true}
    if [[ $status != 0 ]]; then
        if [[ $operation == 'retire deployment configuration helper' ]]; then
            inference_diagnostic=${TEST_CLEANUP_DIAGNOSTIC-}
        else inference_diagnostic=${TEST_PRIMARY_DIAGNOSTIC-}; fi
    fi
    return "$status"
}
''')
        (tools / 'provider_image.sh').write_text('provider_image_inputs=(tools/provider_deployment.py)\n')
        (tools / 'geometry_image.sh').write_text('geometry_image_inputs=(tools/provider_deployment.py)\n')
        (tools / 'git_metadata.sh').write_text('''repository_git() {
    case " $* " in
      *' rev-parse '*) printf '%040d\\n' 1 ;;
      *' --name-only '*) printf 'tools/provider_deployment.py\\n' ;;
      *) printf '160000 commit %040d\\tNMRNetplusplus\\n' 2 ;;
    esac
}
''')
        (tools / 'provider_deployment.py').write_text('# fixture source\n')
        (tools / 'record-engine.sh').write_text(r'''root=$1; shift
count=0
[[ ! -f $root/count ]] || read -r count < "$root/count"
count=$((count + 1)); printf '%s\n' "$count" > "$root/count"
printf '%s\0' "$@" > "$root/call-$count"
case $1 in
 ps) [[ ${TEST_LIVE-false} != true ]] || printf '%064d\n' 7 ;;
 run)
    if [[ ${TEST_RUN_STATUS-0} != 0 ]]; then printf 'actual helper cause\n' >&2; exit "$TEST_RUN_STATUS"; fi
    arguments=("$@")
    for ((i=0;i<${#arguments[@]};i++)); do
      case ${arguments[i]} in
        --bundle|--output) destination=${arguments[i+1]}; mkdir -p "$destination/bindings"; printf '{}\n' > "$destination/compose.json"; printf '[delivery]\n' > "$destination/daemon.toml" ;;
      esac
    done
    ;;
 inspect) printf 'sha256:%064d\n' 9 ;;
 compose)
    if [[ ${TEST_PAUSE-false} == true ]]; then
      : > "$root/ready"
      until [[ -f $root/release ]]; do sleep 0.01; done
    fi
    if [[ " $* " == *' ps --all --quiet prediction '* ]]; then printf '%064d\n' 8; fi
    if [[ ${TEST_COMPOSE_STATUS-0} != 0 ]]; then printf 'actual Compose cause\n' >&2; exit "$TEST_COMPOSE_STATUS"; fi
    ;;
 rm) [[ ${TEST_RM_STATUS-0} == 0 ]] || exit "$TEST_RM_STATUS" ;;
 *) exit 91 ;;
esac
''')
        for kind in ('provider', 'delivery', 'inference', 'preparation'):
            directory = self.root / '.work' / kind
            directory.mkdir(parents=True)
            (directory / 'image-id').write_text(IMAGE + '\n')
        (self.root / 'containers/inference').mkdir(parents=True)
        (self.root / 'containers/inference/image-inputs.txt').write_text('tools/provider_deployment.py\n')
        definitions = self.root / 'config/deployments/fixture'
        definitions.mkdir(parents=True)
        for name in ('provider.toml', 'deployment.toml'):
            (definitions / name).write_text('fixture\n')
        self.provider = self.root / 'secrets/deployments/fixture'
        self.provider.mkdir(parents=True, mode=0o700)
        (self.provider / 'signing.private.json').write_text('private credential\n')
        (self.provider / 'unselected-directory').mkdir(mode=0o700)
        (self.provider / 'unselected-directory/canary').write_text('never grant this root\n')
        self.bundle = self.selected(self.provider)
        self.delivery = self.home / '.local/share/nmr-api-delivery'
        self.delivery.mkdir(parents=True, mode=0o700)
        self.delivery_bundle = self.selected(self.delivery)

    def selected(self, root):
        bundles = root / 'bundles'
        bundles.mkdir(mode=0o700)
        bundle = bundles / 'generation-original'
        bundle.mkdir(mode=0o700)
        (bundle / 'compose.json').write_text('{}\n')
        (bundle / 'daemon.toml').write_text('[delivery]\n')
        (root / 'current').symlink_to('bundles/generation-original')
        return bundle

    def run_command(self, action, role='provider', *, changes=None, extra=()):
        environment = dict(os.environ, HOME=str(self.home), NMRNET_DEPLOYMENT_INPUT='fixture')
        environment.update(changes or {})
        return subprocess.run(['bash', str(self.root / 'tools' / f'{role}_deployment.sh'), action, *extra],
                              env=environment, capture_output=True, text=True, timeout=5)

    def calls(self):
        count = int((self.root / 'count').read_text()) if (self.root / 'count').exists() else 0
        return [[item.decode() for item in (self.root / f'call-{index}').read_bytes().split(b'\0')[:-1]]
                for index in range(1, count + 1)]

    def test_missing_or_executable_names_refuse_before_any_engine_call(self):
        for name in ('', '../fixture', 'fixture; touch sentinel', '$(touch sentinel)', 'x\nup'):
            with self.subTest(name=name):
                result = self.run_command('up', changes={'NMRNET_DEPLOYMENT_INPUT': name})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.root / 'sentinel').exists())

    def test_direct_commands_keep_literal_recipe_project_and_finite_options(self):
        expected = {
            'up': ['up', '--detach', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120'],
            'down': ['down', '--timeout', '60'], 'status': ['ps', '--all'],
            'logs': ['logs', '--no-color', '--tail', '80'],
        }
        for role in ('provider', 'delivery'):
            for action, suffix in expected.items():
                with self.subTest(role=role, action=action):
                    result = self.run_command(action, role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    call = self.calls()[-1]
                    project = 'nmr-api-delivery' if role == 'delivery' else 'nmrnet-' + hashlib.sha256(
                        (str(self.root) + '\0fixture').encode()).hexdigest()[:20]
                    bundle = self.delivery_bundle if role == 'delivery' else self.bundle
                    directory = self.delivery if role == 'delivery' else self.root
                    self.assertEqual(call, ['compose', '--env-file', '/dev/null', '--project-name', project,
                                            '--project-directory', str(directory), '--file', str(bundle / 'compose.json'), *suffix])
                    self.assertTrue((bundle / 'compose.json').exists())
        self.assertFalse(any('--volumes' in call or '-v' in call for call in self.calls()))
        self.assertTrue((self.root / 'cleanup').exists())

    def test_failed_compose_preserves_original_status_and_selected_configuration_without_retry(self):
        result = self.run_command('up', changes={'TEST_COMPOSE_STATUS': '7'})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn('actual Compose cause', result.stderr)
        self.assertIn('No automatic retry', result.stderr)
        self.assertEqual(os.readlink(self.provider / 'current'), 'bundles/generation-original')

    def test_completed_command_exits_without_reading_new_checkout_tail(self):
        environment = dict(os.environ, HOME=str(self.home), NMRNET_DEPLOYMENT_INPUT='fixture', TEST_PAUSE='true')
        process = subprocess.Popen(['bash', str(self.root / 'tools/provider_deployment.sh'), 'status'],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not (self.root / 'ready').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / 'ready').exists())
            with (self.root / 'tools/compose_deployment.sh').open('a') as changed:
                changed.write('\nprintf replayed > "$repository_root/sentinel"\n')
            (self.root / 'release').touch()
            _stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse((self.root / 'sentinel').exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_install_refuses_live_project_without_shutdown_or_replacement(self):
        result = self.run_command('install', changes={'TEST_LIVE': 'true'})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.calls()[0][0:2], ['ps', '--quiet'])
        self.assertEqual(len(self.calls()), 1)
        self.assertIn('stop this deployment', result.stderr)
        self.assertEqual(os.readlink(self.provider / 'current'), 'bundles/generation-original')

    def test_provider_freeze_has_narrow_readonly_sources_and_fixed_helper_grants(self):
        result = self.run_command('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Next: make delivery/deployment/install DEPLOYMENTS="<complete profile list>"', result.stdout)
        self.assertIn('Include fixture and every other profile using the shared delivery service on this host.', result.stdout)
        self.assertIn('If fixture is the only profile: make delivery/deployment/install DEPLOYMENTS=fixture', result.stdout)
        self.assertNotIn('Next: make delivery/deployment/install DEPLOYMENTS=fixture', result.stdout)
        call = next(call for call in self.calls() if call[0] == 'run')
        for pair in (['--network', 'none'], ['--cap-drop', 'ALL'], ['--pull', 'never'],
                     ['--security-opt', 'no-new-privileges:true'], ['--user', f'{os.getuid()}:{os.getgid()}'],
                     ['--memory', '512m'], ['--memory-swap', '512m'], ['--pids-limit', '64']):
            self.assertIn(pair, [call[index:index+2] for index in range(len(call)-1)])
        self.assertIn('--read-only', call)
        self.assertIn('--init', call)
        mounts = [call[index + 1] for index, value in enumerate(call[:-1]) if value == '--mount']
        self.assertIn(f'type=bind,src={self.provider}/signing.private.json,dst={self.provider}/signing.private.json,readonly', mounts)
        self.assertFalse(any(f'src={self.provider},' in mount or 'unselected-directory' in mount for mount in mounts))
        self.assertIn('freeze', call)
        self.assertIn('--source-root', call)
        self.assertIn('/opt/provider/environment/bin/python', call)
        selected = self.provider / os.readlink(self.provider / 'current')
        self.assertNotEqual(selected, self.bundle)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o500)
        self.assertEqual((selected / 'compose.json').stat().st_mode & 0o777, 0o400)
        self.assertTrue(self.bundle.exists())
        self.assertFalse(any(call[0] in ('inspect', 'create', 'start') for call in self.calls()))

    def test_native_lifetime_locks_are_created_private_and_keep_their_inode(self):
        result = self.run_command('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        locks = [self.provider / 'runtime' / role / '.owner-session.lock'
                 for role in ('prediction', 'reader', 'smiles')]
        before = [path.stat().st_ino for path in locks]
        for path in locks:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        result = self.run_command('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path.stat().st_ino for path in locks], before)

    def test_native_lifetime_lock_symlink_is_refused_before_freeze(self):
        (self.provider / 'runtime').mkdir(mode=0o700)
        directory = self.provider / 'runtime' / 'reader'
        directory.mkdir(mode=0o700)
        target = self.root / 'unrelated'
        target.write_text('keep original bytes')
        (directory / '.owner-session.lock').symlink_to(target)
        result = self.run_command('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('native lifetime locks', result.stderr)
        self.assertEqual(target.read_text(), 'keep original bytes')
        self.assertFalse(any(call[0] == 'run' for call in self.calls()))

    def test_private_qualification_project_is_passed_only_to_provider_freezer(self):
        result = self.run_command('install', extra=('--delivery-project', 'fixture-private'))
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(call for call in self.calls() if call[0] == 'run')
        index = call.index('--delivery-project')
        self.assertEqual(call[index+1], 'fixture-private')
        self.assertIn('Private delivery namespace: fixture-private', result.stdout)
        self.assertNotIn('Next: make delivery/deployment/install', result.stdout)

    def test_failed_helper_is_removed_by_its_original_name_without_reexecution(self):
        result = self.run_command('install', changes={'TEST_RUN_STATUS': '7'})
        self.assertEqual(result.returncode, 7, result.stderr)
        calls = self.calls()
        runs = [call for call in calls if call[0] == 'run']
        self.assertEqual(len(runs), 1)
        name = runs[0][runs[0].index('--name')+1]
        self.assertEqual(calls[-1], ['rm', '--force', name])
        self.assertIn('actual helper cause', result.stderr)
        self.assertEqual(os.readlink(self.provider / 'current'), 'bundles/generation-original')

    def test_unretired_local_helper_never_dispatches_a_new_cleanup_client(self):
        result = self.run_command('install', changes={'TEST_RUN_STATUS': '7', 'TEST_RETIRED': 'false'})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['ps', 'run'])
        self.assertFalse((self.root / 'cleanup').exists())
        self.assertIn('Configuration helper, if still present:', result.stderr)

    def test_helper_removal_failure_preserves_the_original_command_failure(self):
        result = self.run_command('install', changes={'TEST_RUN_STATUS': '7', 'TEST_RM_STATUS': '9'})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.calls()[-1][0], 'rm')

    def test_helper_cleanup_preserves_primary_and_separate_cleanup_diagnostics(self):
        for cleanup_status in ('0', '9'):
            with self.subTest(cleanup_status=cleanup_status):
                before = len(self.calls())
                result = self.run_command('install', changes={
                    'TEST_RUN_STATUS': '7', 'TEST_RM_STATUS': cleanup_status,
                    'TEST_PRIMARY_DIAGNOSTIC': 'original native client diagnostic',
                    'TEST_CLEANUP_DIAGNOSTIC': 'separate retirement diagnostic',
                })
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(result.stderr.count('original native client diagnostic'), 1)
                self.assertEqual(result.stderr.count('separate retirement diagnostic'), int(cleanup_status != '0'))
                self.assertIn('actual helper cause', result.stderr)
                calls = self.calls()[before:]
                runs = [call for call in calls if call[0] == 'run']
                self.assertEqual(len(runs), 1)
                name = runs[0][runs[0].index('--name') + 1]
                self.assertEqual(calls[-1], ['rm', '--force', name])
                self.assertEqual(os.readlink(self.provider / 'current'), 'bundles/generation-original')

    def test_client_inputs_uses_selected_prediction_image_and_readonly_bundle(self):
        output = Path(self.temporary.name) / 'client'
        result = self.run_command('client-inputs', changes={'NMRNET_OUTPUT_INPUT': str(output)})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0][-4:], ['ps', '--all', '--quiet', 'prediction'])
        self.assertEqual(calls[1][0:3], ['inspect', '--type', 'container'])
        call = calls[2]
        self.assertIn(f'type=bind,src={self.bundle},dst={self.bundle},readonly', call)
        self.assertEqual(call[call.index('client-inputs')+1:], ['--bundle', str(self.bundle),
            '--prediction-image-id', 'sha256:' + f'{9:064d}', '--client-root', str(output)])

    def test_delivery_prepare_uses_explicit_unique_provider_bundles(self):
        result = self.run_command('install', 'delivery', changes={'NMRNET_DELIVERY_DEPLOYMENTS_INPUT': 'fixture'})
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(call for call in self.calls() if call[0] == 'run')
        self.assertIn('nmrnet_provider.delivery_deployment', call)
        self.assertIn(f'type=bind,src={self.bundle},dst={self.bundle},readonly', call)
        self.assertEqual(call[call.index('--provider-bundle')+1], str(self.bundle))
        self.assertFalse(any('nmr-api-delivery-queue' in value for value in call))
        self.assertTrue((self.delivery_bundle / 'compose.json').exists())

    def test_duplicate_delivery_binding_profiles_refuse_before_helper(self):
        result = self.run_command('install', 'delivery', changes={'NMRNET_DELIVERY_DEPLOYMENTS_INPUT': 'fixture fixture'})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['ps'])

    def test_held_and_discard_are_offline_with_exact_literal_selectors(self):
        changes = {'NMRNET_API_ORIGIN_INPUT': 'https://api.example', 'NMRNET_PROVIDER_REF_INPUT': 'provider:fixture',
                   'NMRNET_EXECUTION_ATTEMPT_REF_INPUT': 'execution_attempt:sha256:' + 'c'*64,
                   'NMRNET_BODY_SHA256_INPUT': 'd'*64}
        for action in ('held', 'discard'):
            result = self.run_command(action, 'delivery', changes=changes)
            self.assertEqual(result.returncode, 0, result.stderr)
            call = self.calls()[-1]
            self.assertEqual(call[0], 'run')
            self.assertIn('provider_core.delivery_discard', call)
            self.assertIn('type=volume,src=nmr-api-delivery-queue,dst=/state,volume-nocopy', call)
            self.assertNotIn('signing.private.json', ' '.join(call))
            self.assertEqual(call[call.index('--network')+1], 'none')
            if action == 'held': self.assertEqual(call[-1], '--list-held')
            else: self.assertEqual(call[-4:], ['--execution-attempt-ref', changes['NMRNET_EXECUTION_ATTEMPT_REF_INPUT'], '--body-sha256', 'd'*64])

    def test_native_event_projection_and_engine_failure_keep_their_existing_owners(self):
        operation = 'a'*32
        for status in (0, 7):
            script = f"printf '%s\\n' PRIVATE_APPLICATION_OUTPUT 'INFO event=child_started operation={operation} pid=14' 'INFO event=child_retired operation={operation} returncode=-15 reason=owner_lost' >&2; exit {status}"
            result = subprocess.run(['bash', str(ROOT/'tools/compose_runner_events.sh'), 'b'*64, 'bash', '-c', script], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertEqual(result.stdout, f'started\t{operation}\t14\nretired\t{operation}\t-15\towner_lost\n')
            self.assertNotIn('PRIVATE', result.stdout)
            self.assertIn('PRIVATE_APPLICATION_OUTPUT', result.stderr)
