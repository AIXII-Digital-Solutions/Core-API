"""
Lazy ARQ enqueue helpers for code paths (e.g. DBQuery functions) that don't have
access to the request/app state. Maintains a single module-level pool per process.
"""
from arq import create_pool

from Queue import get_redis_settings, EXTERNAL_QUEUE, FILE_QUEUE

_pool = None


async def _get_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(get_redis_settings())
    return _pool


async def enqueue_external(func_name: str, **kwargs):
    pool = await _get_pool()
    return await pool.enqueue_job(func_name, _queue_name=EXTERNAL_QUEUE, **kwargs)


async def enqueue_file(func_name: str, **kwargs):
    pool = await _get_pool()
    return await pool.enqueue_job(func_name, _queue_name=FILE_QUEUE, **kwargs)


__all__ = ["enqueue_external", "enqueue_file"]
