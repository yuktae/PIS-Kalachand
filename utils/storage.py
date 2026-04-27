"""
Storage abstraction — uploads images to Azure Blob Storage in production,
falls back to local static/uploads/ in development.

Environment variables (set in .env for local, App Settings for Azure):
  AZURE_STORAGE_CONNECTION_STRING  — if set, cloud mode is active
  AZURE_STORAGE_CONTAINER          — blob container name (default: "pis-images")
"""
import os


def store_image(local_path: str, label: str = '') -> str:
    """
    Given a locally saved file path, either:
      - Upload to Azure Blob and return the public blob URL, or
      - Return the relative path for local static serving.

    'label' is used as a hint for the blob name.
    """
    conn_str   = os.environ.get('AZURE_STORAGE_CONNECTION_STRING')
    container  = os.environ.get('AZURE_STORAGE_CONTAINER', 'pis-images')

    if conn_str and os.path.exists(local_path):
        try:
            return _upload_to_azure(local_path, container, conn_str)
        except Exception as e:
            print(f"⚠️  Azure upload failed, falling back to local path: {e}")

    # Local fallback — return relative path under static/
    return _local_relative(local_path)


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

    # Create container if it doesn't exist (idempotent)
    try:
        container_c.create_container(public_access='blob')
    except Exception:
        pass  # Already exists

    blob_client = container_c.get_blob_client(blob_name)
    with open(local_path, 'rb') as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type)
        )

    # Clean up local temp file after successful upload
    try:
        os.remove(local_path)
    except OSError:
        pass

    url = blob_client.url
    print(f"☁️  Uploaded to Azure: {url}")
    return url


def _local_relative(local_path: str) -> str:
    """Convert an absolute local path like .../static/uploads/foo.jpg → uploads/foo.jpg"""
    if 'static' + os.sep in local_path:
        idx = local_path.rfind('static' + os.sep)
        return local_path[idx + len('static' + os.sep):].replace(os.sep, '/')
    if 'static/' in local_path:
        idx = local_path.rfind('static/')
        return local_path[idx + len('static/'):]
    # If path is already relative, return as-is
    return local_path.replace(os.sep, '/')
