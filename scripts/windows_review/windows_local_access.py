"""Loopback-only browser access through Docker exec; no Docker/network/DB mutations.

Put next to HANDOFF-CODEX.md in the restored Windows project. Uses Python stdlib.
No HTTP bodies, credentials, command stderr or environment values are logged.
Ctrl+C stops the bridge; it leaves all application containers and data untouched.
"""
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import threading
import time
import urllib.request

from review_validation import (
    CONTEXT, docker_read, require_existing_review_volumes, require_review_host, validate_inspect_mounts,
)

PROJECT = 'iteam_windows_transfer'
ROOT = Path(__file__).resolve().parent
SERVICES = {'backend', 'frontend', 'postgres', 'redis'}
FLAGS = ['VK_PUBLISHING_ENABLED', 'TELEGRAM_PUBLISHING_ENABLED', 'VK_METRICS_ENABLED',
         'TELEGRAM_METRICS_ENABLED', 'METRICS_SYNC_ENABLED']
CREDENTIALS = ['VK_ACCESS_TOKEN', 'VK_CREDENTIALS_FILE', 'TELEGRAM_BOT_TOKEN',
               'VK_METRICS_ACCESS_TOKEN', 'TELEGRAM_METRICS_SESSION', 'WORKER_ROLE', 'SCHEDULER_ROLE']

# Runs only inside the already-running backend, talking to two fixed internal targets.
REMOTE = r'''
import os,select,socket,sys
s=None
try:
 host,port=sys.argv[1],int(sys.argv[2])
 if (host,port) not in [('127.0.0.1',8000),('frontend',3000)]: raise ValueError()
 s=socket.create_connection((host,port),timeout=8)
 s.settimeout(None)
 inputs=[s,0]
 while s in inputs:
  ready,_,_=select.select(inputs,[],[],90)
  if not ready: break
  for channel in ready:
   if channel==0:
    data=os.read(0,65536)
    if not data:
     inputs.remove(0)
     s.shutdown(socket.SHUT_WR)
    else: s.sendall(data)
   else:
    data=s.recv(65536)
    if not data:
     inputs.remove(s)
     break
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
except Exception:
 pass
finally:
 if s is not None: s.close()
'''


def check_snapshot(rows, networks, release):
    """Validate actual running containers, rather than trusting a Compose file alone."""
    if len(rows) != 4:
        raise RuntimeError('EXACTLY_FOUR_REVIEW_SERVICES_REQUIRED')
    by_service = {}
    network_names = set()
    for row in rows:
        labels = row['Config'].get('Labels') or {}
        name = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != PROJECT or name not in SERVICES or name in by_service:
            raise RuntimeError('UNEXPECTED_REVIEW_SERVICE')
        if row['State']['Status'] != 'running':
            raise RuntimeError('REVIEW_SERVICE_NOT_RUNNING')
        if row['HostConfig'].get('Privileged') or row['HostConfig'].get('NetworkMode') == 'host':
            raise RuntimeError('ISOLATION_REQUIRED')
        attached = row['NetworkSettings']['Networks']
        if len(attached) != 1 or any(networks.get(n, {}).get('Internal') is not True for n in attached):
            raise RuntimeError('INTERNAL_NETWORK_REQUIRED')
        network_names.update(attached)
        if name in ('backend', 'frontend') and row['Config']['Image'] != release['images'][name]:
            raise RuntimeError('EXACT_RELEASE_REQUIRED')
        if row['HostConfig'].get('PublishAllPorts'):
            raise RuntimeError('LOOPBACK_BIND_REQUIRED')
        for port_map in (row['HostConfig'].get('PortBindings'), row['NetworkSettings'].get('Ports')):
            for bindings in (port_map or {}).values():
                if any(b.get('HostIp') != '127.0.0.1' for b in bindings or []):
                    raise RuntimeError('LOOPBACK_BIND_REQUIRED')
        by_service[name] = row
    if set(by_service) != SERVICES:
        raise RuntimeError('MISSING_REVIEW_SERVICE')
    if len(network_names) != 1:
        raise RuntimeError('SHARED_INTERNAL_NETWORK_REQUIRED')
    members = networks[next(iter(network_names))].get('Containers')
    if not isinstance(members, dict) or set(members) != {row['Id'] for row in by_service.values()}:
        raise RuntimeError('REVIEW_NETWORK_MEMBERSHIP_MISMATCH')
    env = dict(v.split('=', 1) for v in by_service['backend']['Config']['Env'] if '=' in v)
    if any(env.get(k, '').lower() != 'false' for k in FLAGS) or any(env.get(k) for k in CREDENTIALS):
        raise RuntimeError('PUBLISHING_OR_EXECUTION_NOT_DISABLED')
    if env.get('BUILD_SHA') != release['release_sha']:
        raise RuntimeError('BUILD_SHA_MISMATCH')
    validate_inspect_mounts(by_service)
    return by_service['backend']['Id']


