"""
Release-channel guards.

A channel is a second add-on with its own slug, so it is a second
config.yaml describing the same program. Keeping two in step by hand is the
failure this project has already paid for once: a stale pin in one file
shipped a controller with no ingress support, presenting as two unrelated
faults (#160). Two files multiply that by every option, schema entry and
permission.

So the EA add-on is generated, and these tests fail if the committed copy
is not what the generator produces. The generator and the guard state the
same rule twice, which turns drift into a red test instead of a support
thread.
"""

import sys
from pathlib import Path

import pytest
import yaml

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import sync_channels  # noqa: E402

GA = yaml.safe_load((CONTROLLER / "config.yaml").read_text())
EA_PATH = sync_channels.EA.path


def _ea():
    assert EA_PATH.joinpath("config.yaml").is_file(), (
        "controller-ea/config.yaml is missing — run "
        "controller/tools/sync_channels.py")
    return yaml.safe_load((EA_PATH / "config.yaml").read_text())


# ── No drift ──────────────────────────────────────────────────────────────────

def test_the_committed_channel_matches_the_generator():
    problems = sync_channels.check(sync_channels.EA)
    assert not problems, (
        "Channel add-on is out of date:\n  " + "\n  ".join(problems) +
        "\n\nRun: controller/tools/sync_channels.py")


@pytest.mark.parametrize("key", [
    "arch", "host_network", "ingress", "ingress_port", "panel_admin",
    "panel_icon", "environment", "options", "schema",
    "image", "init", "url",
])
def test_channel_shares_every_non_identity_field_with_ga(key):
    """
    A setting reachable in one channel and not the other is the divergence
    the deployment-parity rule exists to prevent, one level up: it would
    make an EA bug report unanswerable without first asking which add-on
    the person installed.
    """
    ea = _ea()
    if key not in GA:
        pytest.skip(f"GA config has no {key}")
    assert ea.get(key) == GA[key], (
        f"{key} differs between channels — regenerate rather than editing")


def test_options_and_schema_agree_key_for_key():
    # Belt and braces over the field comparison above: an option present in
    # one channel's schema but not the other is a validation difference that
    # only shows up when a user sets it.
    ea = _ea()
    assert set(ea["options"]) == set(GA["options"])
    assert set(ea["schema"]) == set(GA["schema"])


# ── Identity, which must differ ───────────────────────────────────────────────

def test_channel_has_its_own_slug():
    """
    The slug is the add-on's identity AND the name of its /data directory.
    Sharing one would make installing EA an in-place replacement of GA
    rather than a choice, and there would be no way back.
    """
    ea = _ea()
    assert ea["slug"] != GA["slug"]
    assert ea["slug"] == "controller-ea"


def test_channel_is_distinguishable_in_the_ui():
    # Two add-ons called "EchoMuse" with two identical panels is a support
    # thread waiting to happen.
    ea = _ea()
    assert ea["name"] != GA["name"]
    assert ea["panel_title"] != GA["panel_title"]


def test_channel_pulls_the_same_image_repository():
    # Channels differ by TAG, not by artefact source. A second repository
    # would need a second publish path and could drift in ways no test here
    # could see.
    ea = _ea()
    assert ea["image"] == GA["image"]


def test_channel_version_is_independent_of_ga():
    """
    version: is the one field a release moves, and it must survive a sync —
    regenerating EA must never quietly drag its pin back to GA's, which
    would ship GA code to everyone who opted into EA.
    """
    ea_before = (EA_PATH / "config.yaml").read_text()
    generated = sync_channels.generate(sync_channels.EA)["config.yaml"]
    import re
    got = re.search(r'^version:\s*"(.*)"', generated, re.M).group(1)
    want = re.search(r'^version:\s*"(.*)"', ea_before, re.M).group(1)
    assert got == want, "sync_channels must preserve the channel's own version"


def test_the_channel_has_a_documentation_tab():
    """
    Home Assistant renders DOCS.md on the Documentation tab. An empty tab is
    worst on the channel someone went out of their way to install — and it
    is where the migration warning has to live, since switching channels
    leaves existing devices unable to connect at all.
    """
    docs = EA_PATH / "DOCS.md"
    assert docs.is_file(), "controller-ea/DOCS.md is missing"
    text = docs.read_text()
    assert "Early Access" in text
    # The two things that will actually bite someone who installs this.
    assert "instead of the stable add-on" in text
    assert "tls/" in text


def test_generated_file_says_it_is_generated():
    # Someone WILL open this file to change a setting.
    text = (EA_PATH / "config.yaml").read_text()
    assert "DO NOT EDIT" in text
    assert "sync_channels.py" in text


