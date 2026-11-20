"""Start the accepted Windows review only. Publishing/AI/metrics stay disabled.

Place in %LOCALAPPDATA%/iTeamTransfer/project beside windows_local_access.py.
No restore, migrations, seeding, token refresh, image builds or external API calls.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from review_validation import validate_compose_mounts

ROOT = Path(__file__).resolve().parent


def local_desktop_required():
    if os.name != 'nt' or os.environ.get('DOCKER_HOST'):
        raise RuntimeError('LOCAL_WINDOWS_DOCKER_DESKTOP_REQUIRED')
    result = subprocess.run(['docker', 'context', 'inspect'], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('DOCKER_DESKTOP_NOT_READY')
    rows = json.loads(result.stdout)
    endpoint = rows[0]['Endpoints']['docker']['Host']
    if len(rows) != 1 or not endpoint.startswith('npipe://'):
        raise RuntimeError('REMOTE_DOCKER_CONTEXT_REFUSED')


def start_review(runtime):
    if not (ROOT / 'windows/database-import-PASS.json').is_file():
        raise RuntimeError('VERIFIED_DATABASE_RESTORE_REQUIRED_NO_AUTOMATIC_IMPORT')
    # Resolve and validate the effective config before any startup command.
    runtime.validate_review_config()
    validate_compose_mounts(json.loads(runtime.compose('config', '--format', 'json').stdout))
    runtime.compose('up', '--pull', 'never', '--no-build', '-d', '--wait', '--wait-timeout', '90',
                    'postgres', 'redis', 'backend', 'frontend')


def main():
    local_desktop_required()
    if not (ROOT / 'windows_local_access.py').is_file():
        raise RuntimeError('PLACE_STARTER_IN_THE_RESTORED_PROJECT_FOLDER')
    sys.path.insert(0, str(ROOT / 'tools'))
    import windows_runtime
    start_review(windows_runtime)
    import windows_local_access
    # This also verifies actual running images/BUILD_SHA, flags, mounts and network.
    windows_local_access.main()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Local access stopped. Data preserved. Use windows_runtime.py stop to stop the review services.')
    except Exception as error:
        reason = str(error) if type(error) is RuntimeError else 'START_FAILED_DETAILS_HIDDEN'
        print(reason)
        print('No database import or publishing requested. Keep existing data; investigate before retrying.')
        raise SystemExit(1)
