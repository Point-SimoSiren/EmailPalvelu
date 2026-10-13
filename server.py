import logging
import os
from typing import Any

import resend
from email_validator import EmailNotValidError, validate_email
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix


SITES = {
    "tarmola-code": {
        "name": "Tarmola Code Garage",
        "recipient": (
            os.getenv("TARMOLA_CODE_RECIPIENTS")
            or os.getenv("TARMOLA_CODE_RECIPIENT")
            or ""
        ),
        "allowed_origins": [
            "https://tarmola-code.fi",
            "https://www.tarmola-code.fi",
        ],
    },
    "kaarisilta": {
        "name": "Oivalluksen kaarisilta",
        "recipient": os.getenv("KAARISILTA_RECIPIENT", ""),
        "allowed_origins": [
            "http://127.0.0.1:8767",
            "https://heini.onrender.com",
            "https://kaarisilta-muutokseen.fi",
            "https://www.kaarisilta-muutokseen.fi",
        ],
    },
}

MAIL_FROM = os.getenv(
    "CONTACT_FROM_EMAIL",
    "Verkkosivujen yhteydenotto <lomake@tarmola-code.fi>",
)
CONTACT_RATE_LIMIT = os.getenv("CONTACT_RATE_LIMIT", "5 per minute")

MAX_LENGTHS = {
    "siteId": 100,
    "name": 120,
    "email": 254,
    "message": 5000,
}
REQUIRED_FIELDS = tuple(MAX_LENGTHS)
FORBIDDEN_MAIL_FIELDS = {"from", "recipient", "reply_to", "to"}


def _load_resend_api_key() -> str:
    """Load the secret from the environment, falling back to the legacy file."""
    value = os.getenv("RESEND_API_KEY")
    if not value:
        try:
            from apikey import api_key
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Set RESEND_API_KEY or define api_key in apikey.py"
            ) from exc
        value = api_key

    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("The Resend API key is not configured")
    return value.strip()


def _origin_is_configured(origin: str) -> bool:
    return any(
        origin in site["allowed_origins"]
        for site in SITES.values()
    )


def _validate_payload(data: Any) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(data, dict):
        return None, "Invalid request"

    if FORBIDDEN_MAIL_FIELDS.intersection(data):
        return None, "Invalid request"

    cleaned: dict[str, str] = {}
    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if not isinstance(value, str):
            return None, "Invalid request"

        value = value.strip()
        if not value or len(value) > MAX_LENGTHS[field]:
            return None, "Invalid request"
        cleaned[field] = value

    try:
        cleaned["email"] = validate_email(
            cleaned["email"], check_deliverability=False
        ).normalized
    except EmailNotValidError:
        return None, "Invalid email address"

    return cleaned, None


def _anti_spam_check(data: dict[str, str]) -> bool:
    """Extension point for Cloudflare Turnstile or another challenge service."""
    del data
    return True


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    trusted_proxy_count = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
    if trusted_proxy_count > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_count,
            x_proto=trusted_proxy_count,
            x_host=trusted_proxy_count,
        )

    resend.api_key = _load_resend_api_key()

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )

    @app.before_request
    def select_cors_origin() -> None:
        origin = request.headers.get("Origin", "")
        if request.path == "/health":
            if request.method == "GET" and _origin_is_configured(origin):
                g.cors_origin = origin
            return

        if request.path != "/contact":
            return

        if request.method == "OPTIONS":
            if _origin_is_configured(origin):
                g.cors_origin = origin
            return

        if request.method == "POST" and request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                site = SITES.get(data.get("siteId"))
                if site and origin in site["allowed_origins"]:
                    g.cors_origin = origin

    @app.after_request
    def add_cors_headers(response):
        origin = getattr(g, "cors_origin", None)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = (
                "GET" if request.path == "/health" else "POST, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
        return response

    def _error_response(message: str, status_code: int):
        return jsonify(success=False, error=message), status_code

    @app.errorhandler(429)
    def rate_limit_exceeded(_error):
        return _error_response("Too many requests", 429)

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        return _error_response("Request too large", 413)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/contact", methods=["POST", "OPTIONS"])
    @limiter.limit(CONTACT_RATE_LIMIT, methods=["POST"])
    def contact():
        origin = request.headers.get("Origin", "")

        if request.method == "OPTIONS":
            requested_method = request.headers.get(
                "Access-Control-Request-Method", ""
            ).upper()
            requested_headers = {
                item.strip().lower()
                for item in request.headers.get(
                    "Access-Control-Request-Headers", ""
                ).split(",")
                if item.strip()
            }
            if (
                not _origin_is_configured(origin)
                or requested_method != "POST"
                or not requested_headers.issubset({"content-type"})
            ):
                return _error_response("Origin not allowed", 403)
            return "", 204

        if not request.is_json:
            return _error_response("Content-Type must be application/json", 415)

        try:
            data = request.get_json()
        except BadRequest:
            return _error_response("Malformed JSON", 400)

        cleaned, validation_error = _validate_payload(data)
        if validation_error:
            return _error_response(validation_error, 400)
        assert cleaned is not None

        site = SITES.get(cleaned["siteId"])
        if site is None:
            return _error_response("Unknown siteId", 400)

        if origin not in site["allowed_origins"]:
            return _error_response("Origin not allowed", 403)

        if not _anti_spam_check(cleaned):
            return _error_response("Request rejected", 403)

        recipient = site["recipient"]
        if (
            not isinstance(recipient, str)
            or not recipient.strip()
            or recipient == "VAIHDA_TAHAN_VASTAANOTTAJA"
        ):
            app.logger.error(
                "Recipient is not configured for siteId=%s", cleaned["siteId"]
            )
            return _error_response("Email service is not configured", 500)

        text = (
            f"Sivusto: {site['name']} ({cleaned['siteId']})\n"
            f"Lähettäjä: {cleaned['name']}\n"
            f"Sähköposti: {cleaned['email']}\n\n"
            "Viesti:\n"
            f"{cleaned['message']}"
        )

        try:
            resend.Emails.send(
                {
                    "from": MAIL_FROM,
                    "to": [recipient],
                    "reply_to": cleaned["email"],
                    "subject": f"Uusi yhteydenotto – {site['name']}",
                    "text": text,
                }
            )
        except Exception:
            app.logger.exception(
                "Resend delivery failed for siteId=%s", cleaned["siteId"]
            )
            return _error_response("Unable to send message", 500)

        app.logger.info(
            "Contact message sent successfully for siteId=%s", cleaned["siteId"]
        )
        return jsonify(success=True)

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=5000, debug=False)
