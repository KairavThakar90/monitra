"""
One engine per process, however many requests arrive at once.

`get_engine` is called from every request's `get_db`. A cold worker -- just started,
or just recycled by the application pool -- meets its first requests together: the
morning's heartbeats, or a burst of starts. Each of those threads used to see
`_engine is None` and build an engine of its own, pool and listeners included, and
the last assignment won: 96 engines across four fresh workers under 200 simultaneous
starts, on the local rig. Here fifty threads ask at once and exactly one engine is
built.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from app.core import database


class EngineSingletonTests(unittest.TestCase):
    def setUp(self):
        self._saved = (database._engine, database._SessionLocal)
        database._engine, database._SessionLocal = None, None

    def tearDown(self):
        database._engine, database._SessionLocal = self._saved

    def test_fifty_simultaneous_first_requests_build_one_engine(self):
        built = []
        gate = threading.Barrier(50)
        lock = threading.Lock()

        def slow_create_engine(*args, **kwargs):
            # Long enough for every thread to be inside `get_engine` at once.
            import time
            time.sleep(0.05)
            engine = MagicMock(name="engine")
            with lock:
                built.append(engine)
            return engine

        seen = []

        def ask():
            gate.wait()
            seen.append(database.get_engine())

        with patch.object(database, "create_engine", side_effect=slow_create_engine), \
             patch.object(database, "_install_pool_monitoring"), \
             patch.object(database, "get_database_url", return_value="postgresql+psycopg2://u@127.0.0.1/x"), \
             patch.object(database, "_connect_args", return_value={}):
            threads = [threading.Thread(target=ask) for _ in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)

        self.assertEqual(len(built), 1, f"{len(built)} engines were built for one process")
        self.assertEqual(len(seen), 50)
        self.assertTrue(all(e is built[0] for e in seen), "every caller got the one engine")

    def test_the_sessionmaker_is_built_once_too(self):
        with patch.object(database, "get_engine", return_value=MagicMock(name="engine")):
            makers = {id(database.get_session_local()) for _ in range(5)}
        self.assertEqual(len(makers), 1)


if __name__ == "__main__":
    unittest.main()
