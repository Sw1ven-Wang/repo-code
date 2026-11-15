"""Run one frozen official Harbor task; scores only come from its verifier."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--task',type=Path,required=True)
    p.add_argument('--jobs-dir',type=Path,required=True)
    p.add_argument('--job-name',required=True)
    p.add_argument('--phase',choices=['oracle','nop','agent','regrade'],required=True)
    p.add_argument('--benchmark',choices=['swe-pro-v2','e2e-swe','terminal-bench-2'],default='swe-pro-v2')
    p.add_argument('--source-job',type=Path)
    p.add_argument('--official-tooling',type=Path)
    p.add_argument('--i-understand-api-costs',action='store_true')
    args=p.parse_args()
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import TaskConfig,EnvironmentConfig,AgentConfig
    if args.phase=='agent':
        if not args.i_understand_api_costs:p.error('Real agent requires explicit API-cost authorization')
        if not os.environ.get('DEEPSEEK_API_KEY_REPO'):p.error('DEEPSEEK_API_KEY_REPO is missing')
        agent=AgentConfig(import_path='harbor_repo_code.agent:RepoCodeHarborAgent',model_name='deepseek/deepseek-flash',
            kwargs={'max_calls':30,'max_tools':150,'wall_seconds':3000,'token_limit':1200000,
                'output_tokens':8192,'thinking':'enabled','benchmark':args.benchmark})
    elif args.phase=='regrade':
        if not args.source_job or not args.official_tooling:p.error('Regrade requires source job and unchanged official tooling')
        agent=AgentConfig(import_path='patch_replay:PatchReplayAgent',model_name='replay',
            kwargs={'source_job':str(args.source_job.resolve())})
    else:agent=AgentConfig(name=args.phase)
    cfg=JobConfig(job_name=args.job_name,jobs_dir=args.jobs_dir.resolve(),
        tasks=[TaskConfig(path=args.task.resolve())],agents=[agent],n_concurrent_trials=1,
        environment=EnvironmentConfig(import_path='harbor_repo_code.environment:DockerPhaseEnvironment'))
    args.jobs_dir.mkdir(parents=True,exist_ok=True)
    config=args.jobs_dir/(args.job_name+'.config.json')
    if config.exists() or (args.jobs_dir/args.job_name).exists():p.error('Use a fresh job name; previous attempts are retained')
    config.write_text(cfg.model_dump_json(indent=2),encoding='utf-8')
    env=os.environ.copy();env['LITELLM_LOCAL_MODEL_COST_MAP']='true'
    if args.official_tooling:
        env['PYTHONPATH']=str(args.official_tooling.resolve())+os.pathsep+env.get('PYTHONPATH','')
    result=subprocess.run([sys.executable,'-m','harbor.cli.main','run','--config',str(config)],env=env)
    return result.returncode


if __name__=='__main__':raise SystemExit(main())
