import os
import re
import json
import asyncio
import base64
from io import BytesIO
import cgi
from typing import Any, Dict, List, Optional, Tuple
from hashlib import sha256


from supabase import create_client, Client
from datetime import datetime
from openai import OpenAI

# --- import your agents (from your module) ---
from agents import ChunkSplitterAgent, FlashcardGeneratorAgent, FlashcardQualityAgent, ContentInstructionAgent
from utils import extract_manifest_from_binary, extract_text_from_binary


# ---- Helpers ----


def _guess_file_type(filename: Optional[str], declared: Optional[str], content: bytes) -> str:
    if declared:
        return declared
    if filename and filename.lower().endswith(".pdf"):
        return "application/pdf"
    if content.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"

def _count_pdf_pages_fast(pdf_bytes: bytes) -> Optional[int]:
    """
    Lightweight heuristic for PDF page count (good enough for mobile/web preview).
    Avoids heavy libs. Returns None if not a PDF or can’t infer.
    """
    if not pdf_bytes.startswith(b"%PDF"):
        return None
    # look only at a slice for speed on Lambda
    sample = pdf_bytes[:5_000_000]
    try:
        # Count '/Type /Page' not followed by 's' (avoid '/Pages').
        return len(re.findall(br"/Type\s*/Page([^s]|$)", sample)) or None
    except Exception:
        return None

def _get_body_and_headers(event) -> Tuple[bytes, Dict[str, str]]:
    if event.get("isBase64Encoded"):
        body = base64.b64decode(event["body"])
    else:
        body = event["body"].encode() if isinstance(event.get("body"), str) else (event.get("body") or b"")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return body, headers


def _parse_multipart(body: bytes, content_type: str) -> Dict[str, Any]:
    """
    Parses multipart/form-data using cgi.FieldStorage.
    Returns a dict of fields; file fields include:
      { "filename": str, "content": bytes, "type": str }
    - Text fields become strings.
    - If a field occurs multiple times, value is a list (preserves all).
    """
    env = {
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)),
    }
    form = cgi.FieldStorage(fp=BytesIO(body), environ=env, keep_blank_values=True)

    fields: Dict[str, Any] = {}
    if form.list:
        for field in form.list:
            key = field.name
            if field.filename:  # file upload
                value = {
                    "filename": field.filename,
                    "content": field.file.read() if hasattr(field.file, "read") else field.value.encode(),
                    "type": field.type,
                }

                # Convert Binary Text to Text with LlamaParse
            else:
                value = field.value  # string

            # support repeated keys -> list
            if key in fields:
                if isinstance(fields[key], list):
                    fields[key].append(value)
                else:
                    fields[key] = [fields[key], value]
            else:
                fields[key] = value
    return fields

