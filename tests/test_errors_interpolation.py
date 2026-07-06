"""InterpolationError shape — typed home for a failed pod interp job."""

from kinoforge.core.errors import InterpolationError, KinoforgeError


def test_interpolation_error_is_kinoforge_error_with_fields():
    # Bug caught: a bare Exception would lose job_id/server_error and break
    # `except KinoforgeError` handlers in the CLI.
    err = InterpolationError(job_id="rife-abc", server_error="cuda oom")
    assert isinstance(err, KinoforgeError)
    assert err.job_id == "rife-abc"
    assert err.server_error == "cuda oom"
    assert "rife-abc" in str(err)
    assert "cuda oom" in str(err)
