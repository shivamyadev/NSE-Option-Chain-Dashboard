# supabase_helper.py
import os
import json
import tempfile
from io import BytesIO
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, List, Dict, Any

try:
    import streamlit as st  # optional, used for warnings in UI
except Exception:
    st = None

from supabase import create_client


def _load_config_from_env_or_secrets():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    bucket = os.environ.get("SUPABASE_BUCKET")

    if (not url or not key) and st:
        try:
            url = url or st.secrets.get("SUPABASE_URL")
            key = key or st.secrets.get("SUPABASE_KEY")
            bucket = bucket or st.secrets.get("SUPABASE_BUCKET", "public")
        except Exception:
            pass

    if not url or not key:
        raise RuntimeError("Supabase config missing. Set SUPABASE_URL and SUPABASE_KEY in env or st.secrets.")

    if not bucket:
        bucket = "public"

    return url, key, bucket


def get_client(use_service_role: bool = False):
    if use_service_role:
        url = os.environ.get("SUPABASE_URL") or (st.secrets.get("SUPABASE_URL") if st else None)
        key = os.environ.get("SERVICE_ROLE_KEY") or (st.secrets.get("SERVICE_ROLE_KEY") if st else None)
        bucket = os.environ.get("SUPABASE_BUCKET") or (st.secrets.get("SUPABASE_BUCKET") if st else None)
        if not url or not key:
            raise RuntimeError("Service role key not configured (SERVICE_ROLE_KEY).")
    else:
        url, key, bucket = _load_config_from_env_or_secrets()

    client = create_client(url, key)
    return client, bucket


def _get_storage_client(client):
    storage_attr = getattr(client, "storage", None)
    if storage_attr is None:
        raise RuntimeError("Supabase client has no 'storage' attribute. Check SDK version.")
    try:
        # Some SDKs: client.storage() returns storage client
        return storage_attr() if callable(storage_attr) else storage_attr
    except Exception:
        # fallback: use attribute directly
        return storage_attr


def upload_json_payload(payload: dict, symbol: str, expiry: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Robust upload: attempts multiple upload methods for SDK compatibility.
    Returns (file_path, public_url_or_none).
    Raises RuntimeError on unrecoverable fail with details.
    """
    client, bucket = get_client(use_service_role=False)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    expiry_safe = (str(expiry) if expiry else "noexpiry").replace(" ", "_")
    symbol_safe = str(symbol).replace("/", "_").replace("\\", "_").upper()
    path = f"fetched/{symbol_safe}/{expiry_safe}/{ts}.json"

    json_bytes = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")

    # get storage client
    storage = _get_storage_client(client)
    if not hasattr(storage, "from_"):
        raise RuntimeError("Storage client incompatible: missing from_ method.")

    bucket_client = storage.from_(bucket)

    last_exc = None
    # Try 1: upload raw bytes (some SDKs accept bytes)
    try:
        bucket_client.upload(path, json_bytes)
        # success
        urlobj = _safe_get_public_url(bucket_client, path)
        return path, urlobj
    except Exception as e:
        last_exc = e

    # Try 2: upload BytesIO (some SDKs accept file-like)
    try:
        bucket_client.upload(path, BytesIO(json_bytes))
        urlobj = _safe_get_public_url(bucket_client, path)
        return path, urlobj
    except Exception as e:
        last_exc = e

    # Try 3: write to temp file and pass local path (works for SDKs expecting file path)
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp_path = tmp.name
        tmp.write(json_bytes)
        tmp.flush()
        tmp.close()
        # some SDKs require a file path string
        bucket_client.upload(path, tmp_path)
        # success
        urlobj = _safe_get_public_url(bucket_client, path)
        return path, urlobj
    except Exception as e:
        last_exc = e
    finally:
        # cleanup temp file if it exists
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

    # All attempts failed -> raise readable error
    err_msg = f"Upload failed for path '{path}'. Last exception: {last_exc}"
    if st:
        st.warning(f"Warning: upload to storage failed: {last_exc}")
    raise RuntimeError(err_msg)


def _safe_get_public_url(bucket_client, path: str) -> Optional[str]:
    """
    Return a public URL if available; handle different return shapes.
    """
    try:
        urlobj = bucket_client.get_public_url(path)
    except Exception:
        return None
    if isinstance(urlobj, dict):
        return urlobj.get("publicURL") or urlobj.get("publicUrl")
    if isinstance(urlobj, str):
        return urlobj
    return None


def store_fetch_record(
    payload: dict,
    symbol: str,
    expiry: Optional[str] = None,
    save_file: bool = True,
    user_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> Any:
    client, bucket = get_client(use_service_role=False)

    file_path = None
    file_url = None
    if save_file:
        try:
            file_path, file_url = upload_json_payload(payload, symbol, expiry)
        except Exception as e:
            # warn in UI but continue to attempt DB insert without file
            if st:
                st.warning(f"Warning: upload to storage failed: {e}")
            file_path, file_url = None, None

    row = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "expiry": expiry,
        "payload": payload,
        "file_path": file_path,
        "file_url": file_url,
        "user_id": user_id,
        "owner": owner,
    }
    # remove None keys to avoid unknown-column or null-column issues
    row = {k: v for k, v in row.items() if v is not None}

    try:
        res = client.table("fetched_data").insert(row).execute()
    except Exception as e:
        raise RuntimeError(f"Supabase insert failed (exception): {e}")

    err = getattr(res, "error", None) or (res.get("error") if isinstance(res, dict) else None)
    if err:
        raise RuntimeError(f"Supabase insert error: {err}")

    return res


def delete_older_than(days: int = 30) -> Dict[str, Any]:
    client, bucket = get_client(use_service_role=True)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff_dt.isoformat()

    result = {"deleted_rows": 0, "deleted_files": 0, "errors": []}

    try:
        q = client.table("fetched_data").select("id,file_path").lt("fetched_at", cutoff_iso).execute()
        rows = getattr(q, "data", None) or []
    except Exception as e:
        result["errors"].append(str(e))
        return result

    if not rows:
        return result

    ids = []
    file_paths = []
    for r in rows:
        if not r:
            continue
        ids.append(r.get("id"))
        if r.get("file_path"):
            file_paths.append(r.get("file_path"))

    if file_paths:
        try:
            storage = _get_storage_client(client)
            storage.from_(bucket).remove(file_paths)
            result["deleted_files"] = len(file_paths)
        except Exception as e:
            result["errors"].append(str(e))

    if ids:
        try:
            client.table("fetched_data").delete().in_("id", ids).execute()
            result["deleted_rows"] = len(ids)
        except Exception as e:
            result["errors"].append(str(e))

    return result


def list_recent_by_owner(owner: str, limit: int = 100) -> List[Dict[str, Any]]:
    client, bucket = get_client(use_service_role=False)
    try:
        q = client.table("fetched_data").select("*").eq("owner", owner).order("fetched_at", desc=True).limit(limit).execute()
        return getattr(q, "data", q)
    except Exception:
        return []
