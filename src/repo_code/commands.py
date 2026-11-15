"""Conservative command descriptions, never a shell interpreter or permission check."""
from __future__ import annotations
import ast
import re
import sys
import shlex
from pathlib import PurePosixPath


def command_segments(argv, depth=0):
    if not isinstance(argv, list) or not argv or depth > 3:
        return []
    words = list(argv)
    while words and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]):
        words.pop(0)
    if not words:
        return []
    executable = PurePosixPath(words[0].replace('\\', '/')).name.lower()
    if executable == 'env':
        return command_segments(words[1:], depth + 1)
    if executable in {'bash', 'sh', 'dash', 'zsh'} and len(words) >= 3 and words[1] in {'-c', '-lc', '-ic'}:
        try:
            # shlex's default wordchars does not include every path character.
            lexer = shlex.shlex(words[2], posix=True, punctuation_chars=';&|')
            lexer.whitespace_split = True
            lexer.commenters = ''
            tokens = list(lexer)
        except ValueError:
            return []
        groups = []; group = []
        for word in tokens:
            if word in {';', '&&', '||', '|'}:
                if group: groups.extend(command_segments(group, depth + 1))
                group = []
            else:
                group.append(word)
        if group: groups.extend(command_segments(group, depth + 1))
        return groups
    return [[executable, *words[1:]]]


def git_inspections(argv):
    """Count direct argv and shell forms; quoted 'echo git diff' is not Git."""
    found = []
    for words in command_segments(argv):
        if words[0] != 'git': continue
        i = 1
        while i < len(words) and words[i].startswith('-'):
            i += 2 if words[i] in {'-C', '-c', '--git-dir', '--work-tree'} else 1
        if i < len(words) and words[i] in {'diff', 'status', 'log', 'show', 'rev-parse'}:
            found.append(words[i])
    return found


def inline_python(argv):
    if len(argv) != 3 or argv[1] != '-c' or len(argv[2]) > 16000:
        return None
    if not re.fullmatch(r'(?:python(?:[23](?:\.\d+)?)?|pypy3?)(?:\.exe)?',
                        PurePosixPath(argv[0].replace('\\', '/')).name.lower()):
        return None
    try:
        return ast.parse(argv[2])
    except (SyntaxError, ValueError, RecursionError):
        return None


def canonical_python(argv, stdin=''):
    """Normalize only exact script forms for the existing bound-probe protocol.

    Parsing is not execution evidence. Callers must still bind the command to a
    stored probe, current source/environment and completed execution receipt.
    """
    if not isinstance(argv, list) or not argv:
        return None
    if inline_python(argv) is not None:
        return list(argv) if not stdin else None
    if len(argv) == 2 and argv[1] == '-' and isinstance(stdin, str):
        candidate = [argv[0], '-c', stdin]
        return candidate if inline_python(candidate) is not None else None
    if (len(argv) == 3 and argv[0] in {'bash', '/bin/bash', 'sh', '/bin/sh'}
            and argv[1] in {'-c', '-lc'} and not stdin):
        match = re.fullmatch(r"(python(?:3(?:\.\d+)?)?) - <<'([A-Za-z_][A-Za-z_0-9]*)'\n([\s\S]*)\n\2\n?", argv[2])
        if match:
            if '\n' + match[2] + '\n' in '\n' + match[3] + '\n':
                return None
            candidate = [match[1], '-c', match[3]]
            return candidate if inline_python(candidate) is not None else None
    return None


