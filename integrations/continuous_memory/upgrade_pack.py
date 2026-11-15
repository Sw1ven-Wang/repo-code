"""Create a NEW taskpack version; never edit historical tasks or grades in place."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path


def upgrade(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Destination must be new')
    if destination==source or destination.is_relative_to(source):raise ValueError('Keep upgraded pack outside the historical source')
    destination.mkdir(parents=True)
    shutil.copytree(source/'continuous_memory',destination/'continuous_memory')
    shutil.copytree(source/'grader_only/continuous_memory',destination/'grader_only/continuous_memory')
    control=destination/'continuous_memory/controller_only'
    for task in ['ledgerflow','queuecraft']:
        file=control/(task+'.turns.jsonl')
        turns=[json.loads(line) for line in file.read_text(encoding='utf-8').splitlines() if line.strip()]
        assert len(turns)==37 and [r['turn'] for r in turns]==list(range(1,38))
        # Declare the API at project creation, avoiding expensive provisional
        # implementations. Later behavior corrections retain their turn/order.
        turns[0]['user_message']+='\n项目接口约定从现在起固定如下（下一轮仍会确认）：\n'+turns[1]['user_message']
        if task=='ledgerflow':
            turns[0]['user_message']+='\nCSV 列固定为 account_id,external_id,occurred_at,amount,description。'
            turns[9]['user_message']+='\n只读含义明确为：数据库不存在时不创建磁盘数据库；数据库存在时不改变文件、表、交易或审计。可以用内存数据库做预演。'
            turns[13]['user_message']+='\nCLI 导入语法固定为 python -m ledgerflow import CSV_PATH --db DB_PATH，支持 --dry-run 和 --preview。成功 JSON 至少包含 inserted、skipped；错误 JSON 写 stderr，含 error_code 和 line。'
        else:
            turns[28]['kind']='recall_probe';turns[29]['kind']='user_message'
            turns[21]['user_message']+='\n固定 enqueue 语法：python -m queuecraft enqueue --db DB_PATH --queue NAME --payload JSON --idempotency-key KEY --now ISO_TIMESTAMP。成功输出 JSON，错误 JSON 写 stderr 并含 error_code。doctor 语法固定为 python -m queuecraft doctor --db DB_PATH。'
        file.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in turns),encoding='utf-8')
    rubric=control/'probe_rubric.json';data=json.loads(rubric.read_text(encoding='utf-8'))
    for row in data['queuecraft']:
        if row['turn']==30:row['turn']=29
    rubric.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    grader=destination/'grader_only/continuous_memory'
    ledger=grader/'test_ledgerflow_contract.py';text=ledger.read_text(encoding='utf-8')
    start=text.index('def test_dry_run_and_preview_do_not_write(');end=text.index('def test_totals_preserve_sign_and_pagination(',start)
    text=text[:start]+'''def test_dry_run_and_preview_do_not_write(tmp_path):
    db=tmp_path/'ledger.db'; f=tmp_path/'x.csv'
    f.write_text('account_id,external_id,occurred_at,amount,description\\na,x,2026-11-03T10:00:00Z,1.00,x\\n',encoding='utf-8')
    import_csv(f,db,dry_run=True)
    assert not db.exists(), 'dry-run must not create a disk database'

'''+text[end:]
    ledger.write_text(text,encoding='utf-8')
    for name in ['stage_checks.py','hardening_checks.py']:
        shutil.copy2(Path(__file__).with_name(name),grader/name)
    matrix=control/'acceptance_matrix.yaml'
    if matrix.exists():
        matrix.write_text(matrix.read_text(encoding='utf-8').replace('version: 1','version: 2',1)+
            '\ncalibration:\n  status: BLOCKED_REFERENCE_OR_BUDGET_CALIBRATION\n  requires_complete_reference: true\n  requires_sequential_no_future_turn_usage: true\n',encoding='utf-8')
    (destination/'V2_CHANGES.zh-CN.md').write_text(
        '这是新的 DEV 任务版本，不能与 V1 做严格配对或回填旧成绩。API 从 T1 提供；后续行为纠正仍按原轮次发生。\n'
        'Queue 生命周期召回为 T29，T30 是迁移需求。dry-run 不再被评分器要求创建磁盘数据库。\n'
        'CLI 语法在对应公开轮次明确后才评分。T37 前只运行 stage_checks.py，不执行未来要求的终态检查。\n'
        '逐轮分别记录 delivered / handed_off / stage passed；未到达召回为 N/A。完整参考通过不等于预算校准通过。\n'
        '正式长程 Memory 指标仍需完整参考与无未来题可见的 37 轮执行 Usage 正对照；本次未发起模型调用。\n',encoding='utf-8')
    # Do not silently relabel an uncalibrated reference as a positive control.
    manifest={'protocol_version':2,'kind':'NEW_DEV_TASK_VERSION_NOT_COMPARABLE_TO_V1',
              'source_pack':str(source),'calibration_status':'BLOCKED_REFERENCE_OR_BUDGET_CALIBRATION',
              'changes':['API available at T1; corrections remain sequential','Queue recall T29 / migration T30 aligned',
                         'dry-run no disk writes clarified; grader no longer requires schema creation',
                         'CLI grammar published before matching checks','stage handoff and terminal/recall scores separated'],
              'paid_agent_runs':0,'files':{}}
    for p in sorted(destination.rglob('*')):
        if p.is_file():manifest['files'][p.relative_to(destination).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    (destination/'protocol-v2.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();upgrade(args.source,args.output)
