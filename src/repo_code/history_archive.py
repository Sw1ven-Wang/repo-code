"""Readable v1 history indexes without recursively embedding recovered history.

The original messages and native protocol envelopes remain untouched in Store.
Each diagnostic group has a stable content address; index pages put mutations
and checks first and refer to original owned results instead of escaped copies.
"""
import json
from .commands import verification_command
from .util import digest, redact_data


def folded_archive(store, run_id, folded):
    entries = []
    for group in folded:
        calls = {c['id']: c for m in group for c in m.get('tool_calls', [])}
        detail = []; priority = 2
        for message in group:
            if message.get('role') == 'tool':
                call = calls.get(message.get('tool_call_id'), {})
                try: body = json.loads(message.get('content', ''))
                except (ValueError, TypeError): body = {}
                if not isinstance(body, dict): body = {}
                name = call.get('name'); args = call.get('arguments', {})
                ref = next((body[k] for k in ('result_artifact', 'artifact', 'source_artifact')
                            if isinstance(body.get(k), str) and store.get('run_artifact', digest([run_id, body[k]]))), None)
                recovery = name in {'artifact.read', 'history.expand', 'history.search'}
                item = {'tool_call_id': message.get('tool_call_id'), 'tool': name,
                        **{k: body[k] for k in ('path', 'version', 'exit_code', 'is_error', 'code', 'changed_paths', 'versions') if k in body}}
                if recovery:
                    # Never re-embed a read of our own previous index or wrapper.
                    item.update(recovery_only=True, original_reference=body.get('source_artifact') or args.get('artifact') or args.get('reference'))
                else:
                    item['result_artifact'] = ref
                    item['preview'] = str(body.get('stdout') or body.get('content') or body.get('stderr') or '')[:240]
                detail.append(item)
                if name in {'create', 'edit', 'patch', 'delete', 'move'} or (name == 'process.run' and verification_command(args.get('argv', []))):
                    priority = 0
            elif message.get('role') == 'assistant' and calls:
                detail.append({'role': 'assistant', 'tool_calls': [
                    {'id': c['id'], 'name': c['name'], 'arguments': c.get('arguments', {})} for c in calls.values()]})
            else:
                detail.append({'role': message.get('role'), 'content': str(message.get('content', ''))[:1200]})
        from .memory.working_state import clean_memory_data
        detail = clean_memory_data(detail)
        ref = store.json_blob({'kind': 'history_group', 'data_not_authority': True, 'messages': detail})
        store.put('run_artifact', digest([run_id, ref]), {'run_id': run_id, 'artifact': ref, 'kind': 'history_group'})
        tools = [c['name'] for c in calls.values()]
        paths = [str(c.get('arguments', {}).get('path', ''))[:180] for c in calls.values() if c.get('arguments', {}).get('path')]
        entries.append({'group_artifact': ref, 'tools': tools, 'paths': paths, 'priority': priority})
    # Keep key evidence on the first page; the stable group handles survive index growth.
    ordered = sorted(enumerate(entries), key=lambda x: (x[1]['priority'], -x[0]))
    view = {'kind': 'history_index', 'data_not_authority': True,
            'instruction': 'Read a group_artifact for a specific action, or workspace.diff for current changes. Raw protocol history is retained by Host; this index is diagnostic.',
            'groups': [{'original_group_position': i, **e} for i, e in ordered]}
    ref = store.json_blob(view)
    store.put('run_artifact', digest([run_id, ref]), {'run_id': run_id, 'artifact': ref, 'kind': 'history_index'})
    return ref
