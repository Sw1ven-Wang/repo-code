"""Run INSIDE an isolated grader after each handoff; future checks skip."""
import importlib
import os
import sqlite3
import pytest

TASK=os.environ['RC_CONTINUOUS_TASK']
TURN=int(os.environ['RC_CONTINUOUS_DELIVERED_TURN'])


def applicable(task,turn):return pytest.mark.skipif(TASK!=task or TURN<turn,reason='Requirement not received for this stage')


@applicable('ledgerflow',2)
def test_ledger_api_skeleton():
    m=importlib.import_module('ledgerflow')
    for name in ['parse_amount_to_cents','parse_timestamp','import_csv','account_totals','list_transactions']:
        assert callable(getattr(m,name,None)),name


@applicable('ledgerflow',4)
def test_ledger_exact_basic_cents():
    m=importlib.import_module('ledgerflow')
    assert m.parse_amount_to_cents('0.29')==29
    assert m.parse_amount_to_cents('-1.20')==-120


@applicable('queuecraft',2)
def test_queue_api_skeleton():
    cls=importlib.import_module('queuecraft').QueueStore
    for name in ['enqueue','claim','complete','fail','reclaim_expired','list_jobs']:
        assert callable(getattr(cls,name,None)),name


@applicable('queuecraft',4)
def test_queue_declared_tables(tmp_path):
    db=tmp_path/'queue.db';importlib.import_module('queuecraft').QueueStore(db)
    with sqlite3.connect(db) as c:tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    assert {'jobs','audit_events'}<=tables
