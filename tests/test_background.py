import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner
from codex_claude_agent_mcp.server import AppState, _execute_task_core, _get_job_status_core, _continue_task_core, _review_task_core
from codex_claude_agent_mcp.session_store import SessionStore


class GateRunner(FakeClaudeRunner):
    def __init__(self, confirm=True):
        super().__init__()
        self.started=asyncio.Event()
        self.release=asyncio.Event()
        self.active=0
        self.peak=0
        self.confirm=confirm

    async def execute(self, **kwargs):
        self.active += 1
        self.peak=max(self.peak,self.active)
        if self.confirm:
            await kwargs['on_session_id'](kwargs['session_id'])
        self.started.set()
        try:
            await self.release.wait()
            return await super().execute(**kwargs)
        finally:
            self.active -= 1


async def submit(app, cwd, job_id='bg', **kwargs):
    return await _execute_task_core(app, job_id, 't', cwd, ['c'], None, None, 30, **kwargs)


async def drain(app):
    async with asyncio.timeout(5):
        while app.background_tasks:
            await asyncio.gather(*list(app.background_tasks), return_exceptions=True)
            await asyncio.sleep(0)


async def test_background_returns_before_runner_releases_and_persists(app, isolated_cwd):
    runner=app.runner=GateRunner()
    result=await asyncio.wait_for(submit(app, isolated_cwd, background=True), 2)
    assert result.status == 'QUEUED' and not result.execution_completed
    assert result.execution_session_id and not result.execution_session_confirmed
    await asyncio.wait_for(runner.started.wait(), 2)
    status=await _get_job_status_core(app,'bg',isolated_cwd)
    assert status.status == 'EXECUTING' and status.execution_session_confirmed
    assert status.usage.total.calls == 1 and status.usage.total.reported_calls == 0
    runner.release.set()
    await drain(app)
    status=await _get_job_status_core(app,'bg',isolated_cwd)
    assert status.status == 'COMPLETED'
    assert status.execution_result.execution_completed
    assert status.execution_result.files_changed == ['fake/file.py']
    assert not any(call[0] == 'review' for call in runner.calls)


@pytest.mark.parametrize('mode,expected',[('blocked','BLOCKED'),('failure','EXECUTION_INCOMPLETE'),('timeout','EXECUTION_INCOMPLETE')])
async def test_background_outcomes(app, isolated_cwd, mode, expected):
    app.runner.execute_mode=mode
    await submit(app,isolated_cwd,background=True)
    await drain(app)
    status=await _get_job_status_core(app,'bg',isolated_cwd)
    assert status.status == expected
    if mode == 'timeout': assert status.execution_error.code == 'TASK_TIMEOUT'


async def test_background_scheduler_and_duplicate_and_no_early_review(app, isolated_cwd):
    runner=app.runner=GateRunner()
    for i in range(5):
        await submit(app,isolated_cwd,f'bg{i}',background=True)
    await runner.started.wait()
    duplicate=await submit(app,isolated_cwd,'bg0',background=True)
    assert duplicate.error and duplicate.error.code == 'STATE_STORE_ERROR'
    review=await _review_task_core(app,'bg0','t',['c'],isolated_cwd,None,None,30)
    assert review.error.code == 'JOB_STATE_CONFLICT'
    assert runner.peak <= app.config.max_concurrency
    # Three queued jobs have not invoked the runner yet.
    jobs=await app.store.list_jobs()
    assert sum(j['status']=='QUEUED' for j in jobs) >= 3
    runner.release.set()
    await drain(app)
    assert runner.peak == app.config.max_concurrency
    assert all(j['status']=='COMPLETED' for j in await app.store.list_jobs())


async def test_sync_remains_blocking(app, isolated_cwd):
    runner=app.runner=GateRunner()
    task=asyncio.create_task(submit(app,isolated_cwd,background=False))
    await runner.started.wait()
    assert not task.done()
    runner.release.set()
    assert (await task).execution_completed


