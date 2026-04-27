"""
Storage abstraction — uploads images to Azure Blob Storage in production,
falls back to local static/uploads/ in development.

Environment variables (set in .env for local, App Settings for Azure):
  AZURE_STORAGE_CONNECTION_STRING  — if set, cloud mode is active
  AZURE_STORAGE_CONTAINER          — blob container name (default: "pis-images")

Blobs are created PRIVATE. Use get_image_url() to generate short-lived SAS URLs
for serving images to the browser. Register it as a Jinja2 global in app.py.
"""
import os
from urllib.parse import urlparse


def store_image(local_path: str, _label: str = '') -> str:
    """
    Given a locally saved file path, either:
      - Upload to Azure Blob (private) and return the bare blob URL, or
      - Return the relative path for local static serving.

    'label' is used as a hint for the blob name.
    The returned URL is the permanent identifier — pass it through get_image_url()
    before rendering in templates to obtain a signed, time-limited SAS URL.
    """
    conn_str  = os.environ.get('AZURE_STORAGE_CONNECTION_STRING')
    container = os.environ.get('AZURE_STORAGE_CONTAINER', 'pis-images')

    if conn_str and os.path.exists(local_path):
        try:
            return _upload_to_azure(local_path, container, conn_str)
        except Exception as e:
            print(f"⚠️  Azure upload failed, falling back to local path: {e}")

    # Local fallback — return relative path under static/
    return _local_relative(local_path)


def get_image_url(path: str, expiry_hours: int = 4) -> str:
    """
    Convert a stored image path into a URL safe for browser use.

    - Azure blob URLs  → generate a short-lived SAS token (private blobs require this).
    - Local paths      → return unchanged (served by Flask's static handler).
    - Non-Azure https  → return unchanged (already public CDN / external URL).

    Use as a Jinja2 global: {{ get_image_url(product.image_path) }}
    """
    if not path:
        return ''

    conn_str = os.environ.get('AZURE_STORAGE_CONNECTION_STRING')
    if not conn_str or not path.startswith('https://'):
        return path

    # Only sign URLs that belong to our own storage account
    try:
        parsed = urlparse(path)
        parts = {k: v for item in conn_str.split(';') for k, v in [item.split('=', 1)] if '=' in item}
        account_name = parts.get('AccountName', '')
        if account_name and account_name not in parsed.netloc:
            return path  # External URL — don't try to sign

        return _generate_sas_url(path, conn_str, expiry_hours)
    except Exception as e:
        print(f"⚠️  SAS URL generation failed: {e}")
        return path


def _generate_sas_url(blob_url: str, conn_str: str, expiry_hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import generate_blob_sas, BlobSasPermissions

    parts = {k: v for item in conn_str.split(';') for k, v in [item.split('=', 1)] if '=' in item}
    account_name = parts.get('AccountName', '')
    account_key  = parts.get('AccountKey', '')
    if not account_name or not account_key:
        return blob_url

    parsed = urlparse(blob_url)
    path_parts = parsed.path.lstrip('/').split('/', 1)
    if len(path_parts) < 2:
        return blob_url
    container_name, blob_name = path_parts[0], path_parts[1]

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
    )
    return f"{blob_url}?{sas_token}"


def _upload_to_azure(local_path: str, container: str, conn_str: str) -> str:
    from azure.storage.blob import BlobServiceClient, ContentSettings

    filename  = os.path.basename(local_path)
    blob_name = f"uploads/{filename}"

    ext = os.path.splitext(filename)[1].lower()
    mime_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                '.gif': 'image/gif', '.webp': 'image/webp'}
    content_type = mime_map.get(ext, 'application/octet-stream')

    client      = BlobServiceClient.from_connection_string(conn_str)
    container_c = client.get_container_client(container)

    # Create container as PRIVATE (no public_access) — blobs served via SAS tokens
    try:
        container_c.create_container()
    except Exception:
        pass  # Already exists

    blob_client = container_c.get_blob_client(blob_name)
    with open(local_path, 'rb') as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type)
        )

    # Clean up local temp file only after confirmed successful upload
    try:
        os.remove(local_path)
    except OSError:
        pass

    url = blob_client.url
    print(f"☁️  Uploaded to Azure (private): {url}")
    return url


def _local_relative(local_path: str) -> str:
    """Convert an absolute local path like .../static/uploads/foo.jpg → uploads/foo.jpg"""
    if 'static' + os.sep in local_path:
        idx = local_path.rfind('static' + os.sep)
        return local_path[idx + len('static' + os.sep):].replace(os.sep, '/')
    if 'static/' in local_path:
        idx = local_path.rfind('static/')
        return local_path[idx + len('static/'):]
    return local_path.replace(os.sep, '/')
