import asyncio
import json

from shorts.jobs import Jobs


def test_completed_checkpoint_survives_failed_job_and_restart():
    class Pool:
        def __init__(self):
            self.rows = {}
        async def execute(self, sql, *args):
            if sql.startswith('INSERT'):
                identifier, kind, chat, payload = args
                self.rows.setdefault(identifier, dict(id=identifier,kind=kind,chat_id=chat,payload=payload,
                                                     status='queued',result=None,error=''))
            elif 'result=$2' in sql:
                self.rows[args[0]]['result'] = args[1]
            else:
                self.rows[args[0]].update(status=args[1],error=args[2])
            return 'UPDATE 1'
        async def fetchrow(self, sql):
            for row in self.rows.values():
                if row['status'] == 'queued':
                    row['status'] = 'running'
                    return dict(row)
            return None
        async def fetchval(self, sql, identifier, chat):
            row = self.rows[identifier]
            if row['chat_id'] == chat and row['status'] in {'failed','interrupted','cancelled'}:
                row['status'] = 'queued'
                return 1
    async def scenario():
        pool = Pool()
        jobs = Jobs(pool)
        identifier = await jobs.enqueue('visual', 1, {'remix_work':'/persistent/project'})
        assert await jobs.enqueue('visual', 1, {'remix_work':'/persistent/project'}) == identifier
        async def fail(kind, chat, data, job_id, result):
            await jobs.checkpoint(job_id, {'output':'/persistent/result.mp4','sha256':'abc'})
            jobs.stopping = True
            raise ValueError('Telegram delivery failed after rendering')
        await jobs.worker(fail)
        assert pool.rows[identifier]['status'] == 'failed'
        restarted = Jobs(pool)
        assert not await restarted.resume(identifier, 999)
        assert await restarted.resume(identifier, 1)
        seen = []
        async def finish(kind, chat, data, job_id, result):
            seen.append(result)
            restarted.stopping = True
        await restarted.worker(finish)
        assert seen == [{'output':'/persistent/result.mp4','sha256':'abc'}]
        assert pool.rows[identifier]['status'] == 'succeeded'
        assert len(pool.rows) == 1
        assert json.loads(pool.rows[identifier]['payload'])['remix_work'] == '/persistent/project'
    asyncio.run(scenario())


def test_worker_recovers_from_temporary_database_outage(monkeypatch):
    import shorts.jobs as module
    class Pool:
        calls = 0
        async def fetchrow(self, sql):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError('temporary outage')
            return dict(id='one',kind='visual',chat_id=1,payload={},result={})
        async def execute(self, *args):
            pass
    async def no_delay(*args):
        pass
    monkeypatch.setattr(module.asyncio, 'sleep', no_delay)
    async def scenario():
        jobs = Jobs(Pool())
        seen = []
        async def execute(*args):
            seen.append(args[0])
            jobs.stopping = True
        await jobs.worker(execute)
        assert seen == ['visual']
    asyncio.run(scenario())
