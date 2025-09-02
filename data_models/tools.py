from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


class OperationItem(BaseModel):
    name: str
    summary: str | None = None
    category: str | None = None
    score: int
    inputType: str | None = None
    outputType: str | None = None
    args: Optional[List[Dict[str, Any]]] = None


class SearchOpsOut(BaseModel):
    total: int
    items: list[OperationItem]
    truncated: bool | None = None


class RecipeOp(BaseModel):
    op: str
    args: Dict[str, Any] = Field(default_factory=dict)


class BakeRecipeRequest(BaseModel):
    input_data: str
    recipe: List[RecipeOp]


class BakeRecipeResponse(BaseModel):
    ok: bool
    output: Optional[str] = None
    type: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ArgItem(BaseModel):
    name: str
    type: str
    required: bool = False
    options: Optional[List[str]] = None
    options_total: Optional[int] = None


class GetOperationArgsOut(BaseModel):
    ok: bool
    op: Optional[str] = None
    args: List[ArgItem] = Field(default_factory=list)
    error: Optional[str] = None


class GetOperationArgsIn(BaseModel):
    op: str = Field(..., description="Exact operation name")
    compact: bool = Field(True, description="If true, return slugified options; otherwise full labels")


class BatchBakeRecipeResponse(BaseModel):
    results: List[BakeRecipeResponse]


class ProbeIn(BaseModel):
    raw_input: str = Field(..., description="Data to probe; plain text or base64/hex etc.")


class ProbeOut(BaseModel):
    ok: bool
    output: Optional[str] = None
    recipe: Optional[List[RecipeOp]] = None
    error: Optional[str] = None


class SuggestionItem(BaseModel):
    index: int
    op: str
    candidates: Optional[List[str]] = None
    missingArgs: Optional[List[str]] = None


class ValidateRecipeIn(BaseModel):
    recipe: List[RecipeOp] = Field(..., description="Array of {op, args} steps to validate")


class ValidateRecipeOut(BaseModel):
    ok: bool
    errors: List[str] = Field(default_factory=list)
    suggestions: List[SuggestionItem] = Field(default_factory=list)
    normalized: Optional[List[RecipeOp]] = None
