"""Regression expectations that intentionally FAIL against reviewed commit f9cd3b0.
Run: PYTHONPATH=$PWD .venv/bin/python -m pytest reports/review-evidence/test_review_regressions.py -q
These tests do not modify the implementation or its existing runtime data.
"""
import time
from dataclasses import replace
from mlh.dashboard import node_coverage
from mlh.registry import Registry
from mlh.settings import load_settings
from mlh.supervisor import Supervisor

def test_flush_tail_allowance_must_not_hide_an_internal_gap():
    actual = node_coverage({'first_lo':1000,'committed_hi':1892},
        [{'lo':1400,'hi':1409,'state':'failed'}],1000,1900,18)
    assert actual['state']=='partial', actual

def test_leading_gap_cannot_spend_flush_tail_allowance():
    # Whole historical gap is 19s; only its last 9s intersect the selected range.
    actual = node_coverage({'first_lo':1000,'committed_hi':2392},
        [{'lo':1490,'hi':1509,'state':'failed'}],1500,2400,18)
    assert actual['state']=='partial', actual

class Owner:
    def __init__(self): self.calls=[]
    def execute(self,sql,params=None):self.calls.append(params)

def test_pending_gap_must_be_rechecked_against_max_backfill_age(tmp_path):
    reg=Registry(tmp_path/'r.sqlite3');cid='c000000000001';nid='n000000000001'
    reg.insert_cluster({'id':cid,'name':'fixture','host':'localhost','port':9000,'tls':False,'username':'u','discovered_cluster':None,'candidate_count':1,'server_version':'26.8'},
        [{'id':nid,'cluster_id':cid,'host':'localhost','port':9000,'display_name':'n','server_uuid':'fixture-uuid','server_version':'26.8'}])
    reg.set_node_phase(nid,'active'); now=int(time.time())
    reg.upsert_source_status({'node_id':nid,'source':'query_log','state':'collecting','last_attempt_at':now,'last_success_at':now,'last_error_category':None,'last_error_message':None,'last_error_codes':None,'consecutive_failures':0})
    reg.add_gap(nid,now-2000,now-1970,'pending','initial')
    owner=Owner(); s=replace(load_settings(),max_backfill_seconds=600)
    Supervisor(owner,reg,s)._backfill_one(now)
    assert not owner.calls, {'executed_outside_horizon':owner.calls,'gap_after':reg.gaps([nid])}
    assert reg.gaps([nid])[0]['state']=='unrecoverable'
