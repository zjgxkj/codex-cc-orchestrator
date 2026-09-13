import json

import pytest

from codex_claude_agent_mcp.compact import compact_payload, compact_error, EXEC_OUTPUT_FORMAT, REVIEW_OUTPUT_FORMAT
from codex_claude_agent_mcp.models import ExecuteTaskResult, ReviewTaskResult, JobStatusResult
from codex_claude_agent_mcp.server import _execute_task_core, _get_job_status_core
from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner


def test_short_payload_values_unchanged():
    payload = dict(status='COMPLETED',summary='完成✓',files_changed=['src/a.py'],validation=['passed'])
    result = compact_payload(payload, 'execution')
    assert all(result[k] == v for k,v in payload.items())
    assert result['output_truncated'] is False and result['omitted'] == {}
    assert 'output_truncated' not in payload


@pytest.mark.parametrize('kind,field,count,length', [('execution','files_changed',100,1024),
    ('execution','validation',20,500),('review','evidence',30,800),('review','unmet_criteria',100,1000)])
def test_huge_unicode_arrays_and_single_item(kind, field, count, length):
    payload = dict(summary='错🧪'*4000, **{field:['失败🧪'*(length+1)]*(count+7)})
    result = compact_payload(payload, kind)
    assert len(result['summary']) == 3000 and len(result[field]) == count
    assert all(len(item) == length for item in result[field])
    assert result['omitted'][field] == 7
    assert result['omitted'][field+'_chars'] == (3*(length+1)-length)*count
    assert result['omitted']['summary_chars'] == 5000
    assert compact_payload(result, kind) == result
    json.dumps(result,ensure_ascii=False).encode('utf-8')


def test_failure_priority_keeps_order_and_verdict():
    result = ReviewTaskResult(job_id='r',status='FAIL',review_completed=True,
        summary='BLOCKER: contract mismatch\n'+'ok '*2000,
        unmet_criteria=['unmet critical acceptance']+['missing']*120,
        evidence=['x.py:12 critical failure']+['passed']*50)
    assert result.status == 'FAIL' and result.summary.startswith('BLOCKER:')
    assert result.unmet_criteria[0] == 'unmet critical acceptance'
    assert result.evidence[0] == 'x.py:12 critical failure'
    assert result.output_truncated


def test_error_caps_and_metadata_reach_top_level():
    error = dict(code='INTERNAL_ERROR',retryable=False,message='E'*9000,
                 details={'log':'raw'*20000, 'nested':{'x':{'y':['x']*1000}}})
    result = ExecuteTaskResult(job_id='e',error=error)
    assert len(result.error.message) == 2000
    assert len(result.error.details['log']) == 500
    assert result.output_truncated and result.omitted['error'] == 1
    assert compact_error(result.error.model_dump()) == result.error.model_dump()
    assert len(result.model_dump_json()) < 7000


def test_sdk_schemas_are_constrained():
    for fmt in (EXEC_OUTPUT_FORMAT, REVIEW_OUTPUT_FORMAT):
        props=fmt['schema']['properties']
        assert props['summary']['maxLength'] == 3000
        for value in props.values():
            if value['type'] == 'array':
                assert value['maxItems'] <= 100 and value['items']['maxLength'] <= 1024


async def test_server_persistence_and_status_are_bounded(app, isolated_cwd):
    class Huge(FakeClaudeRunner):
        async def execute(self, **kwargs):
            result=await super().execute(**kwargs)
            result.summary='x'*8000
            result.validation=['failure: '+'z'*2000]*30
            return result
    app.runner=Huge()
    result=await _execute_task_core(app,'huge','t',isolated_cwd,[],None,None,30)
    stored=await app.store.get_job('huge')
    status=await _get_job_status_core(app,'huge',isolated_cwd)
    assert len(result.summary) == len(stored['execution_summary']) == 3000
    assert status.execution_result.output_truncated
    assert len(status.execution_result.validation) == 20


def test_legacy_status_is_capped_explicitly():
    status=JobStatusResult(job_id='legacy',execution_summary='x'*9000)
    assert len(status.execution_summary) == 3000
    assert status.output_truncated and status.omitted['execution_summary_chars'] == 6000


@pytest.mark.parametrize('fallback',[False,True])
async def test_real_sdk_structured_and_fallback_cannot_bypass_server_cap(app, isolated_cwd, monkeypatch, fallback):
    from types import SimpleNamespace
    from codex_claude_agent_mcp.claude_runner import RealClaudeRunner
    runner=RealClaudeRunner()
    app.runner=runner
    payload={'status':'BLOCKED','summary':'关键阻塞'*2000,
             'files_changed':['a.py']*101,'validation':['failure: '+'X'*1000]*25}
    async def query(*args, **kwargs):
        sid=kwargs['expected_session_id']
        msg=SimpleNamespace(session_id=sid,is_error=False,
            structured_output=None if fallback else payload,
            result=json.dumps(payload,ensure_ascii=False) if fallback else None)
        return msg,'',sid,None
    monkeypatch.setattr(runner,'_run_query',query)
    result=await _execute_task_core(app,'sdk-cap','t',isolated_cwd,[],None,None,30)
    assert result.status=='BLOCKED' and result.execution_completed
    assert len(result.summary)==3000 and len(result.files_changed)==100
    assert len(result.validation)==20 and len(result.validation[0])==500
    assert result.omitted['files_changed']==1 and result.omitted['validation']==5
    assert result.output_truncated