def _extract_input_text(fields: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Attempts to extract the user-provided text/JS from the form.
    Priority: 'text' field -> 'js' field -> first file (as text).
    Returns (text, deck_id).
    """
    deck_id = fields.get("deck_id") or None

                    # value['content'] = binary_to_text(value['content'])

    # Direct text fields
    if isinstance(fields.get("text"), str) and fields["text"].strip():
        return fields["text"], deck_id
    if isinstance(fields.get("js"), str) and fields["js"].strip():
        return fields["js"], deck_id

    # File upload fallback
    for key, val in fields.items():
        if isinstance(val, dict) and "content" in val:
            try:
                return val["content"], deck_id
            except Exception:
                continue

    return "", deck_id

def _cors_headers(origin: str) -> Dict[str, str]:
    allowed = ['http://localhost:8081', 'https://www.aibuddies.io']
    cors_origin = origin if origin in allowed else 'null'
    return {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
        "Content-Type": "application/json",
    }

def _ensure_list(obj, key: str) -> list:
    v = obj.get(key, [])
    return v if isinstance(v, list) else []

# ---- Pipeline ----

async def _run_pipeline(
    *,
    input_text: str,
    supabase: Client,
    gpt_api_key: str,
    model: str,
    user_id: str,
    jwt_token: str,
    deck_id: Optional[str],
) -> Dict[str, Any]:
    """
    Runs ChunkSplitter → Generator → QA. If QA rejects too many, loop back once to Generator.
    Inserts accepted cards into Supabase and returns a structured response.
    """

    # Initialize agents
    
    chunker = ChunkSplitterAgent(
        system_prompt="chunk_splitting_agent",
        api_key=gpt_api_key,
        uuid=user_id,
        jwt_token=jwt_token,
        model=model,
    )

    content_instructor = ContentInstructionAgent(
        api_key=gpt_api_key,
        user_id=user_id,
        jwt_token=jwt_token,
        model=model
    )

    
    generator = FlashcardGeneratorAgent(
        system_prompt="flashcard_generator_agent",
        api_key=gpt_api_key,
        uuid=user_id,
        jwt_token=jwt_token,
        model=model,
    )
    
    qa = FlashcardQualityAgent(
        system_prompt="flashcard_quality_agent",
        api_key=gpt_api_key,
        uuid=user_id,
        jwt_token=jwt_token,
        model=model,
    )

    # 1) Chunk splitting
    chunk_out = chunker.run(
        user_id=user_id,
        jwt_token=jwt_token,
        system_role_id=6,
        supabase_client=supabase,
        message=input_text,
    )

    instructions = content_instructor.run(
        user_id=user_id,
        jwt_token=jwt_token,
        system_role_id=9,
        supabase_client=supabase,
        message=input_text
    )

    # 1) Run chunking + instruction steps concurrently
    chunk_out, instructions = await asyncio.gather(
            chunker.run(
                user_id=user_id,
                jwt_token=jwt_token,
                system_role_id=6,
                supabase_client=supabase,
                message=input_text,
            ),
            content_instructor.run(
                user_id=user_id,
                jwt_token=jwt_token,
                system_role_id=9,
                supabase_client=supabase,
                message=input_text,
            ),
    )

    # Expect either dict with "chunks" or raw text; guard:
    chunks = (chunk_out or {}).get("chunks") if isinstance(chunk_out, dict) else None

    if not chunks:
        # Fallback: single chunk covering the whole text
        chunks = [{
            "index": 0,
            "title": None,
            "heading_level": None,
            "language": "und",
            "span": {"start": 0, "end": len(input_text)},
            "text": input_text
        }]

    print('candiate cards')
    # 2) Generation across chunks → collect all candidate cards
    candidate_cards: List[Dict[str, Any]] = []
    for ch in chunks:
        ch_text = ch.get("text", "")
        # You may pass chunk metadata in the message as JSON:
        gen_message = json.dumps({
            "chunk_index": ch.get("index", 0),
            "chunk_span": ch.get("span", {"start": 0, "end": len(input_text)}),
            "text": ch_text,
            "content_instructions": instructions
        })

        gen_out = await generator.run(
            user_id=user_id,
            jwt_token=jwt_token,
            system_role_id=7,
            supabase_client=supabase,
            message=gen_message,
        )

        print('gen out', gen_out)
        # Generator returns a dict per batch in our implementation; if you stream batches,
        # you’d loop here. For now assume a single dict with "cards" or a list of batches.
        if isinstance(gen_out, dict) and "cards" in gen_out:
            candidate_cards.extend(_ensure_list(gen_out, "cards"))
        elif isinstance(gen_out, list):  # list of batches
            for batch in gen_out:
                candidate_cards.extend(_ensure_list(batch, "cards"))

    # 3) QA/Dedupe
    qa_payload = {
        "source_text": input_text,
        "cards": candidate_cards
    }



    qa_out = await qa.run(
        user_id=user_id,
        jwt_token=jwt_token,
        system_role_id=8,
        supabase_client=supabase,
        message=json.dumps(qa_payload),
    )

    print('qa out', qa_out)

    accepted_cards = []
    rejected_count = 0
    if isinstance(qa_out, dict): 
        accepted_cards = _ensure_list(qa_out, "accepted")
        summary        = qa_out.get("summary") or {}
        rejected_count = int(summary.get("rejected_count", 0))

    
    print('acccepted', accepted_cards)
    # 4) Conditional loop back to generator once if QA says it needs work
    # Criterion: if more than 30% rejected, try one refinement pass.
    if candidate_cards and rejected_count / max(1, len(candidate_cards)) > 0.3:
        # Provide QA feedback to generator as context
        feedback = {
            "instruction": "Regenerate/improve cards addressing QA feedback. Avoid duplicates; ensure traceability to spans.",
            "qa_summary": qa_out.get("summary", {}),
            "examples_of_issues": _ensure_list(qa_out, "rejected")[:5],  # sample a few issues
        }

        improved_candidates: List[Dict[str, Any]] = []
        for ch in chunks:
            gen_message = json.dumps({
                "chunk_index": ch.get("index", 0),
                "chunk_span": ch.get("span", {"start": 0, "end": len(input_text)}),
                "text": ch.get("text", ""),
                "feedback": feedback
            })
            gen_out2 = await generator.run(
                user_id=user_id,
                jwt_token=jwt_token,
                system_role_id=7,
                supabase_client=supabase,
                message=gen_message,
            )
            if isinstance(gen_out2, dict) and "cards" in gen_out2:
                improved_candidates.extend(_ensure_list(gen_out2, "cards"))
            elif isinstance(gen_out2, list):
                for batch in gen_out2:
                    improved_candidates.extend(_ensure_list(batch, "cards"))

        qa_payload2 = {
            "source_char_len": len(input_text),
            "cards": improved_candidates
        }
        qa_out2 = await qa.run(
            user_id=user_id,
            jwt_token=jwt_token,
            system_role_id=8,
            supabase_client=supabase,
            message=json.dumps(qa_payload2),
        )
        if isinstance(qa_out2, dict):
            accepted_cards = _ensure_list(qa_out2, "accepted")

    # Create Text Encoded Hash
    source_hash = sha256(input_text.encode("utf-8")).hexdigest()

    # Try to reuse an identical source for this user (optional)
    existing = supabase.table("flashcard_sources").select("id").eq("user_id", user_id).eq("hash", source_hash).limit(1).execute()
    if existing.data:
        source_id = existing.data[0]["id"]
    else:
        src_res = supabase.table("flashcard_sources").insert({
            "user_id": user_id,
            "title": "Uploaded text",
            "text": input_text,           # omit if huge; or store excerpt
            "hash": source_hash,
            "metadata": {"language": "und"}
        }).execute()

        source_id = src_res.data[0]["id"]


    # 5) Persist to Supabase
    # Ensure or create a deck if not provided:
    if not deck_id:
        # print('deck this')
        deck_name = f"Generated Deck {datetime.now()}"
        deck_res = supabase.table("flashcard_decks").insert({
            "user_id": user_id,
            "deck_name": deck_name,
            "source_id": source_id,
        }).execute()

        deck_id = (deck_res.data[0]["id"] if deck_res.data else None)

    # Prepare batch insert for 'cards' table (align to your schema)
    rows = []
    print('qa', qa_out)
    print('card type', type(accepted_cards))
    print('cards', accepted_cards)

    for c in accepted_cards:
        rows.append({
            "user_id": user_id, 
            "deck_id": deck_id,
            "type": c.get("type", "basic"),
            "front": c.get("front", ""),
            "back": c.get("back", ""),
            "hint": c.get("hint"),
            "tags": c.get("tags", []),
            "difficulty": c.get("difficulty", 2),
            "source_id": source_id,
            "source_span": c.get("source_span"),
            "extras": c.get("extras", {}),
        })
    inserted = []
    if rows:
        ins = supabase.table("flashcards").insert(rows).execute()
        inserted = ins.data or []

    return {
        "deck_id": deck_id,
        "inserted_count": len(inserted),
        "accepted_preview": accepted_cards[:10],  # preview for frontend
    }


# ---- Lambda Entry ----
def lambda_function(event, context):
    # ── Env & clients ───────────────────────────────────────────────────────────
    gpt_api_key      = os.environ.get("GPT_API_KEY")
    model            = os.environ.get("GPT_MODEL")
    supabase_url     = os.environ.get("SUPABASE_URL")
    supabase_anon    = os.environ.get("SUPABASE_PUBLISHABLE_KEY")       # user-scoped (RLS)
    # supabase_service = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")     # admin (bypass RLS)

    # Parse body/headers early
    body, headers = _get_body_and_headers(event)
    origin = (headers.get("origin") or headers.get("Origin") or "").strip()

    # CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 200, "headers": _cors_headers(origin), "body": ""}

    content_type = headers.get("content-type") or headers.get("content_type") or ""

    # print(content_type)
    if content_type.startswith("multipart/form-data"):
        fields = _parse_multipart(body, content_type)

        # 👉 NEW: capture action (fallback to 'mode' for older clients)
        action = (fields.get("action") or fields.get("mode") or "").strip().lower()

        # Keep your existing text extraction
        input_text, deck_id = _extract_input_text(fields)

        file_field = fields.get("file")

        filename = file_field.get("filename")
        content  = file_field.get("content") or b""
        declared = file_field.get("type")

        # 👉 NEW: short-circuit for metadata
        if action == "metadata":
            if not isinstance(file_field, dict):
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"error": "Missing 'file' for metadata"}),
                }
            
            ftype    = _guess_file_type(filename, declared, content)
            page_count = _count_pdf_pages_fast(content) if ftype == "application/pdf" else None

            return {
                "statusCode": 200,
                "headers": _cors_headers(origin),
                "body": json.dumps({"ok": True, "type": ftype, "pageCount": page_count}),
            }
        

        # If no action provided on multipart, default to generate (backward compatibility)
        elif action == 'generate':
            input_text = extract_text_from_binary(input_text, filename)


    elif content_type.startswith("application/json"):
        try:
            data = json.loads(body.decode() if isinstance(body, bytes) else body)
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    data = {"text": data}
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": _cors_headers(origin),
                "body": json.dumps({"error": "Invalid JSON body"}),
            }

        # 👉 NEW: capture action (default to generate)
        action = (data.get("action") or data.get("mode") or "generate").strip().lower()

        # In JSON case, expect keys directly in payload
        input_text = data.get("text", "")
        deck_id = data.get("deck_id", "")

    else:
        return {
            "statusCode": 400,
            "headers": _cors_headers(origin),
            "body": json.dumps({"error": "Content-Type must be multipart/form-data or application/json"}),
        }
    
    


    # 👉 NEW: only enforce when NOT metadata
    if (locals().get("action") or "generate") != "metadata":
        if not input_text.strip():
            return {
                "statusCode": 400,
                "headers": _cors_headers(origin),
                "body": json.dumps({"error": "No text/JS content found in request"}),
            }
        
    # ── Auth context (upstream Lambda authorizer already did asymmetric verification) ──
    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {}) or {}
    user_id = auth_ctx.get("sub") or auth_ctx.get("user_id") or "anonymous"

    # Prefer token from authorizer; fall back to Authorization header
    access_token = (
        auth_ctx.get("access_token")
        or auth_ctx.get("token")
        or (headers.get("authorization") or headers.get("Authorization") or "").replace("Bearer ", "").strip()
    )


    # Create Supabase clients:
    #  1) User-scoped client (RLS enforced) -> pass to pipeline
    #  2) Admin client (service role)       -> keep if you need privileged ops
    supabase_user: Client  = create_client(supabase_url, supabase_anon)

    # Attach JWT to user client so PostgREST runs under user's RLS context
    if access_token:
        try:
            supabase_user.postgrest.auth(access_token)
        except Exception:
            # Older client fallback
            try:
                supabase_user.auth.set_session(access_token=access_token, refresh_token="")
            except Exception:
                pass

    # ── Main flow ───────────────────────────────────────────────────────────────
    try:
        result = asyncio.run(
            _run_pipeline(
                input_text=input_text,
                supabase=supabase_user,   # run DB ops under user's RLS
                gpt_api_key=gpt_api_key,
                model=model,
                user_id=user_id,
                jwt_token=access_token,   # pass through for downstream auditing if needed
                deck_id=deck_id,
            )
        )

        return {
            "statusCode": 200,
            "headers": _cors_headers(origin),
            "body": json.dumps({"message": "success", "result": result}),
        }

    except Exception as e:
        print("Pipeline error:", e)
        return {
            "statusCode": 500,
            "headers": _cors_headers(origin),
            "body": json.dumps({"error": "Internal Server Error", "details": str(e)}),
        }
