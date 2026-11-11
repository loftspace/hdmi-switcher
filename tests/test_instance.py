import pytest

from hdmi_follow.instance import AlreadyRunning, instance_lock


def test_second_instance_rejected_and_lock_released_on_exit(tmp_path):
    path = tmp_path / "lock"
    with instance_lock(path), pytest.raises(AlreadyRunning), instance_lock(path):
        pytest.fail("Second instance must not acquire the lock")
    with instance_lock(path):
        pass


def test_lock_released_on_exception(tmp_path):
    path = tmp_path / "lock"
    with pytest.raises(ValueError), instance_lock(path):
        raise ValueError("simulated failure")
    with instance_lock(path):
        pass
