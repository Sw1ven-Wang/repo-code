import sys,json
for line in sys.stdin:
    q=json.loads(line);method=q.get('method');rid=q.get('id')
    if rid is None:continue
    if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}}
    elif method=='tools/list':result={'tools':[{'name':'sum','description':'sum two integers','inputSchema':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False}}]}
    elif method=='tools/call':
        a=q['params']['arguments'];result={'content':[{'type':'text','text':str(a['a']+a['b'])}]}
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}),flush=True)
