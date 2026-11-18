"""Long unsafe histories must not write an archive per reclaimed group."""
from repo_code.context import ContextEngine
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
import repo_code.history_archive as archives


def test_long_fallback_uses_bounded_archive_batches(tmp_path,monkeypatch):
    seed=tmp_path/'seed';seed.mkdir();app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local')
    run=app.new_task('Keep all raw evidence',seed,conversation=True)
    with app.store.tx() as c:
        for i in range(300):app.store.append_message(run['id'],{'role':'assistant','content':f'hypothesis {i} '+'Ω\\" '*400},c=c)
    original=archives.folded_archive;calls=[]
    def counted(*args):calls.append(len(args[-1]));return original(*args)
    monkeypatch.setattr(archives,'folded_archive',counted)
    ContextEngine(app.store,app.runtime).build(run['id'],[],{'max_tokens':24000})
    assert len(calls)<=6,calls
    assert len(app.store.messages(run['id']))==301
    app.close()
