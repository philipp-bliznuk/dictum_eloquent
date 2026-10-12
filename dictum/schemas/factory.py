import typing as t

import pydantic


def _optional(*, info: pydantic.fields.FieldInfo) -> tuple[t.Any, t.Any]:
    annotation: t.Any = info.annotation
    if info.metadata:
        annotation = t.Annotated[annotation, *info.metadata]
    return annotation, None


def partial_model(*, base: type[pydantic.BaseModel], name: str) -> type[pydantic.BaseModel]:
    """Derive a PATCH schema: every field of `base` becomes optional-to-send, constraints and nullability kept.

    Fields keep their original annotation, so `null` is accepted only where `base` already allows `None`;
    unknown keys are rejected; callers must read the result with `model_dump(exclude_unset=True)`.

    Args:
        base: Source model whose fields are copied.
        name: Name of the generated class.
    """
    fields: dict[str, t.Any] = {field_name: _optional(info=info) for field_name, info in base.model_fields.items()}
    return pydantic.create_model(name, __config__=pydantic.ConfigDict(extra="forbid"), **fields)
