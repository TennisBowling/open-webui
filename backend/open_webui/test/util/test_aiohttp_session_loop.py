import asyncio

from open_webui.retrieval.web.session import session_for_current_loop


class _FakeSession:
    def __init__(self, loop, closed=False):
        self._loop = loop
        self.closed = closed


def test_session_for_current_loop_accepts_matching_open_session():
    async def run():
        session = _FakeSession(asyncio.get_running_loop())
        assert session_for_current_loop(session) is session

    asyncio.run(run())


def test_session_for_current_loop_rejects_closed_or_foreign_loop_session():
    foreign_loop = asyncio.new_event_loop()
    try:
        async def run():
            closed_session = _FakeSession(asyncio.get_running_loop(), closed=True)
            assert session_for_current_loop(closed_session) is None
            assert session_for_current_loop(_FakeSession(foreign_loop)) is None

        asyncio.run(run())
    finally:
        foreign_loop.close()
