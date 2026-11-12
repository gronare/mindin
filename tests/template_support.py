from __future__ import annotations

from mindin import note_templates
from mindin.schema import Schema


def filled(schema: Schema, kind: str, first: str = "Text.") -> str:
    template = schema.template(kind)
    assert template is not None
    texts = {section.key: "Text." for section in template.sections if section.required}
    texts[template.sections[0].key] = first
    return note_templates.written_body(template, schema.language, texts)
