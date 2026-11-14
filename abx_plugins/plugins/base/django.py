"""Django file responses shared by plugin views and their hosts."""

import asyncio

from django.core.handlers.asgi import ASGIRequest


class RangedFileReader:
    """
    Wraps a file like object with an iterator that runs over part (or all) of
    the file defined by start and stop. Blocks of block_size will be returned
    from the starting position, up to, but not including the stop point.
    https://github.com/satchamo/django/commit/2ce75c5c4bee2a858c0214d136bfcd351fcde11d
    """

    block_size = 8192

    def __init__(self, file_like, start=0, stop=float("inf"), block_size=None):
        self.f = file_like
        self.block_size = block_size or RangedFileReader.block_size
        self.start = start
        self.stop = stop

    def __iter__(self):
        try:
            self.f.seek(self.start)
            position = self.start
            while position < self.stop:
                data = self.f.read(min(self.block_size, self.stop - position))
                if not data:
                    break

                yield data
                position += len(data)
        finally:
            self.close()

    def close(self):
        self.f.close()


def _is_asgi_request(request) -> bool:
    return isinstance(request, ASGIRequest) or "scope" in request.__dict__


async def _stream_ranged_file_async(ranged_file: "RangedFileReader"):
    iterator = iter(ranged_file)
    try:
        while True:
            read = asyncio.create_task(asyncio.to_thread(next, iterator, None))
            try:
                chunk = await asyncio.shield(read)
            except asyncio.CancelledError:
                # Cancellation cannot stop the kernel read. Keep its task alive
                # until it releases BufferedReader's lock before scheduling close:
                # otherwise every disconnected slow reader consumes TWO executor
                # workers and can starve unrelated cached-file responses. A read
                # error after disconnect has no recipient; normal errors propagate.
                await asyncio.gather(read, return_exceptions=True)
                raise
            if chunk is None:
                break
            yield chunk
    finally:
        # Closing the file can itself perform filesystem work. Keep cleanup
        # off the event loop too, after the pending read has released its lock.
        await asyncio.to_thread(ranged_file.close)
