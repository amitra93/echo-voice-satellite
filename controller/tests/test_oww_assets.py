"""
Asset-distribution planning.

The transport can only be exercised against a real device; the DECISIONS —
what to push, what to delete, what to refuse — are pure and are the part
that can quietly do damage, so they are tested here. A wrong prune deletes
the model a device is using; a wrong "keep" leaves it scoring against a
stale classifier that silently disagrees with the controller.
"""

import hashlib
import time

import pytest

import em_oww_assets as A


def _asset(name, md5, kind, size=1000):
    from pathlib import Path
    return A.Asset(name=name, source=Path("/src") / name, md5=md5, size=size, kind=kind)


def _base(md5_rt="rt1", md5_mel="mel1", md5_emb="emb1"):
    return [
        _asset(A.RUNTIME_NAME, md5_rt, "runtime", size=12_290_332),
        _asset("melspectrogram.onnx", md5_mel, "shared", size=1_087_958),
        _asset("embedding_model.onnx", md5_emb, "shared", size=1_326_578),
    ]


NOW = int(time.time())


def test_a_device_with_everything_is_a_noop():
    """The sync must be idempotent — it runs on every enable, not just once."""
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    p = A.plan_sync(desired, actual)
    assert p.is_noop
    assert sorted(p.keep) == sorted(a.name for a in desired)


def test_only_changed_files_are_pushed():
    """
    12.3MB over a shell transport is slow enough that re-pushing an unchanged
    runtime on every sync would make the feature feel broken.
    """
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    actual = {
        A.RUNTIME_NAME: ("rt1", NOW),
        "melspectrogram.onnx": ("mel1", NOW),
        "embedding_model.onnx": ("OLD", NOW),
        "hey_mycroft_v0.1.onnx": ("c1", NOW),
    }
    p = A.plan_sync(desired, actual)
    assert [a.name for a in p.push] == ["embedding_model.onnx"]
    assert A.RUNTIME_NAME in p.keep


def test_a_missing_device_gets_everything():
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    p = A.plan_sync(desired, {})
    assert len(p.push) == 4
    assert not p.prune


def test_the_selected_classifier_is_never_pruned():
    """
    Evicting the model the device is configured to use is the one outcome
    that breaks it outright rather than costing a re-push.
    """
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    for i in range(6):
        actual[f"old{i}.onnx"] = (f"x{i}", NOW - 10_000 * (i + 1))
    p = A.plan_sync(desired, actual)
    assert "selected.onnx" not in p.prune
    assert "selected.onnx" in p.keep


def test_extra_classifiers_are_evicted_oldest_first():
    """LRU by device mtime — no controller-side bookkeeping to lose."""
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    actual["newest.onnx"] = ("a", NOW - 100)
    actual["middle.onnx"] = ("b", NOW - 5_000)
    actual["oldest.onnx"] = ("c", NOW - 90_000)
    actual["ancient.onnx"] = ("d", NOW - 900_000)

    p = A.plan_sync(desired, actual, slots=4)
    # 1 desired + 3 extras fills four slots; the oldest falls off.
    assert sorted(p.prune) == ["ancient.onnx"]
    assert "newest.onnx" in p.keep and "middle.onnx" in p.keep