async def test_request_cancellation_during_admission_does_not_cancel_job(app, isolated_cwd, monkeypatch):
    created=asyncio.Event()
    release=asyncio.Event()
    original=app.store.create_job
    async def create(*args, **kwargs):
        result=await original(*args, **kwargs)
        created.set()
        await release.wait()
        return result
    monkeypatch.setattr(app.store,'create_job',create)
    request=asyncio.create_task(submit(app,isolated_cwd,background=True))
    await created.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError): await request
    release.set()
    await drain(app)
    assert (await app.store.get_job('bg'))['status']=='COMPLETED'


@pytest.mark.parametrize('confirmed',[False,True])
async def test_shutdown_preserves_resume_rules(app, isolated_cwd, confirmed):
    runner=app.runner=GateRunner(confirm=confirmed)
    await submit(app,isolated_cwd,background=True)
    await runner.started.wait()
    # Include queued work, which must not be abandoned when never scheduled.
    for i in range(4): await submit(app,isolated_cwd,f'queued{i}',background=True)
    await app.close()
    restarted=await AppState.create(config=app.config,runner=FakeClaudeRunner())
    try:
        status=await _get_job_status_core(restarted,'bg',isolated_cwd)
        assert status.status=='EXECUTION_INCOMPLETE'
        assert status.execution_session_confirmed is confirmed
        assert status.execution_error.code=='CANCELLED'
        assert not any(j['status'] in {'QUEUED','EXECUTING'} for j in await restarted.store.list_jobs())
        resumed=await _continue_task_core(restarted,'bg',status.execution_session_id,'fix',isolated_cwd,None,None,30)
        assert resumed.execution_completed is confirmed
        if not confirmed: assert resumed.error.code=='SESSION_NOT_FOUND'
    finally:
        await restarted.close()


async def test_new_server_and_poll_do_not_steal_live_owner(app, isolated_cwd):
    runner=app.runner=GateRunner()
    await submit(app,isolated_cwd,background=True)
    await runner.started.wait()
    other=await AppState.create(config=app.config,runner=FakeClaudeRunner())
    try:
        status=await _get_job_status_core(other,'bg',isolated_cwd)
        assert status.status=='EXECUTING'
    finally: await other.close()
    runner.release.set()
    await drain(app)
    assert (await app.store.get_job('bg'))['status']=='COMPLETED'


