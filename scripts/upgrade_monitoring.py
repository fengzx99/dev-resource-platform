"""Replace only portal/executor, retaining their old containers for rollback."""
import argparse
import copy
import hashlib
import http.client
import json
import os
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def emit(step, **fields):
    print(json.dumps({'step': step, **fields}, ensure_ascii=False), flush=True)


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def api(method, path, payload=None):
    connection = UnixHTTP('localhost', timeout=60)
    try:
        connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError('Docker request failed: ' + method + ' ' + path + ' HTTP ' + str(response.status))
        return json.loads(body) if body else None
    finally:
        connection.close()


def sql_state():
    out = {}
    for table in ['public.users', 'keycloak.user_entity', 'keycloak.credential', 'keycloak.user_role_mapping']:
        order = 'user_id,role_id' if table.endswith('user_role_mapping') else 'id'
        p = subprocess.run(['docker', 'exec', '-i', 'drp-database', 'psql', '-X', '-qAt',
                            '-U', 'platform', '-d', 'platform', '-v', 'ON_ERROR_STOP=1'],
                           input='BEGIN READ ONLY; SELECT row_to_json(t)::text FROM ' + table + ' t ORDER BY ' + order + '; ROLLBACK;',
                           capture_output=True, text=True, timeout=15, check=True)
        out[table] = hashlib.sha256(p.stdout.encode()).hexdigest()
    return out


def secret_state():
    directory = ROOT / 'runtime/secrets'
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    assert args.backup.resolve().is_relative_to((ROOT / '.remote-dev/backups').resolve())
    state = json.loads((args.backup / 'state.json').read_text())
    originals = state['containers']
    prefix = '/v' + api('GET', '/version')['ApiVersion']
    def request(method, path, payload=None): return api(method, prefix + path, payload)
    def inspect(name): return request('GET', '/containers/' + quote(name, safe='') + '/json')
    request('GET', '/images/' + quote(args.image, safe='') + '/json')
    assert state['accounts'] == sql_state(), 'Account baseline changed; refusing upgrade'
    assert state['secrets'] == secret_state(), 'Secret baseline changed; refusing upgrade'
    for name, old in originals.items():
        current = inspect(name)
        assert current['Id'] == old['Id'] and current['State']['Running']
    changed, created = [], {}
    suffix = args.backup.name.replace('monitoring-', '')
    try:
        for name in ['drp-executor', 'drp-portal']:
            old = originals[name]
            assert old['Config']['Labels'].get('io.devresources.infrastructure') == '1'
            payload = copy.deepcopy(old['Config'])
            payload['Image'] = args.image
            payload['HostConfig'] = copy.deepcopy(old['HostConfig'])
            payload['Labels']['io.devresources.monitoring'] = '1'
            if old['Id'].startswith(payload.get('Hostname', '')):
                payload['Hostname'] = ''
            endpoints = {}
            for network, endpoint in old['NetworkSettings']['Networks'].items():
                endpoints[network] = {key: endpoint[key] for key in ['Aliases', 'Links', 'IPAMConfig', 'DriverOpts'] if endpoint.get(key) is not None}
                endpoints[network]['Aliases'] = [a for a in endpoints[network].get('Aliases', []) if a not in (old['Id'], old['Id'][:12])]
            payload['NetworkingConfig'] = {'EndpointsConfig': endpoints}
            if name == 'drp-executor':
                payload['HostConfig']['PidMode'] = 'host'
                binds = payload['HostConfig'].setdefault('Binds', [])
                for bind in ['/proc:/host/proc:ro', '/:/host/fs:ro']:
                    if bind not in binds: binds.append(bind)
            emit('replace_begin', container=name)
            changed.append(name)
            request('POST', '/containers/' + old['Id'] + '/stop?t=20')
            request('POST', '/containers/' + old['Id'] + '/rename?name=' + quote(name + '-previous-' + suffix))
            new = request('POST', '/containers/create?name=' + name, payload)
            created[name] = new['Id']
            request('POST', '/containers/' + new['Id'] + '/start')
            emit('replace_started', container=name, running=inspect(name)['State']['Running'])

        cfg = json.loads((ROOT / 'runtime/secrets/platform.json').read_text())
        context = ssl.create_default_context(cafile=str(ROOT / 'runtime/secrets/server.crt'))
        for attempt in range(40):
            try:
                with urlopen(cfg['public_url'] + '/healthz', context=context, timeout=3) as response:
                    assert response.status == 200
                    break
            except Exception:
                if attempt == 39: raise
                time.sleep(1)
        assert state['accounts'] == sql_state(), 'Account or credential records changed'
        assert state['secrets'] == secret_state(), 'Secret files changed'
        for name in ['drp-database', 'drp-keycloak']:
            now = inspect(name)
            assert now['Id'] == originals[name]['Id']
            assert now['State']['StartedAt'] == originals[name]['State']['StartedAt']
        subprocess.run(['docker','tag',originals['drp-portal']['Image'],
                        'dev-resources/platform:1.1.0-before-monitoring-'+suffix],check=True)
        subprocess.run(['docker','tag',args.image,'dev-resources/platform:1.1.0'],check=True)
        emit('upgrade_verified', portal_http=200, accounts_unchanged=True, secrets_unchanged=True,
             database_and_keycloak_untouched=True, previous_containers_retained=True)
    except BaseException:
        emit('rollback_begin')
        for name in reversed(changed):
            try:
                if name in created:
                    request('DELETE', '/containers/' + created[name] + '?force=true')
                old = originals[name]
                if inspect(old['Id'])['Name'] != '/' + name:
                    request('POST', '/containers/' + old['Id'] + '/rename?name=' + name)
                if not inspect(old['Id'])['State']['Running']:
                    request('POST', '/containers/' + old['Id'] + '/start')
                emit('rollback_restored', container=name)
            except Exception as exc:
                emit('rollback_error', container=name, error_type=type(exc).__name__)
        raise


if __name__ == '__main__':
    main()
