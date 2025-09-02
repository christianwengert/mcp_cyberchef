from typing import Any, Dict, List, Literal, Optional, Union, Annotated
from pydantic import BaseModel, Field, model_validator
import re


class EnumArgDef(BaseModel):
    type: Literal["enum"]
    name: str
    options: List[str] = Field(default_factory=list)
    required: bool = False


class NumberArgDef(BaseModel):
    type: Literal["number"]
    name: str
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None
    required: bool = False


class StringArgDef(BaseModel):
    type: Literal["string"]
    name: str
    required: bool = False


class BooleanArgDef(BaseModel):
    type: Literal["boolean"]
    name: str
    required: bool = False


class BytesArgDef(BaseModel):
    type: Literal["bytes"]
    name: str
    encodings: List[str] = Field(default_factory=list)
    required: bool = False


ArgDef = Annotated[
    Union[
        EnumArgDef, NumberArgDef, StringArgDef, BooleanArgDef, BytesArgDef],
    Field(discriminator="type")
]

_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A, "u": re.U}


def _flags(s: str) -> int:
    f = 0
    for ch in s:
        f |= _FLAG_MAP.get(ch, 0)
    return f


class RegexCheck(BaseModel):
    pattern: str
    flags: str = ""
    args: List[Any] = Field(default_factory=list)

    def compile(self) -> re.Pattern:
        return re.compile(self.pattern, _flags(self.flags))


class OperationDef(BaseModel):
    module: str
    description: Optional[str] = ""
    infoUrl: Optional[str] = Field(default=None, alias="infoUrl")
    inputType: str
    outputType: str
    args: List[ArgDef] = Field(default_factory=list)
    checks: List[RegexCheck] = Field(default_factory=list)

    model_config = dict(populate_by_name=True)

    def validate_args(self, provided: Dict[str, Any]) -> Dict[str, Any]:
        names = {a.name for a in self.args}
        unknown = set(provided) - names
        if unknown:
            raise ValueError(f"unknown args: {sorted(unknown)}")

        # Validate required args and types
        for a in self.args:
            has = a.name in provided
            if getattr(a, "required", False) and not has:
                raise ValueError(f"missing required arg: {a.name}")
            if not has:
                continue

            v = provided[a.name]
            if isinstance(a, EnumArgDef):
                if v not in a.options:
                    raise ValueError(f"{a.name} must be one of {a.options}, got {v!r}")
            elif isinstance(a, NumberArgDef):
                if not isinstance(v, (int, float)):
                    raise ValueError(f"{a.name} must be number")
                if a.min is not None and v < a.min:
                    raise ValueError(f"{a.name} < {a.min}")
                if a.max is not None and v > a.max:
                    raise ValueError(f"{a.name} > {a.max}")
            elif isinstance(a, StringArgDef):
                if not isinstance(v, str):
                    raise ValueError(f"{a.name} must be string")
            elif isinstance(a, BooleanArgDef):
                if not isinstance(v, bool):
                    raise ValueError(f"{a.name} must be boolean")
            elif isinstance(a, BytesArgDef):
                # Accept canonical object {value: <...>, encoding?: <...>} or raw bytes/str for backward compatibility.
                if isinstance(v, dict):
                    if "value" not in v:
                        raise ValueError(f"{a.name} must include 'value' when provided as an object")
                    enc = v.get("encoding")
                    if enc is not None:
                        if a.encodings and enc not in a.encodings:
                            raise ValueError(f"{a.name}.encoding must be one of {a.encodings}, got {enc!r}")
                        if not isinstance(enc, str):
                            raise ValueError(f"{a.name}.encoding must be string when provided")
                    # value can be str or bytes; deeper conversion handled elsewhere
                    val = v["value"]
                    if not isinstance(val, (str, bytes)):
                        raise ValueError(f"{a.name}.value must be string or bytes")
                elif not isinstance(v, (bytes, str)):
                    raise ValueError(f"{a.name} must be bytes/string or object with 'value' and optional 'encoding'")
        return provided


def load_definitions(operations):

    # operations is expected to be a list of operation dicts (as per utils/js/cyberchef_operations_definitions.json)
    op_registry: Dict[str, OperationDef] = {}
    for op_name, op_def in operations.items():
        # noinspection PyBroadException
        try:
            # Parse using Pydantic; infoUrl is supported via alias in OperationDef
            opdef = OperationDef.model_validate(op_def)
            # name = op_def.get("name")
            # if not name:
            #     raise ValueError("operation missing 'name'")
            op_registry[op_name] = opdef
        except Exception as e:
            print(f"Could not add operation: {op_def.get('name')}: {e}")

    class CyberChefRecipeOperation(BaseModel):
        op: str
        args: Dict[str, Any] = Field(default_factory=dict)

        @model_validator(mode="after")
        def _validate_against_catalog(self):
            d = op_registry.get(self.op)
            if not d:
                raise ValueError(f"unknown op {self.op!r}; allowed: {sorted(op_registry.keys())}")
            d.validate_args(self.args)
            return self

    return CyberChefRecipeOperation
