import asyncio
import json


def test_upload_outcome_is_saved_and_not_repeated_without_confirmation():
    from shorts.publications import Uploads
    class Pool:
        row = None
        async def fetchval(self, sql, version, chat, first, meta, force):
            if self.row and not force:
                return None
            self.row = dict(version=version,chat_id=chat,first_id=first,meta=json.loads(meta),status='uploading')
            return 1
        async def execute(self, sql, *args):
            if 'video_id=$2' in sql:
                self.row.update(status='uploaded',video_id=args[1],privacy=args[2])
            else:
                self.row.update(status='uncertain')
    async def scenario():
        pool = Pool()
        uploads = Uploads(pool)
        assert await uploads.claim('version',1,2,{'title':'Film'},False)
        await uploads.fail('version','network lost')
        assert not await uploads.claim('version',1,2,{'title':'Film'},False)
        assert await uploads.claim('version',1,2,{'title':'Film'},True)
        await uploads.complete('version','video123','public')
        assert pool.row['video_id'] == 'video123'
        assert not await uploads.claim('version',1,2,{'title':'Film'},False)
    asyncio.run(scenario())
