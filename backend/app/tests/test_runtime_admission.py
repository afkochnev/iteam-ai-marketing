"""Fresh-process checks for immutable admission and refusal before side effects.

Use subprocesses because reloading or monkeypatching admission in the pytest
process would not test the shipped cold-start boundary. The dependency-light
checks can also run with unittest when the complete backend is unavailable.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
HAS_DATABASE = importlib.util.find_spec("sqlalchemy") is not None
HAS_CELERY = importlib.util.find_spec("celery") is not None
HAS_OPENAI = importlib.util.find_spec("openai") is not None
HAS_AGENTS = importlib.util.find_spec("agents") is not None
HAS_METRICS = importlib.util.find_spec("telethon") is not None


def _environment(mode: str | None = "passive", app_env: str = "development") -> dict[str, str]:
    env = {
        **os.environ,
        "APP_ENV": app_env,
        "PYTHONPATH": str(BACKEND),
        "DATABASE_URL": "postgresql+asyncpg://candidate:candidate@127.0.0.1:1/admission_fixture",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "WORKER_ROLE": "",
        "SCHEDULER_ROLE": "",
        "OPENAI_API_KEY": "",
        "TELEGRAM_PUBLISHING_ENABLED": "false",
        "VK_PUBLISHING_ENABLED": "false",
        "TELEGRAM_METRICS_ENABLED": "false",
        "VK_METRICS_ENABLED": "false",
        "BUILD_SHA": "admission-candidate",
    }
    if mode is None:
        env.pop("RUNTIME_ADMISSION_MODE", None)
    else:
        env["RUNTIME_ADMISSION_MODE"] = mode
    return env


class RuntimeAdmissionTests(unittest.TestCase):
    def run_code(
        self,
        code: str,
        *,
        mode: str | None = "passive",
        app_env: str = "development",
    ) -> None:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=BACKEND,
            env=_environment(mode, app_env),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_admission_preserves_active_runtime(self) -> None:
        self.run_code(
            """
            from app.core.runtime_admission import admission_status, require_active_runtime
            require_active_runtime()
            assert admission_status()['admission_mode'] == 'active'
            assert admission_status()['business_ready'] is False
            assert admission_status(business_ready=True)['business_ready'] is True
            assert admission_status()['admission_protocol'] == 1
            """,
            mode=None,
        )

    def test_passive_cannot_reopen_via_environment_settings_or_settings_reload(self) -> None:
        self.run_code(
            """
            import importlib
            import os
            from app.core import config
            from app.core.runtime_admission import (
                RuntimeAdmissionError, admission_status, require_active_runtime,
            )
            os.environ['RUNTIME_ADMISSION_MODE'] = 'active'
            config.settings.runtime_admission_mode = 'active'
            config.settings.app_env = 'development'
            importlib.reload(config)
            assert config.settings.runtime_admission_mode == 'active'
            try:
                require_active_runtime()
            except RuntimeAdmissionError as error:
                assert error.code == 'RUNTIME_PASSIVE'
            else:
                raise AssertionError('passive process reopened')
            assert admission_status()['admission_mode'] == 'passive'
            assert admission_status(business_ready=True)['business_ready'] is False
            """
        )

    def test_maintenance_forces_passive_even_with_explicit_active(self) -> None:
        self.run_code(
            """
            from app.core.config import settings
            from app.core.runtime_admission import (
                RuntimeAdmissionError, admission_status, require_active_runtime,
            )
            assert admission_status()['admission_mode'] == 'passive'
            settings.app_env = 'development'
            try:
                require_active_runtime()
            except RuntimeAdmissionError as error:
                assert error.code == 'RUNTIME_PASSIVE'
            else:
                raise AssertionError('maintenance reopened')
            """,
            mode="active",
            app_env="maintenance",
        )

    def test_invalid_modes_fail_closed_without_echoing_input(self) -> None:
        for mode in ("", "ACTIVE", " passive ", "secret-sentinel-invalid-mode"):
            with self.subTest(mode=mode):
                self.run_code(
                    """
                    try:
                        import app.core.runtime_admission
                    except RuntimeError as error:
                        assert str(error) == 'RUNTIME_ADMISSION_MODE_INVALID'
                        assert error.code == 'RUNTIME_ADMISSION_MODE_INVALID'
                    else:
                        raise AssertionError('invalid mode admitted')
                    """,
                    mode=mode,
                )

    def test_passive_http_has_exact_allowlist_and_no_business_imports(self) -> None:
        self.run_code(
            """
            import importlib.abc
            import sys
            from fastapi.testclient import TestClient
            class NoBusinessImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    blocked = ('app.api', 'app.services', 'app.workers', 'app.models')
                    if any(fullname == prefix or fullname.startswith(prefix + '.')
                           for prefix in blocked):
                        raise AssertionError('business bootstrap imported: ' + fullname)
            sys.meta_path.insert(0, NoBusinessImports())
            from app.core.passive_runtime import create_passive_app
            with TestClient(create_passive_app()) as client:
                health = client.get('/health')
                assert health.status_code == 200
                assert health.json()['admission_protocol'] == 1
                assert health.json()['admission_mode'] == 'passive'
                assert health.json()['business_ready'] is False
                assert health.json()['build_sha'] == 'admission-candidate'
                for method, path in (
                    ('GET', '/api/v1/marketing-conversations/stale/messages'),
                    ('GET', '/api/v1/system/ready'),
                    ('POST', '/api/v1/campaigns'),
                    ('PATCH', '/api/v1/tasks/stale'),
                    ('POST', '/health'), ('HEAD', '/health'), ('OPTIONS', '/health'),
                    ('GET', '/health/'), ('GET', '/docs'), ('GET', '/openapi.json'),
                ):
                    response = client.request(method, path)
                    assert response.status_code == 503, (method, path)
                    if method != 'HEAD':
                        assert response.json()['error']['code'] == 'RUNTIME_PASSIVE'
            assert 'app.core.database' not in sys.modules
            """
        )

    def test_passive_readiness_is_honest_legacy_superset(self) -> None:
        self.run_code(
            """
            from unittest.mock import AsyncMock
            from fastapi.testclient import TestClient
            from app.core import runtime_readiness
            from app.core.passive_runtime import create_passive_app
            client = TestClient(create_passive_app())
            for database, schema, redis in (
                (True, True, True), (False, False, True),
                (True, False, True), (True, True, False),
            ):
                sql = AsyncMock(return_value=(database, schema))
                ping = AsyncMock(return_value=redis)
                runtime_readiness._check_readonly_database = sql
                runtime_readiness._check_redis_ping = ping
                response = client.get('/health/ready')
                payload = response.json()
                ready = database and schema and redis
                assert response.status_code == (200 if ready else 503)
                assert payload['status'] == ('ok' if ready else 'degraded')
                assert payload['database'] == ('ok' if database else 'unavailable')
                assert payload['schema'] == ('ok' if schema else 'incompatible')
                assert payload['redis'] == ('ok' if redis else 'unavailable')
                assert payload['database_read_only'] is database
                assert payload['business_ready'] is False
                assert payload['admission_protocol'] == 1
                sql.assert_awaited_once()
                ping.assert_awaited_once()
            """
        )

    @unittest.skipUnless(HAS_DATABASE, "full backend dependencies required")
    def test_main_passive_boot_does_not_import_business_routes_or_workers(self) -> None:
        self.run_code(
            """
            import importlib.abc
            import sys
            from fastapi.testclient import TestClient
            class NoBusinessImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.startswith(('app.api.', 'app.workers.', 'app.services.')):
                        raise AssertionError('business bootstrap imported: ' + fullname)
            sys.meta_path.insert(0, NoBusinessImports())
            from app.main import app
            with TestClient(app) as client:
                assert client.get('/health').json()['admission_mode'] == 'passive'
                assert client.get('/api/v1/campaigns').status_code == 503
            assert 'app.core.database' not in sys.modules
            """
        )

    @unittest.skipUnless(HAS_DATABASE, "full backend dependencies required")
    def test_production_validators_still_run_in_passive_mode(self) -> None:
        self.run_code(
            """
            from app.core.config import settings
            settings.app_secret = 'short'
            try:
                import app.main
            except RuntimeError as error:
                assert 'APP_SECRET' in str(error)
            else:
                raise AssertionError('passive bypassed production validation')
            """,
            app_env="production",
        )

    def test_celery_import_refuses_before_optional_dependencies_or_configuration(self) -> None:
        self.run_code(
            """
            import importlib.abc
            import sys
            from app.core.runtime_admission import RuntimeAdmissionError
            class NoCeleryBootstrap(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    blocked = ('celery', 'kombu', 'app.workers.runtime_heartbeat')
                    if any(fullname == name or fullname.startswith(name + '.')
                           for name in blocked):
                        raise AssertionError('Celery booted before admission')
            sys.meta_path.insert(0, NoCeleryBootstrap())
            try:
                import app.workers.celery_app
            except RuntimeAdmissionError as error:
                assert error.code == 'RUNTIME_PASSIVE'
            else:
                raise AssertionError('Celery admitted')
            """
        )

    @unittest.skipUnless(HAS_CELERY, "full backend dependencies required")
    def test_standard_worker_and_beat_cli_refuse_without_roles_or_with_queue_override(self) -> None:
        for command in (
            ["worker", "--pool=solo", "-Q", "ai,publication"],
            ["beat", "--max-interval=1"],
        ):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-m", "celery", "-A", "app.workers.celery_app", *command],
                    cwd=BACKEND,
                    env=_environment(),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RUNTIME_PASSIVE", result.stdout + result.stderr)

    @unittest.skipUnless(HAS_DATABASE, "full backend dependencies required")
    def test_business_sessions_and_worker_engines_refuse_before_connection(self) -> None:
        self.run_code(
            """
            import asyncio
            from unittest.mock import patch
            from sqlalchemy.ext.asyncio import create_async_engine
            from app.core.runtime_admission import RuntimeAdmissionError
            with patch('sqlalchemy.ext.asyncio.create_async_engine',
                       wraps=create_async_engine) as factory:
                from app.core import database
                assert factory.call_count == 1
                options = factory.call_args.kwargs['connect_args']
                assert options['server_settings']['default_transaction_read_only'] == 'on'
                for call in (
                    database.async_session_factory,
                    database.create_worker_session_factory,
                    lambda: database.create_async_engine('invalid-url'),
                    lambda: asyncio.run(database.get_db_session().__anext__()),
                ):
                    try:
                        call()
                    except RuntimeAdmissionError as error:
                        assert error.code == 'RUNTIME_PASSIVE'
                    else:
                        raise AssertionError('business DB access admitted')
                assert factory.call_count == 1
            """
        )

    @unittest.skipUnless(HAS_DATABASE, "full backend dependencies required")
    def test_readonly_probe_rejects_writable_transaction_and_disposes_engine(self) -> None:
        self.run_code(
            """
            import asyncio
            from unittest.mock import AsyncMock, MagicMock, patch
            from app.core import database, runtime_readiness
            connection = MagicMock()
            connection.scalar = AsyncMock(return_value='off')
            connection.execute = AsyncMock()
            engine = MagicMock()
            engine.connect.return_value.__aenter__.return_value = connection
            engine.dispose = AsyncMock()
            with patch.object(database, 'create_readonly_engine', return_value=engine):
                result = asyncio.run(runtime_readiness._check_readonly_database())
            assert result == (False, False)
            connection.execute.assert_not_awaited()
            engine.dispose.assert_awaited_once()
            """
        )

    def test_publication_providers_refuse_before_missing_credentials_or_http(self) -> None:
        self.run_code(
            """
            import asyncio
            from unittest.mock import patch
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.integrations.telegram import TelegramProvider, VKProvider
            with patch('httpx.AsyncClient', side_effect=AssertionError('provider I/O')) as http:
                for cls in (TelegramProvider, VKProvider):
                    try:
                        cls()
                    except RuntimeAdmissionError as error:
                        assert error.code == 'RUNTIME_PASSIVE'
                    else:
                        raise AssertionError('provider constructed')
                    provider = object.__new__(cls)
                    try:
                        asyncio.run(provider.publish(text='candidate', chat_id='-123'))
                    except RuntimeAdmissionError as error:
                        assert error.code == 'RUNTIME_PASSIVE'
                    else:
                        raise AssertionError('provider sent')
                http.assert_not_called()
            """
        )

    def test_credential_refresh_refuses_before_file_lock_or_network(self) -> None:
        self.run_code(
            """
            import asyncio
            from unittest.mock import patch
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.integrations.vk_credentials import VKCredentialStore
            store = object.__new__(VKCredentialStore)
            with patch('os.open', side_effect=AssertionError('credential I/O')) as opened:
                for call in (
                    lambda: asyncio.run(store.access_token(force_refresh=True)),
                    lambda: asyncio.run(store._refresh(None)),
                    lambda: store._locked(0).__enter__(),
                    lambda: asyncio.run(store._vk_method(None, 'wall.get', 'unused')),
                ):
                    try:
                        call()
                    except RuntimeAdmissionError as error:
                        assert error.code == 'RUNTIME_PASSIVE'
                    else:
                        raise AssertionError('credential operation admitted')
                opened.assert_not_called()
            """
        )

    def test_credential_admin_requires_explicit_active_and_never_exempts_maintenance(self) -> None:
        code = """
            from unittest.mock import patch
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.integrations.vk_credentials import VKCredentialStore
            with patch('os.open', side_effect=AssertionError('file I/O')) as opened:
                with patch('httpx.AsyncClient', side_effect=AssertionError('HTTP I/O')) as http:
                    try:
                        store = VKCredentialStore(
                            '/unused-credential-fixture/credentials.json',
                            client_id=1, expected_user_id=2, owner_id=-3,
                            api_version='5.199', request_timeout_seconds=1,
                            refresh_leeway_seconds=30, owner_uid=10001,
                        )
                    except RuntimeAdmissionError as error:
                        assert EXPECTED_ALLOWED is False
                        assert error.code == 'RUNTIME_PASSIVE'
                    else:
                        assert EXPECTED_ALLOWED is True
                        assert store.owner_id == -3
                    opened.assert_not_called()
                    http.assert_not_called()
        """
        for app_env, mode, allowed in (
            ("credential_admin", "active", True),
            ("credential_admin", "passive", False),
            ("maintenance", "active", False),
        ):
            with self.subTest(app_env=app_env, mode=mode):
                self.run_code(
                    code.replace("EXPECTED_ALLOWED", str(allowed)),
                    mode=mode,
                    app_env=app_env,
                )

    @unittest.skipUnless(HAS_OPENAI, "full backend dependencies required")
    def test_openai_provider_refuses_before_client_and_all_provider_calls(self) -> None:
        self.run_code(
            """
            import asyncio
            from unittest.mock import patch
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.integrations.openai_knowledge import OpenAIKnowledgeProvider
            with patch('app.integrations.openai_knowledge.AsyncOpenAI') as client:
                try:
                    OpenAIKnowledgeProvider()
                except RuntimeAdmissionError as error:
                    assert error.code == 'RUNTIME_PASSIVE'
                else:
                    raise AssertionError('provider constructed')
                client.assert_not_called()
            provider = object.__new__(OpenAIKnowledgeProvider)
            for name, args in (
                ('retrieve_store', ('store',)), ('create_store', ('candidate',)),
                ('upload_file', ('file', b'candidate')), ('attach_file', ('store', 'file', {})),
                ('get_file_status', ('store', 'file')), ('detach_file', ('store', 'file')),
                ('search', ('store', 'query', 1)),
            ):
                try:
                    asyncio.run(getattr(provider, name)(*args))
                except RuntimeAdmissionError as error:
                    assert error.code == 'RUNTIME_PASSIVE'
                else:
                    raise AssertionError('provider call admitted: ' + name)
            """
        )

    @unittest.skipUnless(HAS_METRICS, "full backend dependencies required")
    def test_metrics_provider_refuses_even_when_credentials_are_absent(self) -> None:
        self.run_code(
            """
            import asyncio
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.integrations.metrics import TelegramMetricsProvider, VKMetricsProvider
            for provider in (TelegramMetricsProvider(), VKMetricsProvider()):
                try:
                    asyncio.run(provider.get_metrics(external_id='123'))
                except RuntimeAdmissionError as error:
                    assert error.code == 'RUNTIME_PASSIVE'
                else:
                    raise AssertionError('metrics provider admitted')
            """
        )

    @unittest.skipUnless(HAS_DATABASE and HAS_AGENTS, "full backend dependencies required")
    def test_saved_publication_attempt_and_durable_claims_refuse_before_db_mutation(self) -> None:
        self.run_code(
            """
            import asyncio
            from datetime import UTC, datetime
            from uuid import uuid4
            from app.core.runtime_admission import RuntimeAdmissionError
            from app.services.agent_run_service import AgentRunService
            from app.services.agent_runner_service import AgentRunnerService
            from app.services.publication_service import PublicationService
            from app.services.task_dispatcher_service import TaskDispatcherService
            from app.services.task_recovery_service import TaskRecoveryService
            class Untouched:
                def __getattr__(self, name):
                    raise AssertionError('side effect before admission: ' + name)
            session = Untouched()
            publication = PublicationService(session)
            runs = AgentRunService(session)
            calls = (
                publication.execute_vk(uuid4(), session, execution_token='saved-attempt'),
                publication.execute_telegram(uuid4(), session, execution_token='saved-attempt'),
                publication.claim_for_publish(uuid4()),
                publication.claim_canary_retry(uuid4(), session),
                publication.recover_stuck_publishing(cutoff=datetime.now(UTC)),
                publication.recover_one_stuck(uuid4()),
                runs.create_queued_run(uuid4()), runs.claim(uuid4()), runs.enqueue(session),
                TaskDispatcherService(session).dispatch_ready_tasks(),
                TaskRecoveryService(session).recover_stuck(),
                AgentRunnerService().run(session, 'candidate', session, None),
            )
            for call in calls:
                try:
                    asyncio.run(call)
                except RuntimeAdmissionError as error:
                    assert error.code == 'RUNTIME_PASSIVE'
                else:
                    raise AssertionError('claim/attempt admitted')
            """
        )


if __name__ == "__main__":
    unittest.main()