def test_the_runtime_and_shared_models_are_never_evictable():
    """
    They are required by definition — pruning one would delete something the
    same sync is about to push back.
    """
    desired = _base() + [_asset("a.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW - 999_999) for a in desired}
    p = A.plan_sync(desired, actual, slots=1)
    assert not p.prune


def test_unrecognised_files_are_left_alone():
    """
    This deletes only what it positively recognises as an evictable
    classifier. A stray file in the directory is not ours to remove.
    """
    desired = _base() + [_asset("a.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    actual["notes.txt"] = ("z", NOW - 999_999)
    actual["libsomething.so"] = ("y", NOW - 999_999)
    p = A.plan_sync(desired, actual, slots=1)
    assert p.prune == []


def test_a_full_device_is_blocked_with_a_reason_not_half_written():
    desired = _base()
    p = A.plan_sync(desired, {}, free_mb=60)
    assert p.blocked and "free" in p.blocked
    assert not p.push, "a blocked plan must not push anything"


def test_space_is_only_checked_against_what_actually_needs_sending():
    """
    A device that already has everything must never be blocked for space —
    the sync runs on every enable, and refusing a no-op would report a
    problem that does not exist.
    """
    desired = _base()
    actual = {a.name: (a.md5, NOW) for a in desired}
    p = A.plan_sync(desired, actual, free_mb=1)
    assert p.blocked is None
    assert p.is_noop


# ─── Device inventory parsing ────────────────────────────────────────────────

def test_listing_parses_md5_mtime_name():
    text = ("aff8f1c6bee88425111e62aadf3c381c 1753900000 "
            f"{A.DEVICE_DIR}/libonnxruntime.so\n")
    got = A.parse_device_listing(text)
    assert got == {"libonnxruntime.so": ("aff8f1c6bee88425111e62aadf3c381c", 1753900000)}


@pytest.mark.parametrize("line", [
    "",
    "garbage",
    "nothexdigest 123 /x/a.onnx",
    "aff8f1c6bee88425111e62aadf3c381c notanint /x/a.onnx",
    "aff8f1c6 123 /x/a.onnx",
    "md5sum: can't open '/x/a.onnx': No such file or directory",
])
def test_unparseable_lines_are_skipped_not_guessed(line):
    """
    Shell noise (a prompt echo, an error from a missing file) must not become
    an inventory entry. Over-reporting a file as present makes the sync push
    nothing and claim success — the one failure mode with no visible symptom.
    """
    assert A.parse_device_listing(line) == {}


def test_a_partial_inventory_is_safe():
    """
    Missing an entry costs an unnecessary push. That is the right direction
    to fail in, and this pins it: an empty parse means push everything.
    """
    desired = _base()
    p = A.plan_sync(desired, A.parse_device_listing("junk\n"))
    assert len(p.push) == len(desired)


# ─── Contract with the device ────────────────────────────────────────────────

def test_device_dir_matches_the_firmware_constant():
    """
    DEVICE_DIR and shadow.DefaultDir are the same path in two languages. If
    they drift, the controller installs assets the device will not look for,
    and the only symptom is shadow mode silently never starting.
    """
    from pathlib import Path
    go = (Path(__file__).resolve().parents[2]
          / "device/internal/wakeword/shadow/open.go").read_text()
    assert f'DefaultDir = "{A.DEVICE_DIR}"' in go, (
        "em_oww_assets.DEVICE_DIR has drifted from shadow.DefaultDir"
    )


def test_shared_model_names_match_what_the_device_opens():
    from pathlib import Path
    go = (Path(__file__).resolve().parents[2]
          / "device/internal/wakeword/shadow/open.go").read_text()
    for name in A.SHARED_NAMES:
        assert f'"{name}"' in go, f"{name} is not what the device opens"
    assert f'"{A.RUNTIME_NAME}"' in go


def test_classifier_filename_matches_the_device_stem_rule():
    """
    The device derives the classifier filename with shadow.ModelStem, which
    mirrors em_oww_models.prediction_key. Both must agree, or we send
    hey_mycroft_v0.1.onnx and the device opens hey_mycroft_v0.onnx — the
    exact bug ModelStem was written to fix.
    """
    import em_oww_models
    assert em_oww_models.prediction_key("hey_mycroft_v0.1") == "hey_mycroft_v0.1"
    assert em_oww_models.prediction_key("/data/oww_models/clara.onnx") == "clara"


# ─── df parsing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,want", [
    # Wrapped filesystem name — what a real device actually returned.
    ("                  1010       648       346  65% /data", 346),
    # Unwrapped, the shape the column index was originally written for.
    ("/dev/block/platform/mtk-msdc.0/by-name/userdata 1010 648 346 65% /data", 346),
    ("tmpfs 100 0 100 0% /data", 100),
])
def test_free_space_is_read_from_the_percentage_anchor_not_a_column_index(line, want):
    """
    busybox wraps a long filesystem name onto its own line, so the available
    column is $3 sometimes and $4 others. awk '{print $4}' returned "65%" on a
    real device — which parsed as no reading, silently disabling the
    free-space check rather than failing visibly.
    """
    assert A.parse_free_mb(line) == want


@pytest.mark.parametrize("line", ["", "garbage", "df: /data: No such file or directory"])
def test_unreadable_df_yields_no_measurement(line):
    """None means 'unknown', which plan_sync treats as 'do not check' — the
    opposite of 0, which would block every install."""
    assert A.parse_free_mb(line) is None


def test_unknown_free_space_does_not_block():
    desired = _base()
    p = A.plan_sync(desired, {}, free_mb=A.parse_free_mb("garbage"))
    assert p.blocked is None and p.push


def test_paths_md5_and_runtime_source(tmp_path):
    runtime = tmp_path / A.RUNTIME_NAME
    runtime.write_bytes(b"runtime")
    assert A.device_path("x.onnx") == f"{A.DEVICE_DIR}/x.onnx"
    assert A.runtime_source(tmp_path) == runtime
    assert A.runtime_source(tmp_path / "missing") is None
    assert A.md5_file(runtime, _chunk=2) == hashlib.md5(b"runtime").hexdigest()


def test_classifier_source_resolves_stock_custom_and_missing(tmp_path):
    resources = tmp_path / "resources"
    models = tmp_path / "models"
    resources.mkdir()
    models.mkdir()
    (resources / "hey.onnx").write_bytes(b"stock")
    (models / "custom.onnx").write_bytes(b"custom")

    assert A.classifier_source("hey", resources, models) == resources / "hey.onnx"
    assert A.classifier_source("custom.onnx", resources, models) == models / "custom.onnx"
    assert A.classifier_source("/absolute/custom.onnx", resources, models) is None
    assert A.classifier_source("", resources, models) is None
    assert A.classifier_source("missing", resources, models) is None
    assert A.classifier_source("hey", None, models) is None


def test_desired_assets_reports_missing_sources_and_deduplicates(tmp_path):
    runtime_dir = tmp_path / "runtime"
    resources = tmp_path / "resources"
    models = tmp_path / "models"
    runtime_dir.mkdir()
    resources.mkdir()
    models.mkdir()
    (runtime_dir / A.RUNTIME_NAME).write_bytes(b"rt")
    for name in A.SHARED_NAMES:
        (resources / name).write_bytes(name.encode())
    (resources / "hey.onnx").write_bytes(b"hey")
    (models / "custom.onnx").write_bytes(b"custom")

    assets, problems = A.desired_assets(
        ["hey", "hey", "custom.onnx", "missing"],
        runtime_dir=runtime_dir,
        resources=resources,
        models_dir=models,
    )
    names = [asset.name for asset in assets]
    assert names.count("hey.onnx") == 1
    assert names.count("custom.onnx") == 1
    assert len(assets) == 5
    assert any("missing" in problem for problem in problems)
    assert all(len(asset.md5) == 32 and asset.size > 0 for asset in assets)

    assets, problems = A.desired_assets([], runtime_dir=tmp_path / "no-runtime", resources=None, models_dir=None)
    assert not assets
    assert len(problems) >= 3
