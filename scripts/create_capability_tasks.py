"""Curate fixed public tasks and Host-only oracles before any live model runs."""
import json
from pathlib import Path

from repo_code.acceptance import freeze_contract
from repo_code.models import Contract,Requirement,Check

ROOT=Path(__file__).resolve().parents[1]


def case(id,args,expected,**kwargs):return {'id':id,'args':args,'expected':expected,**kwargs}


def cli_check(id,script):
    return Check(id=id,kind='cli',definition={'cases':[{'id':'external','argv':['python','-c',script],'stdout':'OK\n'}]},timeout=30)


def task(id,category,goal,files,checks,reference,protected=(),allowed=('**',)):
    checks=[*checks,Check(id='syntax',kind='static_python')]
    contract=freeze_contract(Contract(requirements=[Requirement(id='business',description=goal)],
        checks=checks,required_checks=[c.id for c in checks],coverage={'business':[c.id for c in checks if c.id!='syntax']},
        global_required_checks=['syntax'],protected_paths=list(protected),allowed_paths=list(allowed),
        interface={'environment':'Python 3.13, standard library and pytest installed; network disabled. Use python -m pytest -q -p no:cacheprovider.'}),
        'capability-suite-curator')
    return {'id':id,'category':category,'goal':goal,'files':files,'contract':contract.model_dump(mode='json'),
            'budget':{'model_calls':18,'tool_calls':70,'wall_seconds':900,'child_calls':0}},reference