def check_review(release):
    require_review_host(ROOT)
    require_existing_review_volumes()
    ids = docker_read('ps', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT).decode().split()
    if not ids:
        raise RuntimeError('REVIEW_SERVICES_NOT_RUNNING')
    rows = json.loads(docker_read('inspect', *ids))
    names = {n for row in rows for n in row['NetworkSettings']['Networks']}
    networks = {row['Name']: row for row in json.loads(docker_read('network', 'inspect', *sorted(names)))}
    return check_snapshot(rows, networks, release)


class Bridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, port, backend, target):
        self.backend, self.target = backend, target
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(('127.0.0.1', port), Handler)

    def handle_error(self, request, client_address):
        pass  # Never log request data, provider responses or native command stderr.


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        child = None
        feed = None
        try:
            child = subprocess.Popen(['docker', '--context', CONTEXT, 'exec', '-i', self.server.backend, 'python', '-u', '-c',
                                      REMOTE, self.server.target[0], str(self.server.target[1])],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.request.settimeout(90)

            def upload():
                try:
                    while True:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        child.stdin.write(data)
                        child.stdin.flush()
                except Exception:
                    pass
                finally:
                    try:
                        child.stdin.close()
                    except Exception:
                        pass

            feed = threading.Thread(target=upload, daemon=True)
            feed.start()
            while True:
                data = child.stdout.read1(65536)
                if not data:
                    break
                self.request.sendall(data)
        except Exception:
            pass
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if feed:
                feed.join(timeout=2)
            if child:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
                child.stdout.close()
            self.server.slots.release()


def main():
    release_path = ROOT / 'windows/release.json'
    if not release_path.is_file():
        raise RuntimeError('PLACE_THIS_FILE_IN_THE_RESTORED_PROJECT_FOLDER')
    if not (ROOT / 'windows/database-import-PASS.json').is_file():
        raise RuntimeError('VERIFIED_DATABASE_RESTORE_REQUIRED')
    release = json.loads(release_path.read_text())
    backend = check_review(release)
    servers = []
    try:
        # Bind both ports before starting threads; never take over an occupied port.
        servers.append(Bridge(8000, backend, ('127.0.0.1', 8000)))
        servers.append(Bridge(3000, backend, ('frontend', 3000)))
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in ['http://localhost:8000/health', 'http://localhost:8000/health/ready',
                    'http://localhost:3000/login']:
            with opener.open(url, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError('LOCAL_HEALTH_CHECK_FAILED')
        print('LOCAL ACCESS: PASS', flush=True)
        print('BUILD_SHA, health, readiness, login page: PASS.', flush=True)
        print('Open http://localhost:3000. Keep this window open. Ctrl+C stops local access.', flush=True)
        print('Publishing OFF. No workers. Container network remains internal. Credentials unchanged.', flush=True)
        while True:
            time.sleep(10)
            if check_review(release) != backend:
                raise RuntimeError('REVIEW_CONTAINER_CHANGED_STOPPING_LOCAL_ACCESS')
    finally:
        for server in servers:
            server.server_close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Local access stopped. Containers and data preserved.')
    except Exception as error:
        if isinstance(error, OSError) and getattr(error, 'winerror', None) == 10048:
            print('LOCAL_PORT_ALREADY_IN_USE: close the other local access process; no settings changed.')
        else:
            reason = str(error) if type(error) is RuntimeError else 'LOCAL_ACCESS_FAILED_DETAILS_HIDDEN'
            print(reason)
        raise SystemExit(1)
