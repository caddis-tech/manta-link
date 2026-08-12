"""The watchdog, and the watchdog's own watchdog.

Health restarts the other workers, so nothing inside the worker set can notice
when health itself dies. The main thread does that, from inside the reader loop,
and these tests drive both halves by hand rather than waiting on real timers.
"""

import threading

import pytest

from manta_link import health as health_mod
from manta_link.health import Counters, Health

from .fakes import wait_until


@pytest.fixture
def counters():
    return Counters()


def blocks_forever() -> None:
    threading.Event().wait()


def ends_immediately() -> None:
    # SystemExit is the one thing the supervisor does not restart, so this is
    # how a worker thread actually dies rather than looping.
    raise SystemExit(0)


# pytest reports a SystemExit that ends a thread as an unhandled thread
# exception. Here it is the mechanism under test, not an accident.
kills_a_thread = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)


class TestCounters:
    def test_bumping_an_unseen_name_starts_at_one(self, counters):
        counters.bump("records_captured")
        assert counters.get("records_captured") == 1

    def test_an_unbumped_name_reads_zero(self, counters):
        assert counters.get("never_touched") == 0

    def test_a_snapshot_does_not_change_underneath_its_reader(self, counters):
        counters.bump("heartbeats")
        taken = counters.snapshot()
        counters.bump("heartbeats")
        assert taken == {"heartbeats": 1}

    def test_concurrent_bumps_lose_nothing(self, counters):
        def bump_many() -> None:
            for _ in range(500):
                counters.bump("records_captured")

        threads = [threading.Thread(target=bump_many) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert counters.get("records_captured") == 4000


class TestWorkerWatchdog:
    def test_a_worker_registered_late_is_started_without_being_called_a_restart(
        self, counters
    ):
        entered = threading.Event()
        health = Health(counters)
        health.register("late", lambda: (entered.set(), blocks_forever()))

        health.tick()

        assert entered.wait(2.0)
        assert counters.get("late_thread_restarts") == 0

    def test_a_live_worker_is_left_alone(self, counters):
        health = Health(counters)
        health.register("steady", blocks_forever)
        health.tick()
        health.tick()
        assert counters.get("steady_thread_restarts") == 0

    @kills_a_thread
    def test_a_dead_worker_is_restarted_and_counted(self, counters):
        runs = []
        health = Health(counters)
        health.register("brittle", lambda: (runs.append(1), ends_immediately()))

        health.tick()
        assert wait_until(lambda: not health._threads["brittle"].is_alive())
        health.tick()

        assert wait_until(lambda: len(runs) == 2)
        assert counters.get("brittle_thread_restarts") == 1
        # Let the replacement finish dying inside this test, so its exit is not
        # reported against whichever test happens to be running next.
        health._threads["brittle"].join(2.0)

    def test_a_supervised_restart_is_counted_separately(self, counters):
        """Two different failures, so two different tallies.

        A loop that raised and was restarted in place is a bad cycle. A thread
        that is gone is a worker that stopped existing, and only the second one
        means the watchdog had to intervene.
        """
        health = Health(counters)
        health.note_restart("capture")
        assert counters.get("capture_restarts") == 1
        assert counters.get("capture_thread_restarts") == 0


class TestHealthWatchesItself:
    @kills_a_thread
    def test_a_dead_health_thread_is_restarted_by_the_main_thread(self, counters):
        class DyingHealth(Health):
            def run_forever(self) -> None:
                self.tick()
                raise SystemExit(0)

        health = DyingHealth(counters)
        health.start()
        first = health._thread
        assert first is not None
        first.join(2.0)
        assert not first.is_alive()

        health.check_from_main_thread()

        assert health._thread is not first
        assert counters.get("health_thread_restarts") == 1
        # As above: the replacement dies too, and it should do so here.
        health._thread.join(2.0)

    def test_a_health_thread_that_has_stopped_ticking_is_restarted(
        self, counters, monkeypatch
    ):
        """Alive is not the same as working.

        A wedged thread cannot be killed, so the restart leaves two. Two
        watchdogs is tolerable; no watchdog is what this is preventing.
        """
        monkeypatch.setattr(health_mod, "HEALTH_STALL_S", 0.0)

        class WedgedHealth(Health):
            def run_forever(self) -> None:
                blocks_forever()

        health = WedgedHealth(counters)
        health.start()
        first = health._thread

        health.check_from_main_thread()

        assert health._thread is not first
        assert counters.get("health_thread_restarts") == 1

    def test_a_ticking_health_thread_is_left_alone(self, counters):
        health = Health(counters)
        health.start()
        first = health._thread

        for _ in range(20):
            health.check_from_main_thread()

        assert health._thread is first
        assert counters.get("health_thread_restarts") == 0


class TestHeartbeat:
    def test_it_beats_once_the_interval_has_passed(self, counters):
        health = Health(counters, heartbeat_interval_s=0.0)
        health.tick()
        assert counters.get("heartbeats") == 1

    def test_it_does_not_beat_inside_the_interval(self, counters):
        health = Health(counters, heartbeat_interval_s=3600.0)
        health.start()
        health.tick()
        health.tick()
        assert counters.get("heartbeats") == 0

    def test_the_beat_carries_the_counters(self, counters, caplog):
        counters.bump("records_captured", 7)
        health = Health(counters, heartbeat_interval_s=0.0)
        with caplog.at_level("INFO"):
            health.tick()
        assert "records_captured=7" in caplog.text
