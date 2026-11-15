"""Model-visible LF snippets must edit Windows CRLF files without changing EOLs."""
import pytest

from repo_code.storage import Store
from repo_code.util import RepoError,digest
from repo_code.workspace import Workspace,Mutator


def mutator(tmp_path,raw):
    source=tmp_path/'source';source.mkdir()
    (source/'main.py').write_bytes(raw)
    return Mutator(Workspace(source,Store(tmp_path/'state')),'newline-test'),source/'main.py'


@pytest.mark.parametrize('bom',[b'',b'\xef\xbb\xbf'])
def test_lf_snippet_edits_crlf_file_preserving_eol_and_bom(tmp_path,bom):
    raw=bom+'def answer():\r\n    return 0\r\n# 中文\r\n'.encode()
    m,path=mutator(tmp_path,raw)
    m.apply('edit-newline',[{'kind':'edit','path':'main.py','expected_version':digest(raw),
        'old_text':'def answer():\n    return 0\n','new_text':'def answer():\n    return 42\n'}])
    assert path.read_bytes()==raw.replace(b'return 0',b'return 42')


def test_normalized_match_still_requires_uniqueness(tmp_path):
    raw=b'x\r\nx\r\n';m,path=mutator(tmp_path,raw)
    with pytest.raises(RepoError,match='AMBIGUOUS_OR_MISSING_MATCH'):
        m.plan([{'kind':'edit','path':'main.py','expected_version':digest(raw),'old_text':'x\n','new_text':'y\n'}])
    assert path.read_bytes()==raw


def test_crlf_normalization_does_not_bypass_stale_version(tmp_path):
    raw=b'x\r\n';m,path=mutator(tmp_path,raw)
    with pytest.raises(RepoError) as exc:
        m.plan([{'kind':'edit','path':'main.py','expected_version':digest(b'old'),'old_text':'x\n','new_text':'y\n'}])
    assert exc.value.code=='STALE_VERSION'
    assert path.read_bytes()==raw


def test_mixed_newlines_are_not_silently_converted(tmp_path):
    raw=b'x\r\ny\n';m,path=mutator(tmp_path,raw)
    m.apply('edit-mixed',[{'kind':'edit','path':'main.py','expected_version':digest(raw),'old_text':'y\n','new_text':'z\n'}])
    assert path.read_bytes()==b'x\r\nz\n'
