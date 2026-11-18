import asyncio
import os
import sys


def test_cancelling_remix_process_stops_child(tmp_path):
    from shorts import remix
    pidfile = tmp_path / 'pid'
    async def scenario():
        task = asyncio.create_task(remix.run(sys.executable, '-c',
            'import os,time,pathlib;pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()));time.sleep(30)'))
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(.01)
        assert pidfile.exists()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        pid = int(pidfile.read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        os.kill(pid, 9)
        raise AssertionError('Cancelled render process is still alive')
    asyncio.run(scenario())
