"""Native Django integration shared by plugin apps and their hosts."""

import asyncio

from django.core.handlers.asgi import ASGIRequest
from django.dispatch import Signal


webhook_event = Signal()


def register_webhooks(model, *, events=None, status_events=None):
    """Register a model in signal_webhooks from its AppConfig.ready()."""
    from signal_webhooks.settings import webhook_settings
    from signal_webhooks.utils import reference_for_model

    declarations = (
        events if events is not None else getattr(model, "webhook_events", {})
    )
    if status_events is not None:
        model._meta.get_field("status")
        if set(status_events.values()) - declarations.keys():
            raise ValueError("Status transitions require declared webhook events")
        setattr(model, "webhook_status_events", status_events)
    if events is not None:
        setattr(model, "webhook_events", events)
    # Reuse the package registry so native CRUD handlers and model validation
    # see the same registration; preserve an explicitly disabled/custom handler.
    webhook_settings.HOOKS.setdefault(reference_for_model(model), ...)


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
