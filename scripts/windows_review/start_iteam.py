"""Start the accepted Windows review only. Publishing/AI/metrics stay disabled.

Place in the dedicated review transfer beside windows_local_access.py.
No restore, migrations, seeding, token refresh, image builds or external API calls.
"""
import json
from pathlib import Path
import sys

from review_validation import require_existing_review_volumes, require_review_host, validate_compose_mounts

ROOT = Path(__file__).resolve().parent


def start_review(runtime=None):
    require_review_host(ROOT)
    if not (ROOT / 'windows/database-import-PASS.json').is_file():
        raise RuntimeError('VERIFIED_DATABASE_RESTORE_REQUIRED_NO_AUTOMATIC_IMPORT')
    if runtime is None:
        sys.path.insert(0, str(ROOT / 'tools'))
        import windows_runtime as runtime
    # Resolve and validate the effective config before any startup command.
    runtime.validate_review_config()
    validate_compose_mounts(json.loads(runtime.compose('config', '--format', 'json').stdout))
    require_existing_review_volumes()
    runtime.compose('up', '--pull', 'never', '--no-build', '-d', '--wait', '--wait-timeout', '90',
                    'postgres', 'redis', 'backend', 'frontend')


def main():
    if not (ROOT / 'windows_local_access.py').is_file():
        raise RuntimeError('PLACE_STARTER_IN_THE_RESTORED_PROJECT_FOLDER')
    start_review()
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
