"""Structural command counts for future reports; never rewrites old results."""
from repo_code.commands import git_inspections
from repo_code.cost_metrics import trial_cost


def usage_metrics(run, attempts):
    """Keep prediction eligibility independent from unsettled attempt costs."""
    row={'cost':run.get('budget_used',{}),'usage':{'input_tokens':run.get('input_tokens'),
         'output_tokens':run.get('output_tokens')},'wall_seconds':run.get('agent_seconds')}
    return {'statistics_version':3, 'prediction_scoreable':run.get('score_eligible',False),
            **trial_cost(row,attempts)}


def closure_metrics(responses):
    requests=[];writes=[];commands=0
    for number,response in responses:
        for call in response.get('tool_calls',[]):
            if call['name'] in {'edit','create','patch','delete','move'}:
                writes.append({'call':number,'tool':call['name'],'path':call.get('arguments',{}).get('path')})
            if call['name']!='process.run':continue
            args=call.get('arguments',{})
            actions=[a for a in git_inspections(args.get('argv',[])) if a in {'diff','status'}]
            if actions:
                commands+=len(actions)
                requests.append({'call':number,'arguments':args,'commands':actions})
    return {'explicit_write_requests':writes,'last_explicit_write_call':writes[-1]['call'] if writes else None,
            'git_diff_status_requests':requests,'git_diff_status_request_count':len(requests),
            'git_diff_status_command_count':commands,'git_diff_status_api_call_count':len({r['call'] for r in requests})}


if __name__=='__main__':
    import argparse,json
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Create separate corrected metrics without modifying historical results')
    parser.add_argument('--root',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    root=Path(args.root);target=Path(args.output)
    if target.exists():raise SystemExit('Output already exists; historical metrics are immutable')
    if not (root/'tasks').is_dir():raise SystemExit('Missing tasks directory')
    cases=[]
    for folder in sorted((root/'tasks').rglob('responses')):
        if not folder.is_dir():continue
        rows=[(int(p.stem),json.loads(p.read_text(encoding='utf-8'))) for p in sorted(folder.glob('*.json')) if p.stem.isdigit()]
        cases.append({'case':folder.parent.relative_to(root).as_posix(),**closure_metrics(rows)})
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps({'source':str(root),'statistics_version':2,'scores_unchanged':True,'cases':cases},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
