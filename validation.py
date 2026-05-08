from parsing import extract_download_links


def get_missing_record_fields(record):
    missing = []

    if not (record.get("movie") or record.get("title")):
        missing.append("title")
    if not record.get("poster_url"):
        missing.append("poster")
    if not record.get("description"):
        missing.append("description")
    if not record.get("downloads"):
        missing.append("links")

    return missing


def format_missing_fields_message(missing_fields):
    if not missing_fields:
        return "All required fields are present."

    friendly_names = {
        "title": "title",
        "description": "description",
        "links": "download links",
        "poster": "poster image",
    }
    items = ", ".join(friendly_names.get(field, field) for field in missing_fields)
    return f"Missing {items}. Please send the correct value."


def extract_downloads_from_message(msg, parsed=None):
    downloads = extract_download_links(msg)
    if parsed and parsed.get("downloads"):
        downloads.update(parsed.get("downloads") or {})
    return downloads
