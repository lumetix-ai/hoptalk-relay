"""Running the synchronous services from the event loop.

Every call goes through thread-sensitive sync_to_async, so the worker's ORM work runs on one
executor thread with one database connection, and worker transactions interleave only at
await points. Each call first closes a connection that died or outlived its age, as a request
would, so a database restart costs one failed call instead of every later one.
"""

from collections.abc import Callable

from asgiref.sync import sync_to_async
from django.db import close_old_connections


def call_with_usable_connection[**Parameters, Result](
    function: Callable[Parameters, Result], *arguments: Parameters.args, **keyword_arguments: Parameters.kwargs
) -> Result:
    close_old_connections()
    return function(*arguments, **keyword_arguments)


async def run_in_database_thread[**Parameters, Result](
    function: Callable[Parameters, Result], *arguments: Parameters.args, **keyword_arguments: Parameters.kwargs
) -> Result:
    return await sync_to_async(call_with_usable_connection, thread_sensitive=True)(
        function, *arguments, **keyword_arguments
    )
