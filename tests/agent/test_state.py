import json
import stat

from botman_agent.state import AppState, StateStore, choose_start


def test_atomic_state_write_is_restrictive(tmp_path):
    store = StateStore(tmp_path / "state")
    state = AppState(subscription_id="s1", cursor="c1", last_realtime_usec=123)
    store.save("app-a", state)
    path = store.path_for("app-a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["cursor"] == "c1"
    assert store.load("app-a") == state


def test_short_same_subscription_resumes_cursor():
    state = AppState(subscription_id="s1", cursor="c1", last_realtime_usec=10_000_000)
    decision = choose_start(
        state, subscription_id="s1", now_realtime_usec=20_000_000, resume_max_age_sec=30
    )
    assert decision.mode == "cursor"
    assert decision.cursor == "c1"


def test_new_subscription_starts_tail_without_gap_marker():
    state = AppState(subscription_id="old", cursor="c1", last_realtime_usec=10_000_000)
    decision = choose_start(
        state, subscription_id="new", now_realtime_usec=20_000_000, resume_max_age_sec=30
    )
    assert decision.mode == "tail"
    assert not decision.gap_marker


def test_stale_same_subscription_starts_tail_with_gap_marker():
    state = AppState(subscription_id="s1", cursor="c1", last_realtime_usec=10_000_000)
    decision = choose_start(
        state, subscription_id="s1", now_realtime_usec=50_000_000, resume_max_age_sec=30
    )
    assert decision.mode == "tail"
    assert decision.gap_marker
