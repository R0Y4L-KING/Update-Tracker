import re


def extract_app_name(text):

    match = re.search(
        r"APK INFO\s*:-\s*#([^\s\n]+)",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None



def extract_expiry(text):

    match = re.search(
        r"VALIDITY\s*:-\s*(\d{2}/\d{2}/\d{4})",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None