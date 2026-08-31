"""
Entity names must NOT repeat the device label — CLAUDE.md's "HA entities
beyond the voice satellite" / "Entity names must NOT repeat the device
label" rule, ported to this integration's own source.

HA composes the displayed name from `<device name> <entity name>` for any
entity with `_attr_has_entity_name = True` set (`entities.py`'s
`EchoCoordinatorEntity`, whose `device_info["name"]` is the device's own
`label`, or the device_id if unlabelled). Baking that label into an entity's
own `_attr_name` — directly, or by composing a string that includes it —
repeats it twice ("Lounge Voice Assistant Lounge"), exactly as happened on
every ESPHome-mode entity until 2026-08-16. This is a source-shape test
(reading `.py` files as text/AST, not importing them) so it runs without a
`homeassistant` install, matching every other pure-module test in this
package's `tests/` directory.

Replaces `controller/tests/test_deploy.py`'s removed
`test_entity_names_do_not_repeat_the_device_label`, which pinned the same
property against the (now-deleted) ESPHome `ListEntitiesMediaPlayerResponse`
path. The underlying HA behaviour is general, not protocol-specific, so the
property is asserted here against every entity in this package instead.
"""

import ast
from pathlib import Path

HACS = Path(__file__).resolve().parent.parent / "custom_components" / "echo_voice_satellite"


def _attr_name_assignments():
    """
    Yield (path, lineno, node) for every assignment to `_attr_name` — as a
    class body attribute (`_attr_name = "..."`) or an instance attribute
    (`self._attr_name = "..."`) — across every module in the integration.

    AST-based rather than a regex: a regex matching only the literal-string
    form would simply stop matching the moment someone switched to an
    f-string or `.format()` call, which is exactly the change that could
    smuggle a device label into a name — silently passing a test that only
    ever looked for quotes.
    """
    for path in sorted(HACS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                name = None
                if isinstance(target, ast.Name):
                    name = target.id
                elif isinstance(target, ast.Attribute):
                    name = target.attr
                if name == "_attr_name":
                    yield path, node.lineno, node.value


def test_entities_opt_into_ha_name_composition():
    """
    Every entity's displayed name depends on HA prefixing it with the device
    name — if the shared base ever stopped setting
    `_attr_has_entity_name = True`, every entity would need its full name
    spelled out by hand, and nothing here would catch a label creeping back
    in by hand-composition.
    """
    src = (HACS / "entities.py").read_text()
    assert "_attr_has_entity_name = True" in src, (
        "EchoCoordinatorEntity must set _attr_has_entity_name so HA composes "
        "'<device name> <entity name>' itself"
    )


def test_every_attr_name_is_a_plain_string_literal():
    """
    A plain string constant cannot reference the device's label — it is
    typed once, by us, and never touches `record.get('label')`,
    `self.device_id`, or any other per-device value. Anything else (an
    f-string, `.format()`, string concatenation with a variable) is capable
    of composing the label into the name, which is the exact bug class this
    guards against.
    """
    assignments = list(_attr_name_assignments())
    assert assignments, (
        "no _attr_name assignments found in "
        f"{HACS} — has every entity stopped setting one, or has the pattern "
        "used to set it changed?"
    )
    for path, lineno, value in assignments:
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{path.name}:{lineno}: _attr_name is not a plain string literal "
            f"({ast.dump(value)}) — a dynamically composed entity name is "
            f"exactly how a device label ends up baked into it twice"
        )


def test_no_attr_name_literal_contains_a_label_placeholder():
    """
    Belt-and-braces against a literal that itself contains a format
    placeholder left un-substituted by a refactor (e.g. someone starts an
    f-string conversion, forgets the leading `f`, and ships
    `"{label} Voice Assistant"` as an inert literal that nonetheless carries
    the intent to repeat the label).
    """
    for path, lineno, value in _attr_name_assignments():
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        text = value.value
        for marker in ("{label", "{device", "{self.label", "%s", "{}"):
            assert marker not in text, (
                f"{path.name}:{lineno}: _attr_name literal {text!r} looks "
                f"like an un-substituted format string referencing the "
                f"device — this is exactly how a label ends up repeated"
            )
