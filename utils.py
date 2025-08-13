import os
import json
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# -----------------------------------------------------------------------------
# Config (override via environment in your service/container)
# -----------------------------------------------------------------------------
PARSER_FUNCTION_NAME = os.getenv("DOCFILE_EXTRACTION_FUNCTION", "docfile-extraction")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")  # or None to use default Boto3 chain

# -----------------------------------------------------------------------------
# Public helpers you can call from your backend (DO NOT modify your Lambda entry)
# -----------------------------------------------------------------------------
def extract_text_from_binary(data: Union[bytes, bytearray, memoryview], *, filename: str = "document") -> str:
    """
    Sends in-memory file bytes to the 'docfile-extraction' Lambda and returns extracted text.
    Prefers Markdown; falls back to plain text. Assumes the parser Lambda uploads outputs to S3
    and returns s3:// URIs inside its 'artifacts' map.

    Raises:
        RuntimeError if the Lambda invocation fails or artifacts are not accessible.
    """
    manifest = extract_manifest_from_binary(data, filename=filename)
    docs = manifest.get("documents") or []
    if not docs:
        raise RuntimeError("Parser Lambda returned no documents.")

    artifacts = docs[0].get("artifacts") or {}
    # Prefer markdown, then text
    for key in ("markdown", "text"):
        uri = artifacts.get(key)
        if uri:
            content = _read_artifact(uri)
            if content is not None:
                return content

    raise RuntimeError(
        "No accessible artifacts returned. Ensure the parser Lambda uploads to S3 "
        "(set OUTPUT_S3_BUCKET) and returns s3:// URIs."
    )


def extract_manifest_from_binary(data: Union[bytes, bytearray, memoryview], *, filename: str = "document") -> Dict[str, Any]:
    """
    Same as above but returns the full manifest dict:
      { "documents": [ { input_name, artifacts{markdown/json/text/chunks}, status, ... } ] }
    """
    event = _build_event_from_bytes([(filename, bytes(data))])
    return _invoke_parser_lambda(PARSER_FUNCTION_NAME, AWS_REGION, event)

# -----------------------------------------------------------------------------
# Internal: Lambda invocation + S3/local artifact reading
# -----------------------------------------------------------------------------
def _build_event_from_bytes(files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
    """
    [(filename, data_bytes)] → event shape expected by docfile-extraction:
      Single:  {"file_name": "...", "content_b64": "..."}
      Multi:   {"files": [{"file_name": "...", "content_b64": "..."}, ...]}
    """
    items = [{"file_name": name, "content_b64": base64.b64encode(b).decode("ascii")} for name, b in files]
    if len(items) == 1:
        return {"file_name": items[0]["file_name"], "content_b64": items[0]["content_b64"]}
    return {"files": items}


def _invoke_parser_lambda(function_name: str, region_name: Optional[str], event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Invoke the parsing Lambda and return the parsed body dict.
    Your handler returns: {"statusCode":200,"body":"{\"documents\":[...]}"}.
    """
    try:
        client = boto3.client("lambda", region_name=region_name)
        resp = client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",  # sync
            Payload=json.dumps(event).encode("utf-8"),
        )

        payload_text = resp["Payload"].read().decode("utf-8", "replace")
        if "FunctionError" in resp:
            raise RuntimeError(f"Parser Lambda FunctionError={resp['FunctionError']}: {payload_text}")

        outer = json.loads(payload_text) if payload_text else {}
        body = outer.get("body")
        body_obj = json.loads(body) if isinstance(body, str) else body

        if not isinstance(body_obj, dict) or "documents" not in body_obj:
            raise RuntimeError(f"Unexpected parser Lambda response body: {body}")

        return body_obj

    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"AWS invoke failed: {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to decode parser Lambda response JSON: {e}") from e


def _read_artifact(uri_or_path: str) -> Optional[str]:
    """Read artifact from S3 (s3://bucket/key) or local path (if accessible)."""
    if uri_or_path.startswith("s3://"):
        return _s3_read_text(uri_or_path)

    # Local path fallback (generally not usable across containers/environments)
    try:
        p = Path(uri_or_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None


def _s3_read_text(s3_uri: str) -> Optional[str]:
    """Download S3 object and return as utf-8 text."""
    try:
        _, _, rest = s3_uri.partition("s3://")
        bucket, _, key = rest.partition("/")
        s3 = boto3.client("s3", region_name=AWS_REGION or None)
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None