def build():
    tasks=[];references={}
    def add(pair):
        t,r=pair;tasks.append(t);references[t['id']]=r

    # Empty workspace: package, public function, CLI, tests and README.
    goal=('从空目录创建可运行的标准库 Python 项目 parcelquote。parcelquote/__init__.py 导出 quote(weight_grams, zone, express=False)，'
          '返回整数分：local 首500克500分、每额外不足或满500克加200分；remote 首500克900分、每额外500克加400分；express 额外加300分。'
          '重量必须为正整数且不能是bool，zone仅local/remote；无效重量/区域抛ValueError。提供 python -m parcelquote WEIGHT ZONE [--express]，'
          '成功输出含唯一键cents的JSON；无效参数退出码2。编写并实际运行pytest覆盖边界、异常和CLI，写README使用说明。最后请求外部验收。')
    quote='''def quote(weight_grams, zone, express=False):
    if type(weight_grams) is not int or weight_grams<=0 or zone not in {'local','remote'}:raise ValueError('invalid parcel')
    base,step=(500,200) if zone=='local' else (900,400)
    return base+((weight_grams-1)//500)*step+(300 if express else 0)
'''
    quotecli='''import argparse,json
from . import quote
p=argparse.ArgumentParser();p.add_argument('weight',type=int);p.add_argument('zone',choices=['local','remote']);p.add_argument('--express',action='store_true')
a=p.parse_args()
try:value=quote(a.weight,a.zone,a.express)
except ValueError as e:p.error(str(e))
print(json.dumps({'cents':value}))
'''
    script="""import subprocess,sys,json,pathlib
for args,wanted,code in [(['501','local','--express'],{'cents':1000},0),(['1001','remote'],{'cents':1700},0),(['0','local'],None,2),(['5','mars'],None,2)]:
 r=subprocess.run([sys.executable,'-m','parcelquote',*args],capture_output=True,text=True);assert r.returncode==code,(args,r.stderr)
 if code==0:assert json.loads(r.stdout)==wanted
from parcelquote import quote
for weight,zone in [(True,'local'),(-1,'remote'),(1.5,'local'),(2,'mars')]:
 try:quote(weight,zone)
 except ValueError:pass
 else:raise AssertionError('invalid input accepted')
assert pathlib.Path('README.md').is_file()
assert any(pathlib.Path('.').glob('test*.py')) or any(pathlib.Path('tests').glob('test*.py'))
print('OK')"""
    add(task('greenfield-parcel-cli','code_development',goal,{},[
        Check(id='rates',kind='function',definition={'module':'parcelquote','function':'quote','cases':[
            case('minimum',[1,'local'],500),case('boundary',[500,'remote'],900),case('increment',[501,'remote'],1300),
            case('express',[1000,'local',True],1000),case('large',[2501,'local'],1500)]}),cli_check('cli-and-layout',script)],
        {'parcelquote/__init__.py':quote,'parcelquote/__main__.py':quotecli,'README.md':'# Parcelquote\nPython standard-library parcel quote CLI.\n','tests/test_quote.py':'from parcelquote import quote\ndef test_minimum():assert quote(1,"local")==500\n'}))

    # Existing multi-file project: preserve API while extracting pricing logic.
    goal=('改造购物车项目：保留 checkout.total(items, discount_percent=0) 公共入口，将单行价格计算提取到 pricing.py 的 line_total(item)，'
          '每行item含整数cents、quantity；非负整数但不能bool，否则ValueError。先对所有行求和，再用整数向下取整应用0到100的整数百分比折扣；'
          '非法折扣抛ValueError。空购物车返回0；计算不能修改输入items。保留现有公开测试且不得编辑 tests/test_existing.py，'
          '补充多文件单元测试、实际运行pytest，更新README。')
    existing='''from checkout import total
def test_basic():assert total([{'cents':100,'quantity':2}])==200
def test_empty():assert total([])==0
'''
    pricing='''def line_total(item):
    values=[item['cents'],item['quantity']]
    if any(type(v) is not int or v<0 for v in values):raise ValueError('invalid item')
    return values[0]*values[1]
'''
    checkout='''from pricing import line_total
def total(items,discount_percent=0):
    if type(discount_percent) is not int or not 0<=discount_percent<=100:raise ValueError('invalid discount')
    return sum(line_total(x) for x in items)*(100-discount_percent)//100
'''
    script="""import copy,pathlib,ast
from checkout import total
from pricing import line_total
items=[{'cents':1,'quantity':1},{'cents':1,'quantity':1}];before=copy.deepcopy(items)
assert total(items,50)==1 and items==before
for fn,args in [(total,([],True)),(total,([],-1)),(total,([],101)),(line_total,({'cents':True,'quantity':2},)),(line_total,({'cents':1,'quantity':-1},))]:
 try:fn(*args)
 except ValueError:pass
 else:raise AssertionError('invalid input')
tree=ast.parse(pathlib.Path('checkout.py').read_text());assert any(isinstance(n,ast.Call) and ((isinstance(n.func,ast.Name) and n.func.id=='line_total') or (isinstance(n.func,ast.Attribute) and n.func.attr=='line_total')) for n in ast.walk(tree))
assert pathlib.Path('README.md').exists();print('OK')"""
    add(task('multi-file-cart-refactor','code_development',goal,
        {'checkout.py':"def total(items,discount_percent=0):\n    return sum(x['cents']*x['quantity'] for x in items)\n",'tests/test_existing.py':existing,'README.md':'# Cart\nBasic item totals.\n'},[
        Check(id='totals',kind='function',definition={'module':'checkout','function':'total','cases':[
            case('empty',[[]],0),case('discount',[[{'cents':199,'quantity':3}],10],537),case('full',[[{'cents':123,'quantity':2}],100],0),
            case('aggregate-rounding',[[{'cents':1,'quantity':1},{'cents':1,'quantity':1}],50],1)]}),cli_check('boundaries-and-refactor',script)],
        {'checkout.py':checkout,'pricing.py':pricing},protected=['tests/test_existing.py']))

    # Deterministic clock: reproduce real failing pytest output before editing.
    cachetests='''import pytest
from cache import TTLCache
def test_missing_default():assert TTLCache(5,lambda:0).get('absent','fallback')=='fallback'
def test_exact_expiry():
 now=[0];c=TTLCache(5,lambda:now[0]);c.set('a',1);now[0]=5;assert c.get('a') is None
def test_purge_and_length():
 now=[0];c=TTLCache(1,lambda:now[0]);c.set('a',1);c.set('b',2);now[0]=1;assert c.purge()==2;assert len(c)==0
def test_length_purges_expired():
 now=[0];c=TTLCache(1,lambda:now[0]);c.set('a',1);now[0]=2;assert len(c)==0
def test_overwrite_resets_deadline():
 now=[0];c=TTLCache(2,lambda:now[0]);c.set('a',1);now[0]=1;c.set('a',2);now[0]=2;assert c.get('a')==2
def test_zero_ttl():
 c=TTLCache(0,lambda:0);c.set('a',1);assert c.get('a','expired')=='expired'
def test_negative_ttl():
 with pytest.raises(ValueError):TTLCache(-1,lambda:0)
'''
    broken='''class TTLCache:
 def __init__(self,ttl,clock):
  if ttl<0:raise ValueError('ttl')
  self.ttl,self.clock,self.data=ttl,clock,{}
 def set(self,key,value):self.data[key]=(value,self.clock()+self.ttl)
 def get(self,key,default=None):
  if key not in self.data:return None
  value,deadline=self.data[key]
  if self.clock()>deadline:del self.data[key];return default
  return value
 def purge(self):
  removed=0
  for key,(_,deadline) in self.data.items():
   if self.clock()>deadline:del self.data[key];removed+=1
  return removed
 def __len__(self):return len(self.data)
'''
    fixed='''class TTLCache:
 def __init__(self,ttl,clock):
  if ttl<0:raise ValueError('ttl')
  self.ttl,self.clock,self.data=ttl,clock,{}
 def set(self,key,value):self.data[key]=(value,self.clock()+self.ttl)
 def get(self,key,default=None):
  if key not in self.data:return default
  value,deadline=self.data[key]
  if self.clock()>=deadline:del self.data[key];return default
  return value
 def purge(self):
  keys=[k for k,(_,deadline) in self.data.items() if self.clock()>=deadline]
  for key in keys:del self.data[key]
  return len(keys)
 def __len__(self):self.purge();return len(self.data)
'''
    goal=('先实际运行 python -m pytest -q -p no:cacheprovider，记录TTL缓存的失败，再修复cache.py；不要改tests/test_cache.py。'
          'TTLCache(ttl,clock)在now>=deadline时过期，缺失/过期返回get的default；set同一key重置过期时间；purge删除所有过期项并返回数量，'
          'len只计未过期项；ttl=0立即过期，负ttl抛ValueError。修复后重新运行原始pytest，补充必要回归并写简短BUGFIX.md说明原因。')
    script="""import subprocess,sys,pathlib
r=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider'],capture_output=True,text=True)
assert r.returncode==0,r.stdout+r.stderr
assert pathlib.Path('BUGFIX.md').is_file()
from cache import TTLCache
now=[10];c=TTLCache(2,lambda:now[0]);c.set('x',None);now[0]=12
assert c.get('x','miss')=='miss' and len(c)==0 and c.purge()==0
print('OK')"""
    add(task('pytest-ttl-debug','testing_debugging',goal,{'cache.py':broken,'tests/test_cache.py':cachetests},
        [cli_check('pytest-and-boundaries',script)],{'cache.py':fixed,'BUGFIX.md':'# Bug fixes\nExpiry boundary, defaults, safe deletion, active length.\n'},protected=['tests/test_cache.py']))

    # Documentation is verified against immutable runnable source, then reviewed by a human-facing reader.
    docapp='''import argparse,csv,json
from pathlib import Path
def main():
 p=argparse.ArgumentParser(description='Summarize sales rows. Python 3.11+, standard library only.')
 p.add_argument('--input',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 try:
  with open(a.input,encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
  value={'rows':len(rows),'total_cents':sum(int(r['cents']) for r in rows)}
  Path(a.output).write_text(json.dumps(value,sort_keys=True)+'\\n',encoding='utf-8')
 except (OSError,ValueError,KeyError) as e:p.error(str(e))
 print('processed',len(rows))
if __name__=='__main__':main()
'''
    doctests="import subprocess,sys\ndef test_help():assert subprocess.run([sys.executable,'sales_summary.py','--help'],capture_output=True).returncode==0\n"
    goal=('阅读项目sales_summary.py、示例CSV和现有README，修订README.md并新增docs/usage.md，使用中文说明。只允许修改这两个Markdown文件。'
          '准确说明Python 3.11+、仅标准库、无需联网安装依赖、--input/--output参数、输出JSON的rows和total_cents、缺失文件/非法cents退出码2。'
          'README链接docs/usage.md；移除旧的pandas、--csv、HTTP服务和端口说明。docs/usage.md提供两个独立的bash代码块，'
          '第一个仅一行 python sales_summary.py --help，第二个仅一行 python sales_summary.py --input examples/sales.csv --output /tmp/repo-code-sales.json。'
          '实际运行这两条命令以及现有pytest核对文档，解释示例输出rows=3,total_cents=325；不要改源码、测试和示例数据。')
    docscript=r"""import pathlib,re,shlex,subprocess,sys,json
readme=pathlib.Path('README.md').read_text(encoding='utf-8');usage=pathlib.Path('docs/usage.md').read_text(encoding='utf-8')
assert 'docs/usage.md' in readme
for token in ['3.11','--input','--output','rows','total_cents','325','2']:assert token in readme+usage,token
assert any(ord(c)>127 for c in usage)
for token in ['pandas','--csv','8000','HTTP']:assert token not in readme+usage,token
blocks=re.findall(r'```bash\s*\n(.*?)```',usage,re.S);assert len(blocks)==2
commands=[shlex.split(b.strip()) for b in blocks]
assert commands==[['python','sales_summary.py','--help'],['python','sales_summary.py','--input','examples/sales.csv','--output','/tmp/repo-code-sales.json']]
for command in commands:
 r=subprocess.run(command,capture_output=True,text=True);assert r.returncode==0,r.stderr
assert json.loads(pathlib.Path('/tmp/repo-code-sales.json').read_text())=={'rows':3,'total_cents':325}
print('OK')"""
    gooddoc='''# 销售汇总
Python 3.11+，仅标准库，无需联网安装依赖。输入 --input，输出 --output。
JSON 包含 rows 和 total_cents；缺失文件或非法金额退出码 2。
使用说明见 [文档](docs/usage.md)。示例 rows=3，total_cents=325。
'''
    usage='''# 用法
Python 3.11+，仅标准库，无需安装依赖。
```bash
python sales_summary.py --help
```
```bash
python sales_summary.py --input examples/sales.csv --output /tmp/repo-code-sales.json
```
--input 指定输入，--output 指定输出。JSON rows=3,total_cents=325。
缺失文件或非法 cents 时退出码 2。
'''
    add(task('source-grounded-documentation','documentation',goal,{'sales_summary.py':docapp,'examples/sales.csv':'name,cents\nA,100\nB,250\nC,-25\n','tests/test_help.py':doctests,'README.md':'# Sales server\nInstall pandas, then python sales_summary.py --csv data.csv. HTTP port 8000.\n'},
        [cli_check('documentation-and-runnable-examples',docscript)],{'README.md':gooddoc,'docs/usage.md':usage},
        protected=['sales_summary.py','tests/**','examples/**'],allowed=['README.md','docs/usage.md']))

    # Latest valid revision wins, including cancellations; monetary arithmetic is exact.
    header='order_id,revision,status,currency,amount\n'
    sample=header+'A,1,paid,CNY,10.10\nB,1,paid,USD,2.30\nA,2,paid,CNY,12.20\nB,2,cancelled,USD,0\nC,1,paid,CNY,-2.05\nA,1,paid,CNY,99\nD,1,paid,USD,bad\n'
    dataref='''import csv,io,re,json,argparse
from decimal import Decimal
def summarize(csv_text):
 latest={};invalid=0
 for row in csv.DictReader(io.StringIO(csv_text.lstrip('\\ufeff'))):
  try:
   oid=row['order_id'].strip();rev=row['revision'].strip();status=row['status'].strip();currency=row['currency'].strip();amount=row['amount'].strip()
   if not oid or not rev.isascii() or not rev.isdigit() or int(rev)<1 or status not in {'paid','cancelled'} or currency not in {'CNY','USD'} or not re.fullmatch(r'-?[0-9]+(?:\\.[0-9]{1,2})?',amount):raise ValueError()
   record=(int(rev),status,currency,int(Decimal(amount)*100))
   if oid not in latest or record[0]>=latest[oid][0]:latest[oid]=record
  except (ValueError,KeyError,AttributeError,TypeError):invalid+=1
 totals={};orders=0
 for _,status,currency,cents in latest.values():
  if status=='paid':orders+=1;totals[currency]=totals.get(currency,0)+cents
 return {'orders':orders,'totals_cents':dict(sorted(totals.items())),'invalid_rows':invalid}
def main():
 p=argparse.ArgumentParser();p.add_argument('input');a=p.parse_args()
 with open(a.input,encoding='utf-8-sig',newline='') as f:value=summarize(f.read())
 print(json.dumps(value,sort_keys=True))
if __name__=='__main__':main()
'''
    goal=('实现ledger.py的summarize(csv_text)及CLI python ledger.py INPUT.csv，使用标准库，将订单CSV转成JSON汇总，并处理fixtures/orders.csv，'
          '保存实际结果到summary.json。字段order_id,revision,status,currency,amount均去首尾空格；order_id非空，revision为ASCII正整数，'
          'status仅paid/cancelled，currency仅CNY/USD，amount为可带负号的十进制整数或1到2位小数；非法行跳过并逐行计invalid_rows。'
          '每订单保留最高有效revision，同revision以最后出现行胜出；低revision晚到不覆盖；非法修订不覆盖历史有效记录。'
          '最新cancelled不计订单也不计金额；paid负金额视为退款。返回恰好orders、totals_cents、invalid_rows三键：orders为有效paid订单数，'
          'totals_cents按币种累计整数分，不能混算币种或产生浮点误差，无paid记录的币种不出现。支持UTF-8 BOM和中文订单号。'
          '补充并实际运行pytest，不得修改fixtures/orders.csv。')
    datacases=[case('sample',[sample],{'orders':2,'totals_cents':{'CNY':1015},'invalid_rows':1}),
        case('empty',[header],{'orders':0,'totals_cents':{},'invalid_rows':0}),
        case('tie-stale',[header+'甲,2,paid,USD,0.10\n甲,1,paid,CNY,99\n甲,2,paid,USD,0.20\n'],{'orders':1,'totals_cents':{'USD':20},'invalid_rows':0}),
        case('invalid-revision',[header+'A,1,paid,CNY,3.33\nA,2,paid,CNY,1.234\nB,0,paid,USD,2\nC,1,pending,CNY,5\nD,1,paid,EUR,3\n'],{'orders':1,'totals_cents':{'CNY':333},'invalid_rows':4}),
        case('bom-spaces',['\ufeff'+header+' 甲 , 1 , paid , CNY , 0.10 \n乙,1,paid,CNY,0.20\n丙,1,paid,USD,-0.01\n'],{'orders':3,'totals_cents':{'CNY':30,'USD':-1},'invalid_rows':0}),
        case('cancel',[header+'A,1,paid,CNY,8\nA,3,cancelled,CNY,0\nA,2,paid,CNY,7\n'],{'orders':0,'totals_cents':{},'invalid_rows':0})]
    datascript="""import json,pathlib,subprocess,sys
r=subprocess.run([sys.executable,'ledger.py','fixtures/orders.csv'],capture_output=True,text=True);assert r.returncode==0,r.stderr
wanted={'orders':2,'totals_cents':{'CNY':1015},'invalid_rows':1}
assert json.loads(r.stdout)==wanted
assert json.loads(pathlib.Path('summary.json').read_text())==wanted
print('OK')"""
    add(task('csv-revisions-refunds','data_processing',goal,{'ledger.py':'def summarize(csv_text):\n    raise NotImplementedError\n','fixtures/orders.csv':sample},
        [Check(id='ledger-cases',kind='function',definition={'module':'ledger','function':'summarize','cases':datacases}),cli_check('cli-and-saved-output',datascript)],
        {'ledger.py':dataref,'summary.json':json.dumps(datacases[0]['expected'])},protected=['fixtures/**']))
    return tasks,references


def main():
    tasks,references=build()
    destination=ROOT/'examples'/'capability_tasks.json'
    destination.write_text(json.dumps(tasks,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    private=ROOT/'reports'/'capability-validation'/'oracle-reference-solutions.json'
    private.parent.mkdir(parents=True,exist_ok=True)
    private.write_text(json.dumps(references,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'tasks':len(tasks),'dataset':str(destination)},ensure_ascii=False))


if __name__=='__main__':main()