def inline_observation(argv):
    """Recognize a narrow, replayable Python behavior probe, not general stdout."""
    tree = inline_python(argv)
    if tree is None or not tree.body or any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
        return None
    def print_call(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print'

    printed = [(index, statement.value) for index, statement in enumerate(tree.body)
               if isinstance(statement, ast.Expr) and print_call(statement.value)]
    if not printed or printed[-1][0] != len(tree.body) - 1:
        return None
    if sum(print_call(node) for node in ast.walk(tree)) != len(printed):
        return None
    if any(not call.args or call.keywords or any(isinstance(x, ast.Starred) for x in call.args)
           for _, call in printed):
        return None
    for node in ast.walk(tree):
        names = ([x.name for x in node.names] if isinstance(node, ast.Import)
                 else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
        if any(name.split('.')[0] not in sys.stdlib_module_names for name in names if name):
            return tree, printed
    return None


def independent_assertion_command(argv, stdin, token):
    """Propose a bounded local check; only Broker can execute and attest it.

    Every required assertion must actually finish. This is local candidate
    evidence, never an independently authored acceptance or Release verdict.
    """
    command = canonical_python(argv, stdin)
    tree = inline_python(command) if command else None
    if tree is None or any(isinstance(n,(ast.Try,ast.TryStar)) for n in ast.walk(tree)):
        return None
    assertions = [n for n in ast.walk(tree) if isinstance(n,ast.Assert)]
    if not assertions or any(not any(isinstance(x,ast.Name) and isinstance(x.ctx,ast.Load)
                                     for x in ast.walk(n.test)) for n in assertions):
        return None
    seen = '_host_assertions_' + token
    class Rewrite(ast.NodeTransformer):
        def visit_Assert(self, node):
            index=assertions.index(node)
            fail=ast.If(test=ast.UnaryOp(op=ast.Not(),operand=node.test),
                        body=[ast.Raise(exc=ast.Call(func=ast.Name(id='AssertionError',ctx=ast.Load()),
                                                   args=[node.msg] if node.msg else [],keywords=[]),cause=None)],orelse=[])
            record=ast.Expr(value=ast.Call(func=ast.Attribute(value=ast.Name(id=seen,ctx=ast.Load()),
                                attr='add',ctx=ast.Load()),args=[ast.Constant(index)],keywords=[]))
            return [fail,record]
    tree=Rewrite().visit(tree)
    tree.body.insert(0,ast.Assign(targets=[ast.Name(id=seen,ctx=ast.Store())],
                                 value=ast.Call(func=ast.Name(id='set',ctx=ast.Load()),args=[],keywords=[])))
    # Only a complete set yields the exact marker expected by the Host.
    tail=ast.parse(f"if {seen} == set(range({len(assertions)})):\n print({token!r})")
    tree.body.extend(tail.body)
    return [command[0],'-c',ast.unparse(ast.fix_missing_locations(tree))]


def probe_assertion_argv(probe_argv, assertion_argv):
    """Bind supplied assertions to the result of the original probe expression."""
    observation = inline_observation(probe_argv)
    checks = inline_python(assertion_argv)
    if observation is None or checks is None or not checks.body:
        return None
    probe, printed = observation
    for node in checks.body:
        if (not isinstance(node, ast.Assert)
                or not any(isinstance(x, ast.Name) and x.id == 'observed' and isinstance(x.ctx, ast.Load)
                           for x in ast.walk(node.test))
                or any(isinstance(x, ast.NamedExpr) for x in ast.walk(node.test))):
            return None
        if isinstance(node.test, ast.Compare) and any(
                ast.dump(x) == ast.dump(node.test.left) for x in node.test.comparators):
            return None
    used_names = {n.id for tree in (probe, checks) for n in ast.walk(tree) if isinstance(n, ast.Name)}
    captured = []
    for index, call in printed:
        expression = call.args[0] if len(call.args) == 1 else ast.Tuple(elts=call.args, ctx=ast.Load())
        name = f'_repo_code_probe_{index}'
        while name in used_names:
            name += '_'
        probe.body[index] = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=expression)
        captured.append(ast.Name(id=name, ctx=ast.Load()))
    values = captured[0] if len(captured) == 1 else ast.Tuple(elts=captured, ctx=ast.Load())
    probe.body.append(ast.Assign(targets=[ast.Name(id='observed', ctx=ast.Store())], value=values))
    # Explicit raises keep the checks active even under Python optimization.
    for check in checks.body:
        error = ast.Call(func=ast.Name(id='AssertionError', ctx=ast.Load()),
                         args=[check.msg] if check.msg is not None else [], keywords=[])
        probe.body.append(ast.If(test=ast.UnaryOp(op=ast.Not(), operand=check.test),
                                 body=[ast.Raise(exc=error, cause=None)], orelse=[]))
    return [probe_argv[0], '-c', ast.unparse(ast.fix_missing_locations(probe))]


def verification_command(argv):
    for words in command_segments(argv):
        name, args = words[0], words[1:]
        if name in {'pytest', 'py.test'} and not any(x in args for x in {'--collect-only', '--co', '--help', '--version'}):
            return True
        if name.startswith('python') or name in {'py', 'pypy', 'pypy3'}:
            if args[:1] == ['-m'] and len(args) > 1 and args[1] in {'pytest', 'unittest', 'doctest'}:
                return not any(x in args for x in {'--collect-only', '--co', '--help', '--version'})
            if args[:1] == ['-c'] and len(args) > 1 and re.search(r'\bassert\s', args[1]):
                return True
            if args and re.fullmatch(r'(?:.*/)?test[^/]*\.py', args[0]): return True
        if name in {'npm', 'pnpm', 'yarn'} and args[:1] == ['test']: return True
        if name in {'cargo', 'go', 'dotnet'} and args[:1] == ['test']: return True
        if name in {'mvn', 'mvnw', 'gradle', 'gradlew'} and 'test' in args: return True
    return False


def command_evidence(argv, result, stdin='', *, strict=False, store=None, run=None, operation_id=None, approved_epoch=None):
    """Describe observed work without treating outer shell status as proof."""
    # Only Host-issued bound-probe receipts may use normalized script evidence.
    from .execution_evidence import valid_execution
    bound = valid_execution(store,result,argv,stdin,run=run,operation_id=operation_id,approved_epoch=approved_epoch) if strict else bool(result.get('checked_probe'))
    if bound:
        argv = canonical_python(argv, stdin) or argv
    segments = command_segments(argv)
    if (not segments or result.get('executed') is False or type(result.get('exit_code')) is not int
            or result.get('code') == 'WORKSPACE_WRITE_NO_EFFECT'):
        return None
    if inspection_command(argv) or no_match(argv,result):
        return None
    args = segments[-1]
    verification = verification_command(argv)
    assertion = verification and args[0].startswith(('python','pypy')) and args[1:2] == ['-c']
    if strict and assertion and not bound:
        verification = False
        assertion = False
    kind = ('ASSERTION_CHECK' if assertion else 'TEST_EXECUTION') if verification else 'BEHAVIOR_PROBE'
    scope = None
    if kind == 'TEST_EXECUTION':
        invocation = args[3:] if args[1:2] == ['-m'] else args[1:]
        scope = 'FOCUSED' if any(arg in {'-k','-m','--lf','--last-failed'} or '::' in arg or arg.endswith('.py')
                                 for arg in invocation) else 'AS_INVOKED'
    incomplete = any(result.get(field) for field in ('timeout','cancelled','running'))
    outer_status_only = verification and len(segments) != 1
    changed = result.get('output_snapshot') and result.get('relevant_state') and result['output_snapshot'] != result['relevant_state']
    if incomplete or outer_status_only or (verification and changed):
        outcome = 'UNCERTAIN'
    elif verification:
        outcome = 'PASSED' if result['exit_code'] == 0 else 'FAILED'
    else:
        outcome = 'OBSERVED'
    return {'evidence_kind':kind, 'scope':scope, 'outcome':outcome, 'verification':verification,
            'trusted_binding':bound, 'descriptive_only':not strict,
            'summary':verification_summary(result)}


def inspection_command(argv):
    segments = command_segments(argv)
    return bool(segments) and all(w[0] in {'pwd', 'ls', 'cat', 'head', 'tail', 'sed', 'grep', 'rg', 'find', 'wc', 'git'} for w in segments)


def no_match(argv, result):
    parts = command_segments(argv)
    return (result.get('exit_code') == 1 and not result.get('stderr') and bool(parts)
            and all(w[0] in {'grep', 'rg', 'head', 'tail'} for w in parts)
            and any(w[0] in {'grep', 'rg'} for w in parts))


def verification_summary(result):
    """Keep test counts, not timing/memory noise; this is not acceptance proof."""
    output = str(result.get('stdout', '')) + '\n' + str(result.get('stderr', ''))
    counts = re.findall(r'\b\d+\s+(?:passed|failed|skipped|deselected|errors?|xfailed|xpassed)\b|Ran \d+ tests?\b', output)
    if counts:
        return '; '.join(dict.fromkeys(counts))[:300]
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return ' | '.join(lines[-2:])[-300:]
