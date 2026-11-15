"""Additional terminal checks for protocol v2; no unseen-turn failures."""
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime,timezone
import pytest

TASK=os.environ['RC_CONTINUOUS_TASK']
TURN=int(os.environ['RC_CONTINUOUS_DELIVERED_TURN'])
pytestmark=pytest.mark.skipif(TURN<37,reason='Full terminal requirements not received')


def only(task):return pytest.mark.skipif(TASK!=task,reason='Other task')
def now():return datetime(2026,11,3,12,tzinfo=timezone.utc)
def csv_file(path):
    path.write_text('account_id,external_id,occurred_at,amount,description\na,z,2026-11-03T10:00:00Z,1.00,z\na,x,2026-11-03T10:00:00Z,1.00,x\na,y,2026-11-03T10:00:00Z,1.00,y\n',encoding='utf-8');return path


@only('ledgerflow')
def test_ledger_keyset_follows_time_and_external_id_not_insert_order(tmp_path):
    m=importlib.import_module('ledgerflow');db=tmp_path/'ledger.db';m.import_csv(csv_file(tmp_path/'rows.csv'),db)
    found=[];cursor=None
    for _ in range(4):
        rows,cursor=m.list_transactions(db,'a',limit=1,cursor=cursor)
        found.extend(r['external_id'] for r in rows)
        if cursor is None:break
    assert found==['x','y','z'] and cursor is None


@only('ledgerflow')
def test_ledger_existing_database_dry_run_is_byte_unchanged(tmp_path):
    m=importlib.import_module('ledgerflow');db=tmp_path/'ledger.db';csv=csv_file(tmp_path/'rows.csv')
    m.import_csv(csv,db);before=db.read_bytes()
    m.import_csv(csv,db,dry_run=True)
    assert db.read_bytes()==before


@only('queuecraft')
def test_queue_list_and_audit_are_real_interfaces(tmp_path):
    m=importlib.import_module('queuecraft');db=tmp_path/'queue.db';q=m.QueueStore(db)
    first=q.enqueue('q',{'x':1},'k',now=now())
    rows,cursor=q.list_jobs('q',limit=50,cursor=None)
    assert any(r['job_id']==first['job_id'] for r in rows) and cursor is None
    with sqlite3.connect(db) as c:assert c.execute('select count(*) from audit_events').fetchone()[0]>=1


@only('queuecraft')
def test_queue_completed_status_is_succeeded(tmp_path):
    q=importlib.import_module('queuecraft').QueueStore(tmp_path/'queue.db')
    q.enqueue('q',{'x':1},'k',now=now());job=q.claim('q','worker',30,now=now())
    assert q.complete(job['job_id'],'worker',job['fencing_token'],now=now()) is True
    # Isolate the old reference's wrong status from its independently missing
    # list_jobs. get_job is optional; the declared list API remains the fallback.
    getter=getattr(q,'get_job',None)
    if callable(getter):
        result=getter(job['job_id'])
        if isinstance(result,dict) and 'status' in result:
            assert result['status']=='succeeded';return
    rows,_=q.list_jobs('q',status=None,limit=50,cursor=None)
    assert any(r['job_id']==job['job_id'] and r['status']=='succeeded' for r in rows)


def cli(args):return subprocess.run([sys.executable,'-m',TASK,*args],capture_output=True,text=True,timeout=20)


def test_cli_negative_has_machine_readable_error():
    result=cli(['invalid-subcommand'])
    assert result.returncode!=0 and json.loads(result.stderr)['error_code']


@only('ledgerflow')
def test_ledger_cli_positive(tmp_path):
    result=cli(['import',str(csv_file(tmp_path/'rows.csv')),'--db',str(tmp_path/'ledger.db')])
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout);assert data['inserted']==3 and data['skipped']==0


@only('queuecraft')
def test_queue_cli_positive(tmp_path):
    db=tmp_path/'queue.db'
    result=cli(['enqueue','--db',str(db),'--queue','q','--payload','{"x":1}','--idempotency-key','k','--now',now().isoformat()])
    assert result.returncode==0,result.stderr
    assert isinstance(json.loads(result.stdout),dict)
    result=cli(['doctor','--db',str(db)])
    assert result.returncode==0 and isinstance(json.loads(result.stdout),dict)
