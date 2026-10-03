"""kinoforge image: the --ephemeral contract (Task 7) and the CLI (Task 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

IMAGE_CFG_YAML = """\
mode: t2i
prompt: "a cat in a meadow"

image:
  engine: {engine}
  spec:
    model: "{model}"

output:
  dir: out
"""


def _write_cfg(tmp_path: Path, engine: str, model: str = "m") -> Path:
    path = tmp_path / f"{engine}-t2i.yaml"
    path.write_text(IMAGE_CFG_YAML.format(engine=engine, model=model))
    return path


@pytest.mark.parametrize("engine", ["fal", "luma_agents", "replicate"])
def test_ephemeral_is_refused_for_every_live_image_engine(
    tmp_path: Path, engine: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusal is a DECISION, keyed on the image engine, not a key miss.

    Bug this catches: the refusal drifting back to the accidental ("", None)
    lookup miss it is today — which would silently start ALLOWING --ephemeral
    the moment anyone gave Config.engine a non-None default, with no provider
    scrub hook anywhere to honour it.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, engine)
    rc = main(["--ephemeral", "image", "-c", str(cfg), "--prompt", "x", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--ephemeral" in err
    assert engine in err


def test_ephemeral_refusal_names_the_image_engine_not_video_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message must not offer the video table's remedies.

    Bug this catches: reusing _preflight_error_block verbatim, which tells the
    operator to switch to `engine: replicate` or `engine: runway` — neither of
    which is an IMAGE engine, so the advice is nonsense on this path.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, "luma_agents", model="uni-1")
    main(["--ephemeral", "image", "-c", str(cfg), "--prompt", "x", "--dry-run"])
    err = capsys.readouterr().err
    assert "engine: runway" not in err
    assert "luma_agents" in err


def test_fake_image_engine_permits_ephemeral(tmp_path: Path) -> None:
    """The in-process fake has no provider-side state, so it is trivially safe.

    Bug this catches: a blanket refusal for every image config, which would
    make the ephemeral path untestable offline.
    """
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    assert IMAGE_EPHEMERAL_CAPABILITIES["fake"] is True
    assert IMAGE_EPHEMERAL_CAPABILITIES["fal"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["luma_agents"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["replicate"] is False


def test_image_table_covers_every_registered_image_engine() -> None:
    """Guard the guard: a new image engine must not default to "allowed".

    Bug this catches: adding a fifth image engine and forgetting the table, so
    `.get()` returns None and the operator gets a confusing refusal — or worse,
    a permissive default if the lookup is ever written with `.get(name, True)`.
    """
    import kinoforge._adapters  # noqa: F401  — self-registration
    from kinoforge.core import registry
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    registered = set(registry._image_engines)
    assert registered, "no image engines registered; the assertion below is vacuous"
    missing = registered - set(IMAGE_EPHEMERAL_CAPABILITIES)
    assert not missing, (
        f"image engines with no IMAGE_EPHEMERAL_CAPABILITIES entry: {sorted(missing)}"
    )