def test_every_channel_documents_the_version_it_pins():
    """
    Supervisor shows an add-on's CHANGELOG.md when offering an update, and
    says "No changelog found" when there is none — precisely when someone is
    deciding whether to take it.

    Two separate misses, both found on a real update (2026-08-16):

      - controller-ea/ had NO changelog at all, because sync_channels.py's
        PRESENTATION tuple listed the translations, icon and logo and not
        this file. The generated channel therefore shipped without the one
        thing the update dialog reads.
      - GA 2.19.0 shipped with no entry of its own; the file jumped from
        1.0.1 to an Early Access heading.

    A release's notes also live in its tag annotation (the dashboard's own
    update notice reads that), so it is easy to write them there, see them
    rendered, and never notice Supervisor showing nothing.
    """
    for path in (CONTROLLER, CONTROLLER.parent / "controller-ea"):
        config = yaml.safe_load((path / "config.yaml").read_text())
        version = str(config["version"])

        changelog = path / "CHANGELOG.md"
        assert changelog.is_file(), (
            f"{path.name}/CHANGELOG.md is missing — Supervisor's update "
            f"dialog will read 'No changelog found'")

        assert version in changelog.read_text(), (
            f"{path.name}/CHANGELOG.md never mentions {version}, the version "
            f"its config.yaml pins — an update to it shows notes for some "
            f"other release")


def test_generator_preserves_existing_version_and_copies_docs(tmp_path, monkeypatch):
    controller = tmp_path / "controller"
    repo = tmp_path
    controller.mkdir()
    (controller / "translations").mkdir()
    (controller / "config.yaml").write_text(
        'name: "GA"\nslug: "ga"\npanel_title: GA\nversion: "2.0.0"\n'
        'description: "Stable"\noptions: {}\n'
    )
    (controller / "DOCS.md").write_text("stable docs")
    for rel in sync_channels.PRESENTATION:
        source = controller / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(rel.encode())

    channel = sync_channels.Channel("ea-test", "ea-test", "EA", "EA", "Early", "BANNER\n")
    channel_path = repo / channel.dirname
    channel_path.mkdir()
    (channel_path / "config.yaml").write_text('version: "1.5.0"\n')
    monkeypatch.setattr(sync_channels, "CONTROLLER", controller)
    monkeypatch.setattr(sync_channels, "REPO", repo)
    monkeypatch.setattr(sync_channels, "PRESENTATION", ("translations/en.yaml", "icon.png"))

    generated = sync_channels.generate(channel)
    assert 'version: "1.5.0"' in generated["config.yaml"]
    assert generated["DOCS.md"] == "BANNER\nstable docs"
    sync_channels.write(channel)
    assert not sync_channels.check(channel)
    assert (channel_path / "icon.png").read_bytes() == b"icon.png"

    (channel_path / "config.yaml").write_text("drift")
    assert "differs" in " ".join(sync_channels.check(channel))


def test_generator_uses_ga_version_when_ea_config_is_missing_or_malformed(tmp_path, monkeypatch):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "config.yaml").write_text('version: "3.0.0"\n')
    channel = sync_channels.Channel("ea", "ea", "EA", "EA", "Early", "")
    monkeypatch.setattr(sync_channels, "CONTROLLER", controller)
    monkeypatch.setattr(sync_channels, "REPO", tmp_path)
    assert sync_channels._current_version(tmp_path / "missing", "fallback") == "fallback"
    malformed = tmp_path / "malformed"
    malformed.write_text("name: no version")
    assert sync_channels._current_version(malformed, "fallback") == "fallback"
    assert 'version: "3.0.0"' in sync_channels.generate(channel)["config.yaml"]


def test_main_check_and_set_version_on_isolated_channel(tmp_path, monkeypatch, capsys):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "config.yaml").write_text('version: "4.0.0"\n')
    channel = sync_channels.Channel("ea", "ea", "EA", "EA", "Early", "")
    monkeypatch.setattr(sync_channels, "CONTROLLER", controller)
    monkeypatch.setattr(sync_channels, "REPO", tmp_path)
    monkeypatch.setattr(sync_channels, "EA", channel)
    monkeypatch.setattr(sync_channels, "PRESENTATION", ())

    assert sync_channels.main(["--set-version", "4.1.0"]) == 0
    assert 'version: "4.1.0"' in (tmp_path / "ea/config.yaml").read_text()
    assert sync_channels.main(["--check"]) == 0
    assert "in step" in capsys.readouterr().out
    assert sync_channels.main(["--set-version"]) == 2
