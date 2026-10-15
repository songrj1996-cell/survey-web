"""Optional context-local HTTP attempt gates; existing report calls remain unchanged."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

_gate = ContextVar("llm_attempt_gate", default=None)


class AttemptGate:
    def __init__(self, global_slots: asyncio.Semaphore, per_task: int, before_attempt):
        self.global_slots = global_slots
        self.local_slots = asyncio.Semaphore(per_task)
        self.before_attempt = before_attempt

    @asynccontextmanager
    async def slot(self):
        async with self.local_slots, self.global_slots:
            self.before_attempt()
            yield


@contextmanager
def bind_attempt_gate(gate):
    token = _gate.set(gate)
    try:
        yield
    finally:
        _gate.reset(token)


@asynccontextmanager
async def llm_attempt_slot():
    gate = _gate.get()
    if gate is None:
        yield
    else:
        async with gate.slot():
            yield
