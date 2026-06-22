import asyncio
from typing import Optional

import aiohttp


def session_for_current_loop(
    session: Optional[aiohttp.ClientSession],
) -> Optional[aiohttp.ClientSession]:
    """Return ``session`` only when aiohttp can safely use it now.

    ``ClientSession`` instances are bound to the event loop that created them.
    Background tasks, bootstrapped runners, and tests can execute on a different
    loop than the app lifespan loop; reusing the shared app session there raises
    ``Future attached to a different loop``. Falling back to a per-call session is
    slower but correct for those edge paths.
    """
    if session is None or session.closed:
        return None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return session if getattr(session, "_loop", current_loop) is current_loop else None