@pytest.mark.parametrize('active', ['QUEUED','EXECUTING','RESUMING_EXECUTION','REVIEWING'])
async def test_startup_recovers_dead_process_not_live_process(config, isolated_cwd, active):
    # A separate process holds an OS ownership lock and then is killed. No SDK/API.
    script=textwrap.dedent('''
        import asyncio,sys
        from pathlib import Path
        from codex_claude_agent_mcp.session_store import SessionStore
        from codex_claude_agent_mcp.ownership import ProcessOwner
        async def main():
            store=SessionStore(Path(sys.argv[1])); await store.init()
            owner=ProcessOwner(store.db_path)
            await store.create_job('dead','t',['c'],sys.argv[2],status=sys.argv[3],
                project_root=sys.argv[2],execution_session_id='confirmed',
                review_session_id='review',owner_id=owner.owner_id)
            await store.confirm_session('dead','execution','confirmed')
            await store.start_invocation('inv','dead','review' if sys.argv[3]=='REVIEWING' else 'execution',
                'review' if sys.argv[3]=='REVIEWING' else 'execute','confirmed',owner.owner_id)
            print('ready',flush=True)
            sys.stdin.readline()
        asyncio.run(main())
    ''')
    proc=subprocess.Popen([sys.executable,'-c',script,str(config.db_path),isolated_cwd,active],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert await asyncio.to_thread(proc.stdout.readline) == 'ready\n'
        app=await AppState.create(config=config,runner=FakeClaudeRunner())
        try:
            assert (await app.store.get_job('dead'))['status']==active
            proc.kill(); await asyncio.to_thread(proc.wait)
        finally: await app.close()
        restarted=await AppState.create(config=config,runner=FakeClaudeRunner())
        try:
            status=await _get_job_status_core(restarted,'dead',isolated_cwd)
            stage='REVIEW' if active=='REVIEWING' else 'EXECUTION'
            assert status.status==stage+'_INCOMPLETE'
            assert status.execution_session_id=='confirmed' and status.execution_session_confirmed
            assert (await restarted.store.get_invocations('dead'))[0]['status']=='INCOMPLETE'
        finally: await restarted.close()
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        proc.stdin.close(); proc.stdout.close(); proc.stderr.close()


async def test_invalid_background_matches_sync_no_job(app, isolated_cwd):
    for background in (False,True):
        result=await _execute_task_core(app,'invalid','t',isolated_cwd,[],None,'invalid',30,background=background)
        assert result.error.code=='INVALID_ARGUMENT'
        assert await app.store.get_job('invalid') is None
    assert app.runner.calls==[]


async def test_unexpected_background_exception_is_observed_and_persisted(app, isolated_cwd):
    class Broken(FakeClaudeRunner):
        async def execute(self, **kwargs): raise RuntimeError('broken')
    app.runner=Broken()
    await submit(app,isolated_cwd,background=True)
    await drain(app)
    status=await _get_job_status_core(app,'bg',isolated_cwd)
    assert status.status=='EXECUTION_INCOMPLETE' and status.execution_error.code=='INTERNAL_ERROR'
    assert not app.background_tasks


async def test_cancel_cas_rejects_wrong_owner_and_persists_atomic_evidence(app, isolated_cwd):
    runner=app.runner=GateRunner()
    await submit(app,isolated_cwd,background=True)
    await runner.started.wait()
    await app.store.mark_cancelled('bg','EXECUTING','execution','other-owner')
    assert (await app.store.get_job('bg'))['status']=='EXECUTING'
    for task in list(app.background_tasks): task.cancel()
    await drain(app)
    job=await app.store.get_job('bg')
    assert job['status']=='EXECUTION_INCOMPLETE' and job['execution_status']=='INCOMPLETE'
    assert job['execution_error']['code']=='CANCELLED'
    assert job['execution_error']['details']['session_confirmed']


async def test_terminal_failed_payload_is_failed_not_transport_incomplete(app, isolated_cwd):
    class Failed(FakeClaudeRunner):
        async def execute(self, **kwargs):
            result=await super().execute(**kwargs)
            result.status='FAILED'
            result.summary='Tests failed: regression'
            return result
    app.runner=Failed()
    await submit(app,isolated_cwd,background=True)
    await drain(app)
    status=await _get_job_status_core(app,'bg',isolated_cwd)
    assert status.status=='FAILED'
    assert status.execution_result.execution_completed


async def test_background_job_root_stays_bound_through_continue_review(app, isolated_cwd, tmp_path):
    from pathlib import Path
    app.config.allow_any_cwd=False
    app.config.allowed_roots=[]
    child=Path(isolated_cwd)/'child'; child.mkdir()
    result=await submit(app,str(child),background=True,project_root=isolated_cwd)
    await drain(app)
    other=tmp_path/'unrelated'; other.mkdir()
    denied=await _get_job_status_core(app,'bg',str(other))
    assert denied.error.code=='INVALID_CWD'
    changed=await _continue_task_core(app,'bg',result.execution_session_id,'fix',isolated_cwd,None,None,30)
    assert changed.error.code=='JOB_CONTEXT_MISMATCH'
    resumed=await _continue_task_core(app,'bg',result.execution_session_id,'fix',str(child),None,None,30)
    assert resumed.execution_completed
    review=await _review_task_core(app,'bg','t',['c'],str(child),None,None,30)
    assert review.status=='PASS' and review.review_session_id!=result.execution_session_id
    assert (await app.store.get_job('bg'))['project_root']==str(Path(isolated_cwd).resolve())
