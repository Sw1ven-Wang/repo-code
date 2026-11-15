"""Separate measured costs; unknown usage is never a zero-cost observation."""

FIELDS = ('model_attempts', 'successful_responses', 'tool_calls', 'input_tokens',
          'output_tokens', 'cache_tokens', 'unknown_usage', 'wall_seconds',
          'auxiliary_calls', 'index_seconds', 'reranker_seconds')


def trial_cost(trial, records=None):
    usage = trial.get('usage') or {}
    cost = trial.get('cost') or {}
    values = {k: None for k in FIELDS}
    values.update(model_attempts=cost.get('model_calls'), tool_calls=cost.get('tool_calls'),
                  wall_seconds=trial.get('wall_seconds'), actual_cost=None)
    for key in ('input_tokens', 'output_tokens', 'cache_tokens'):
        value = usage.get(key)
        values[key] = value if type(value) is int and value >= 0 else None
    if values['cache_tokens'] is None:
        cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', usage.get('cached_tokens'))
        if type(cached) is int and cached >= 0: values['cache_tokens'] = cached
    values['unknown_usage'] = int(not all(values[k] is not None for k in ('input_tokens','output_tokens')))
    if records is not None:
        values.update(model_attempts=len(records),
            successful_responses=sum(r['status']=='COMPLETED' for r in records),
            auxiliary_calls=sum(r.get('purpose')=='auxiliary' for r in records),
            unknown_usage=sum(r['status'] not in {'NOT_DISPATCHED'} and 'settled_tokens' not in r for r in records))
        for key in ('input_tokens','output_tokens'):
            values[key] = sum((r.get('usage') or {}).get(key,0) for r in records)
        caches=[((r.get('usage') or {}).get('input_tokens_details') or {}).get('cached_tokens')
                for r in records if r['status']=='COMPLETED']
        if caches and all(type(v) is int and v>=0 for v in caches): values['cache_tokens']=sum(caches)
        values['unknown_reservation'] = sum(r.get('reservation',0) for r in records
                                           if r['status']!='NOT_DISPATCHED' and 'settled_tokens' not in r)
    memory = trial.get('memory_cost') or {}
    for key in ('index_seconds','reranker_seconds'):
        if key in memory: values[key] = memory[key]
    values['cost_settled'] = values['unknown_usage']==0
    return values


def aggregate_cost(trials):
    rows = [t.get('cost_vector') or trial_cost(t) for t in trials]
    result = {}
    for field in FIELDS:
        vals = [r.get(field) for r in rows]
        result[field] = sum(vals) if all(v is not None for v in vals) else None
    result.update(actual_cost=None, cost_settled=all(r['cost_settled'] for r in rows))
    return result


def compare_cost(old, new, ratio):
    """Require per-dimension resource bounds; do not sum calls and tools."""
    dimensions = ['model_attempts','tool_calls']
    if all(old.get(k) is not None and new.get(k) is not None for k in ('input_tokens','output_tokens')):
        dimensions += ['input_tokens','output_tokens']
    comparable = all(old.get(k) is not None and new.get(k) is not None for k in dimensions)
    bounded = comparable and all(new[k] <= max(old[k],1)*ratio for k in dimensions)
    # Wall-clock jitter alone is never enough to claim an efficiency gain.
    improved = comparable and all(new[k] <= old[k] for k in dimensions) and any(new[k] < old[k] for k in dimensions)
    return {'bounded':bool(bounded),'improved':bool(improved),'dimensions':dimensions,
            'cost_settled':old['cost_settled'] and new['cost_settled']}
