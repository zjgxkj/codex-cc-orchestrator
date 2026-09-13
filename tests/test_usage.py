import json
from types import SimpleNamespace

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner, RealClaudeRunner
from codex_claude_agent_mcp.server import _execute_task_core, _continue_task_core, _review_task_core, _get_job_status_core
from codex_claude_agent_mcp.usage import Usage, extract_usage, aggregate_usage


def report(n=1):
    return Usage(input_tokens=n, cache_creation_input_tokens=2*n,
        cache_read_input_tokens=3*n, total_input_tokens=6*n, output_tokens=4*n,
        num_turns=n, duration_ms=10*n, duration_api_ms=8*n)


class UsageRunner(FakeClaudeRunner):
    async def execute(self, **kwargs):
        result = await super().execute(**kwargs)
        result.usage = report()
        return result

    async def continue_session(self, **kwargs):
        result = await super().continue_session(**kwargs)
        result.usage = report(2)
        return result

    async def review(self, **kwargs):
        result = await super().review(**kwargs)
        result.usage = report(3)
        return result


async def test_invocations_survive_continue_and_review(app, isolated_cwd):
    app.runner = UsageRunner()
    result = await _execute_task_core(app, 'usage', 't', isolated_cwd, ['c'], None, None, 30)
    assert result.usage == report()
    for _ in range(2):
        await _continue_task_core(app, 'usage', result.execution_session_id, 'fix', isolated_cwd, None, None, 30)
    review = await _review_task_core(app, 'usage', 't', ['c'], isolated_cwd, None, None, 30)
    assert review.usage == report(3)
    rows = await app.store.get_invocations('usage')
    assert [r['operation'] for r in rows] == ['execute', 'continue', 'continue', 'review']
    assert [r['usage']['input_tokens'] for r in rows] == [1, 2, 2, 3]
    assert len({r['invocation_id'] for r in rows}) == 4
    status = await _get_job_status_core(app, 'usage', isolated_cwd)
    assert status.usage.execution.calls == 3 and status.usage.review.calls == 1
    assert status.usage.total.input_tokens == 8 and status.usage.total.total_input_tokens == 48
    assert status.usage.total.complete
    assert 'invocations' not in status.model_dump()
    assert 'cost' not in status.model_dump_json().lower()
    assert 'price' not in status.model_dump_json().lower()


@pytest.mark.parametrize('value', [None, -1, True, '42', 1.5, float('nan'), 2**70])
def test_missing_or_invalid_fields_are_unknown(value):
    usage = extract_usage(SimpleNamespace(usage={'input_tokens': value}, num_turns=value))
    assert usage.input_tokens is None and usage.num_turns is None
    assert usage.total_input_tokens is None


def test_zero_is_preserved_and_cost_is_ignored():
    usage = extract_usage(SimpleNamespace(usage=dict(input_tokens=0, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, output_tokens=0), total_cost_usd=123, duration_ms=0))
    assert usage.input_tokens == usage.total_input_tokens == usage.duration_ms == 0
    assert 'cost' not in usage.model_dump_json()
    assert extract_usage(None) == Usage()


def test_aggregate_missing_is_not_zero_or_partial_sum():
    result = aggregate_usage([{'stage':'execution','usage': report().model_dump()},
                              {'stage':'execution','usage':None}])
    assert result.total.calls == 2 and result.total.reported_calls == 1
    assert result.total.input_tokens is None and not result.total.complete
    assert result.review.calls == 0 and result.review.input_tokens is None


async def test_missing_usage_and_failure_count(app, isolated_cwd):
    app.runner.execute_mode = 'timeout'
    await _execute_task_core(app, 'missing', 't', isolated_cwd, [], None, None, 1)
    usage = await app.store.get_usage('missing')
    assert usage.total.calls == 1 and usage.total.reported_calls == 0
    assert usage.total.duration_ms is None
    rows = await app.store.get_invocations('missing')
    assert rows[0]['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('review,is_error', [(False,False),(False,True),(True,False),(True,True)])
async def test_terminal_sdk_usage_on_success_and_error(monkeypatch, tmp_path, review, is_error):
    runner = RealClaudeRunner()
    payload = ({'verdict':'PASS','summary':'ok','unmet_criteria':[],'evidence':[]} if review else
               {'status':'COMPLETED','summary':'ok','files_changed':[],'validation':[]})
    message = SimpleNamespace(session_id='sid', usage={'input_tokens':3,'output_tokens':5},
        num_turns=2, duration_ms=100, duration_api_ms=80, total_cost_usd=None,
        is_error=is_error, structured_output=payload, result=None)
    async def query(*args, **kwargs): return message, '', 'sid', None
    monkeypatch.setattr(runner, '_run_query', query)
    if review:
        result = await runner.review(original_task='t', acceptance=['c'], cwd=str(tmp_path),
            execution_summary=None, model=None, timeout_sec=30, session_id='sid')
    else:
        result = await runner.execute(task='t', acceptance=[], cwd=str(tmp_path), model=None,
            effort=None, timeout_sec=30, session_id='sid')
    assert result.usage.input_tokens == 3 and result.usage.output_tokens == 5
    assert result.usage.total_input_tokens is None
    assert result.usage.num_turns == 2


async def test_usage_and_compact_result_survive_store_reopen(app, isolated_cwd):
    from codex_claude_agent_mcp.session_store import SessionStore
    app.runner = UsageRunner()
    await _execute_task_core(app, 'persist', 't', isolated_cwd, [], None, None, 10)
    await app.close()
    store = SessionStore(app.config.db_path)
    await store.init()
    try:
        assert (await store.get_usage('persist')).total.input_tokens == 1
        assert (await store.get_job('persist'))['execution_result']['files_changed'] == ['fake/file.py']
    finally:
        await store.close()
