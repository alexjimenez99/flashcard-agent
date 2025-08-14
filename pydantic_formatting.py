import re
import json
from typing import List, Optional, Literal, Annotated, Any, Type

from pydantic import BaseModel, Field, model_validator, ValidationError
from pydantic.config import ConfigDict

# Reusable constrained aliases (no call expressions in the type slot)
NonNegInt   = Annotated[int, Field(ge=0)]
PosInt      = Annotated[int, Field(ge=1)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
LangStr     = Annotated[str, Field(min_length=2)]  # BCP-47-ish
Conf01      = Annotated[float, Field(ge=0.0, le=1.0)]

HeadingLevel = Optional[Literal["H1", "H2", "H3", "H4"]]
Kind = Optional[Literal[
    "section","paragraph","list","example","quote","code","math",
    "figure","table","caption","footnote","reference","glossary","appendix"
]]


### Chunk Spitter Pydantic Objects ###
class Span(BaseModel):
    start: NonNegInt
    end: NonNegInt

class ListItemRange(BaseModel):
    start: PosInt
    end: PosInt

class Chunk(BaseModel):
    index: NonNegInt
    title: Optional[NonEmptyStr] = None
    heading_level: HeadingLevel = None
    language: LangStr
    span: Span
    text: NonEmptyStr

    # -------- OPTIONAL --------
    kind: Kind = None
    section_path: Optional[List[NonEmptyStr]] = None
    page_numbers: Optional[List[NonNegInt]] = None
    list_item_range: Optional[ListItemRange] = None

class DocStats(BaseModel):
    char_length: NonNegInt
    language: LangStr

class ChunkPayload(BaseModel):
    doc_stats: DocStats
    # Use Field(min_length=1) on the list to enforce non-empty
    chunks: Annotated[List[Chunk], Field(min_length=1)]

### Chunk Generator Pydantic Objects ###

# -------- Leaf objects --------
class SourceSpan(BaseModel):
    start: NonNegInt
    end: NonNegInt  # inclusive-exclusive by your convention

class TableData(BaseModel):
    columns: List[NonEmptyStr]
    rows: List[List[NonEmptyStr]]

    @model_validator(mode="after")
    def _validate_row_widths(self):
        col_count = len(self.columns)
        # If columns empty, allow only empty rows
        if col_count == 0:
            if any(len(r) != 0 for r in self.rows):
                raise ValueError("rows must be empty lists when columns is empty")
            return self
        # Otherwise, every row must match columns length
        for i, r in enumerate(self.rows):
            if len(r) != col_count:
                raise ValueError(f"rows[{i}] length {len(r)} != columns length {col_count}")
        return self

class Media(BaseModel):
    audio_text: Optional[NonEmptyStr] = None
    image_caption: Optional[NonEmptyStr] = None

class Extras(BaseModel):
    table_data: Optional[TableData] = None
    process_steps: Optional[List[NonEmptyStr]] = None
    media: Media = Field(default_factory=Media)

# -------- Card --------
CardType = Literal["basic", "cloze", "table", "process", "concept_check"]

class Card(BaseModel):
    type: CardType
    front: NonEmptyStr
    back: NonEmptyStr
    hint: Optional[NonEmptyStr] = None
    tags: List[NonEmptyStr] = Field(default_factory=list)
    source_span: SourceSpan
    difficulty: Annotated[int, Field(ge=1, le=5)]
    extras: Extras = Field(default_factory=Extras)

# -------- Top-level payload --------
class CardsPayload(BaseModel):
    stage: Literal["cards"]
    chunk_index: NonNegInt
    batch_index: NonNegInt
    cards: Annotated[List[Card], Field(min_length=1)]
    estimated_total_for_chunk: NonNegInt



### QA Check Model ####

class QAResult(BaseModel):
    traceability_ok: bool
    factual_ok: bool
    edits: List[NonEmptyStr] = Field(default_factory=list)

class AcceptedCard(Card):
    id: Optional[NonEmptyStr] = None
    qa: QAResult

# ---------- Rejected item ----------
RejectedReason = Literal[
    "schema","span","traceability","factual","language","pedagogy","content","duplicate","other"
]

class RejectedItem(BaseModel):
    original: Card
    reason: RejectedReason
    details: NonEmptyStr
    confidence: Conf01

# ---------- Summary ----------
class Summary(BaseModel):
    input_count: NonNegInt
    accepted_count: NonNegInt
    rejected_count: NonNegInt
    deduplicated: NonNegInt

# ---------- Top-level payload ----------
class QAReviewPayload(BaseModel):
    summary: Summary
    accepted: List[AcceptedCard] = Field(default_factory=list)
    rejected: List[RejectedItem] = Field(default_factory=list)



# ---------- Make all your models strict ----------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no unknown keys anywhere

# ---------- 1) Get text content from a Responses API object ----------
def responses_text(resp: Any) -> str:
    # Works with the Python SDK’s convenience; otherwise fall back.
    if hasattr(resp, "output_text") and resp.output_text:
        return resp.output_text

    parts = []
    for item in getattr(resp, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            # Typical fields: {"type":"output_text","text":"..."} or {"type":"input_text",...}
            if c.get("type") in ("output_text", "text"):
                t = c.get("text") or c.get("content") or ""
                parts.append(t)
    return "\n".join(parts).strip()

# ---------- 2) Extract JSON from possibly-messy text ----------
_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL
)

def extract_json_block(s: str) -> str:
    s = s.strip()
    # a) try direct parse
    try:
        json.loads(s)
        return s
    except Exception:
        pass

    # b) try fenced ```json ... ``` or ``` ... ```
    m = _JSON_BLOCK_RE.search(s)
    if m:
        return m.group(1)

    # c) last-resort: grab the first top-level {...} (brace matching)
    first = s.find("{")
    last  = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = s[first:last+1]
        # quick sanity check
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass

    # d) try array fallback
    first = s.find("[")
    last  = s.rfind("]")
    if first != -1 and last != -1 and last > first:
        candidate = s[first:last+1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass

    raise ValueError("No valid JSON block found in model output.")

# ---------- 3) Optional deterministic 'repair' (no extra LLM call) ----------
def basic_json_repair(s: str) -> Optional[str]:
    # Common issues: smart quotes, trailing commas, BOM
    repaired = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    repaired = repaired.replace("\ufeff", "")
    # Drop trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    try:
        json.loads(repaired)
        return repaired
    except Exception:
        return None

# ---------- 4) Enforce with Pydantic ----------
def coerce_and_validate(resp: Any, model: Type[StrictModel]) -> StrictModel:
    text = responses_text(resp)
    raw_json = None

    try:
        block = extract_json_block(text)
        raw_json = block
    except ValueError:
        # try naive repair on whole text
        fixed = basic_json_repair(text)
        if not fixed:
            raise

        raw_json = extract_json_block(fixed)

    obj = json.loads(raw_json)

    try:
        return model.model_validate(obj)
    except ValidationError as e:
        # Raise with a clean, actionable message
        # (paths + reasons help you log and iterate)
        raise ValueError(f"Pydantic validation failed: {e}") from e