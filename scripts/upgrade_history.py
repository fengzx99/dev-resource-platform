"""Replace only the portal. Never restart executor, identity, DB or workspaces."""
import argparse
import copy
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
import ssl

from upgrade_monitoring import api, sql_state, secret_state, emit

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / '.remote-dev/backups/history-20261021'
IMAGE = 'dev-resources/platform:1.1.0-history-20261021'


def inspect(name):
    return api('GET', '/containers/' + quote(name, safe='') + '/json')


def verify_untouched(state):
    assert state['accounts'] == sql_state(), 'Accounts or credentials changed'
    assert state['secrets'] == secret_state(), 'Secret files changed'
    count = 0
    for name, old in state['containers'].items():
        if name == 'drp-portal' or not old['State']['Running']:
            continue
        current = inspect(name)
        assert current['Id'] == old['Id'], name + ': container replaced'
        assert current['State']['Running'], name + ': container stopped'
        assert current['State']['StartedAt'] == old['State']['StartedAt'], name + ': restarted'
        assert current['Config'] == old['Config'] and current['HostConfig'] == old['HostConfig'], name + ': resource/mount configuration changed'
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, default=BACKUP)
    parser.add_argument('--image', default=IMAGE)
    args = parser.parse_args()
    assert args.backup.resolve().is_relative_to((ROOT / '.remote-dev/backups').resolve())
    state = json.loads((args.backup / 'state.json').read_text())
    count = verify_untouched(state)
    old = state['containers']['drp-portal']
    assert inspect('drp-portal')['Id'] == old['Id']
    api('GET', '/images/' + quote(args.image, safe='') + '/json')
    payload = copy.deepcopy(old['Config'])
    payload['Image'] = args.image
    payload['HostConfig'] = copy.deepcopy(old['HostConfig'])
    payload['Labels']['io.devresources.history'] = '1'
    if old['Id'].startswith(payload.get('Hostname', '')):
        payload['Hostname'] = ''
    endpoints = {}
    for name, endpoint in old['NetworkSettings']['Networks'].items():
        endpoints[name] = {k: endpoint[k] for k in ['Aliases', 'Links', 'IPAMConfig', 'DriverOpts'] if endpoint.get(k) is not None}
        endpoints[name]['Aliases'] = [a for a in endpoints[name].get('Aliases', []) if a not in (old['Id'], old['Id'][:12])]
    payload['NetworkingConfig'] = {'EndpointsConfig': endpoints}
    new = None
    stopped = False
    try:
        emit('replace_portal_only', untouched_running_containers=count)
        api('POST', '/containers/' + old['Id'] + '/stop?t=20'); stopped = True
        api('POST', '/containers/' + old['Id'] + '/rename?name=' + quote('drp-portal-previous-' + args.backup.name))
        new = api('POST', '/containers/create?name=drp-portal', payload)['Id']
        api('POST', '/containers/' + new + '/start')
        cfg = json.loads((ROOT / 'runtime/secrets/platform.json').read_text())
        context = ssl.create_default_context(cafile=str(ROOT / 'runtime/secrets/server.crt'))
        for attempt in range(40):
            try:
                with urlopen(cfg['public_url'] + '/healthz', context=context, timeout=3) as response:
                    assert response.status == 200
                break
            except Exception:
                if attempt == 39:
                    raise
                time.sleep(1)
        count = verify_untouched(state)
        subprocess.run(['docker', 'tag', old['Image'], 'dev-resources/platform:1.1.0-before-' + args.backup.name], check=True)
        subprocess.run(['docker', 'tag', args.image, 'dev-resources/platform:1.1.0'], check=True)
        emit('history_upgrade_verified', http=200, accounts_and_secrets_unchanged=True,
             untouched_running_containers=count, executor_database_identity_untouched=True,
             previous_portal_retained=True)
    except BaseException:
        emit('rollback_begin')
        if new:
            api('DELETE', '/containers/' + new + '?force=true')
        if inspect(old['Id'])['Name'] != '/drp-portal':
            api('POST', '/containers/' + old['Id'] + '/rename?name=drp-portal')
        if stopped:
            api('POST', '/containers/' + old['Id'] + '/start')
        emit('rollback_restored_portal')
        raise


if __name__ == '__main__':
    main()
