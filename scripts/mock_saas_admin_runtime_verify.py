"""Verify and remove only an owned browser configuration fixture."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import urllib.request

REMOTE = '''
import json,sys,sqlite3,os,hashlib
f=json.load(sys.stdin)
assert f['client_id'].startswith('qa-admin-ui-')
assert f['product_id'].startswith('qa-admin-ui-product-')
with sqlite3.connect('file:'+os.environ.get('MOCK_SAAS_DB_PATH','/data/mock-saas.sqlite3')+'?mode=ro',uri=True) as db:
    row=db.execute('SELECT config FROM clients WHERE client_id=?',(f['client_id'],)).fetchone()
    assert row
    c=json.loads(row[0])
    assert c['client_secret']==f['client_secret']
    assert c['product_id']==f['product_id']
    assert c['product_name']=='Updated browser verification'
    rows=db.execute('SELECT client_id,config FROM clients WHERE client_id!=? ORDER BY client_id',(f['client_id'],)).fetchall()
    print(json.dumps({'blank_secret_preserved':True,'original_clients_sha256':hashlib.sha256(json.dumps(rows).encode()).hexdigest(),'original_client_count':len(rows)}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-clients-sha256', required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    if not (fixture['client_id'].startswith('qa-admin-ui-')
            and fixture['product_id'].startswith('qa-admin-ui-product-')):
        raise ValueError('Only owned synthetic fixture cleanup is allowed')
    command = 'kubectl -n market exec -i deployment/market-mock-saas -- python -c ' + shlex.quote(REMOTE)
    # SSH credentials are supplied using the caller's normal environment/SSH agent.
    result = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', 'root@192.168.10.10', command],
        input=json.dumps(fixture), text=True, capture_output=True, check=True)
    evidence = json.loads(result.stdout)
    assert evidence['original_clients_sha256'] == args.baseline_clients_sha256
    request = urllib.request.Request('http://192.168.10.10:30083/_test/oidc/clients/' + fixture['client_id'],
        method='DELETE', headers={'X-Mock-Admin-Secret': fixture['admin_key']})
    with urllib.request.urlopen(request, timeout=15) as response:
        assert json.load(response)['deleted'] is True
    request = urllib.request.Request('http://192.168.10.10:30083/admin/api/clients',
        headers={'X-Mock-Admin-Secret': fixture['admin_key']})
    with urllib.request.urlopen(request, timeout=15) as response:
        registry = json.load(response)
    assert all(c['client_id'] != fixture['client_id'] and 'client_secret' not in c for c in registry['clients'])
    assert len(registry['clients']) == evidence['original_client_count']
    evidence.update(status='passed', owned_fixture_deleted=True, secrets_returned=False)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
