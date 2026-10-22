"""Consistent PostgreSQL dump and secret backup; workspace files stay on JuiceFS."""
import argparse,datetime,os,pathlib,subprocess,tarfile,json
root=pathlib.Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--verify-restore',action='store_true');a=p.parse_args()
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=root/'runtime/backups'/stamp;out.mkdir(parents=True,mode=0o700);os.chmod(out.parent,0o700)
    dump=out/'platform.dump'
    with dump.open('wb') as f:subprocess.run(['docker','exec','drp-database','pg_dump','-U','platform','-d','platform','-Fc'],stdout=f,check=True)
    os.chmod(dump,0o600)
    with tarfile.open(out/'secrets.tar.gz','w:gz') as t:t.add(root/'runtime/secrets',arcname='secrets')
    os.chmod(out/'secrets.tar.gz',0o600)
    result={'database_dump':str(dump),'secrets_backup':str(out/'secrets.tar.gz'),'volume_path':str(root/'data/volumes'),'workspace_files_included':False}
    if a.verify_restore:
        db='drp_restore_'+stamp.lower()
        subprocess.run(['docker','exec','drp-database','createdb','-U','platform',db],check=True)
        try:
            with dump.open('rb') as f:subprocess.run(['docker','exec','-i','drp-database','pg_restore','-U','platform','-d',db,'--exit-on-error'],stdin=f,check=True)
            counts=[]
            for database in ('platform',db):
                r=subprocess.run(['docker','exec','drp-database','psql','-U','platform','-d',database,'-Atc','SELECT (SELECT count(*) FROM public.users), (SELECT count(*) FROM public.projects), (SELECT count(*) FROM public.workspaces), (SELECT count(*) FROM public.audit), (SELECT count(*) FROM keycloak.user_entity), (SELECT count(*) FROM public.nodes), (SELECT count(*) FROM public.volumes);'],capture_output=True,text=True,check=True);counts.append(r.stdout.strip())
            if counts[0]!=counts[1]:raise RuntimeError('Restore row counts do not match')
            result.update({'restore_verified':True,'restored_row_counts':counts[1]})
        finally:subprocess.run(['docker','exec','drp-database','dropdb','-U','platform',db],check=True)
    (out/'manifest.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
