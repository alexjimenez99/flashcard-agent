import os
import json
import gzip
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
ALLOW_REMOTE = os.getenv("ALLOW_REMOTE_ARTIFACTS", "false").lower() in ("true")


# -----------------------------------------------------------------------------
# Public helpers you can call from your backend (DO NOT modify your Lambda entry)
# -----------------------------------------------------------------------------
def extract_text_from_binary(data: Union[bytes, bytearray, memoryview], filename: str = "document") -> str:
    """
    Sends in-memory file bytes to the parser Lambda and returns extracted text.

    Preferred order (no S3 access by default):
      1) Inline artifacts: artifacts.inline_markdown / artifacts.inline_text
         (also supports base64, gzipped, data: URIs, or chunk arrays)
      2) If ALLOW_REMOTE_ARTIFACTS=1:
           - http(s) URLs (incl. presigned S3 over HTTPS)
           - s3:// URIs
      3) Local path fallback (rare across containers)

    Raises:
        RuntimeError if no accessible artifact is available.
    """
    manifest = extract_manifest_from_binary(data, filename=filename)
    docs = manifest.get("documents") or []

    
    if not docs:
        raise RuntimeError("Parser Lambda returned no documents.")
    

    artifacts: Dict[str, Any] = docs[0].get("artifacts") or {}

    markdown_text = artifacts['markdown']
    json_text = artifacts['json']
    plain_text = artifacts['text']

    return markdown_text

    # 1) Inline-first (no network)
    # inline = _read_inline_artifact(artifacts)
    # if inline is not None and inline.strip():
    #     return inline

    # # 2) Remote (opt-in)
    # if ALLOW_REMOTE:
    #     for key in ("markdown", "text"):
    #         uri = artifacts.get(key)
    #         if isinstance(uri, str) and uri:
    #             content = _read_artifact(uri)
    #             if content is not None and content.strip():
    #                 return content

    # # 3) Local path fallback (best-effort; often not shared across Lambdas)
    # for key in ("markdown", "text"):
    #     p = artifacts.get(key)
    #     if isinstance(p, str):
    #         try:
    #             path = Path(p)
    #             if path.exists() and path.is_file():
    #                 return path.read_text(encoding="utf-8", errors="replace")
    #         except Exception:
    #             pass

    # Nothing accessible
    # msg = (
    #     "No accessible artifacts were returned.\n"
    #     "- Ensure your parser Lambda includes inline content (e.g., artifacts.inline_markdown or artifacts.inline_text), "
    #     "OR set ALLOW_REMOTE_ARTIFACTS=1 to enable remote fetches of http(s)/s3 URIs."
    # )
    # raise RuntimeError(msg)


def extract_manifest_from_binary(data: Union[bytes, bytearray, memoryview], filename: str = "document") -> Dict[str, Any]:
    """
    Same as above but returns the full manifest dict:
      { "documents": [ { input_name, artifacts{markdown/json/text/chunks}, status, ... } ] }
    """
    event = _build_event_from_bytes([(filename, bytes(data))])
    return _invoke_parser_lambda(PARSER_FUNCTION_NAME, AWS_REGION, event)

# -----------------------------------------------------------------------------
# Internal: Lambda invocation + S3/local artifact reading
# -----------------------------------------------------------------------------

def _read_inline_artifact(artifacts: Dict[str, Any]) -> Optional[str]:
    """
    Return inline text if present. Supports several shapes:
      - artifacts.inline_markdown / artifacts.inline_text (plain string)
      - artifacts.inline_markdown_b64 / inline_text_b64 (base64-encoded)
      - artifacts.inline_markdown_b64_gzip / inline_text_b64_gzip (base64 gzipped)
      - artifacts.markdown_data_uri / text_data_uri (data: URL)
      - artifacts.chunks: ["...", "..."]  -> joined with double newlines for quick use
    """
    # 1) Plain inline strings
    for k in ("inline_markdown", "inline_text", "markdown_inline", "text_inline"):
        v = artifacts.get(k)
        if isinstance(v, str) and v:
            return v

    # 2) data: URIs
    for k in ("markdown_data_uri", "text_data_uri"):
        v = artifacts.get(k)
        if isinstance(v, str) and v.startswith("data:"):
            parsed = _read_data_uri(v)
            if parsed is not None:
                return parsed

    # 3) base64 variants
    for k in ("inline_markdown_b64", "inline_text_b64"):
        v = artifacts.get(k)
        if isinstance(v, str):
            try:
                return base64.b64decode(v).decode("utf-8", errors="replace")
            except Exception:
                pass

    # 4) base64 gzipped variants
    for k in ("inline_markdown_b64_gzip", "inline_text_b64_gzip"):
        v = artifacts.get(k)
        if isinstance(v, str):
            try:
                raw = base64.b64decode(v)
                return gzip.decompress(raw).decode("utf-8", errors="replace")
            except Exception:
                pass

    # 5) chunk arrays (fallback: stitch together)
    chunks = artifacts.get("chunks")
    if isinstance(chunks, list) and all(isinstance(x, str) for x in chunks):
        return "\n\n".join(chunks)

    return None

def _read_data_uri(data_uri: str) -> Optional[str]:
    """
    Parse simple data: URIs, e.g., data:text/plain;base64,SGVsbG8=
    """
    try:
        # split header and payload
        if "," not in data_uri:
            return None
        header, payload = data_uri.split(",", 1)
        is_b64 = ";base64" in header.lower()
        if is_b64:
            return base64.b64decode(payload).decode("utf-8", errors="replace")
        return payload  # URL-decoding is usually not necessary for simple cases
    except Exception:
        return None



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
    """
    Remote reader (only used if ALLOW_REMOTE_ARTIFACTS=1).
    Supports: http(s) and s3://, plus best-effort local path.
    """
    if uri_or_path.startswith(("http://", "https://")):
        return _http_read_text(uri_or_path)
    if uri_or_path.startswith("s3://"):
        return _s3_read_text(uri_or_path)  # will raise if boto3 missing
    # Local path (rarely useful cross-container)
    try:
        p = Path(uri_or_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None

def _http_read_text(url: str) -> Optional[str]:
    # stdlib only to avoid extra deps
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception:
        return None
    
def _s3_read_text(s3_uri: str) -> Optional[str]:
    """
    s3://bucket/key -> text (only if ALLOW_REMOTE_ARTIFACTS=1 and boto3 installed).
    """
    try:
        import boto3  # local import to avoid hard dep when not used
    except Exception:
        raise RuntimeError("boto3 not installed, and S3 access was requested.")

    bucket, key = _split_s3_uri(s3_uri)
    client = boto3.client("s3")
    obj = client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8", errors="replace")

def _split_s3_uri(s3_uri: str) -> tuple[str, str]:
    assert s3_uri.startswith("s3://")
    without = s3_uri[len("s3://"):]
    parts = without.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    return parts[0], parts[1]