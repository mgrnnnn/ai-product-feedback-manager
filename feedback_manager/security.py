import hashlib
import hmac
import re
import secrets
from urllib.parse import urlparse


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return salt + ':' + value


def password_check(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def mask(text):
    # Baseline policy. Corporate DLP/NER must extend this before real-data ingestion.
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?<!\w)(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)', '[PHONE]', text)
    text = re.sub(r'(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)', '[CARD]', text)
    text = re.sub(r'(?i)(паспорт\s*[:№]?\s*)\d{2}[ -]?\d{2}[ -]?\d{6}', r'\1[DOCUMENT]', text)
    text = re.sub(r'(?i)((?:фио|снилс|инн|пароль|password|token|secret)\s*[:=]\s*)[^\n;,]+', r'\1[REDACTED]', text)
    return text


def mask_values(value):
    """Mask strings, never serialized JSON delimiters."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [mask_values(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_values(item) for key, item in value.items()}
    return value


def require_role(actor, roles):
    if actor['role'] not in roles:
        raise DomainError('Недостаточно прав', 403)


def internal_url(url, allowed_hosts):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or
            parsed.username or parsed.password or parsed.fragment):
        raise DomainError('Endpoint должен использовать HTTPS и разрешённый внутренний хост')
    return url.rstrip('/')
