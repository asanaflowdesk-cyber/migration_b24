"""Short-lived resource links scoped to the current user, request and screen."""
import base64
from hmac import compare_digest, digest
import json
import re
import time

from processes.flowdesk_chatbot import constructor_engine as graph
from processes.flowdesk_chatbot.field_templates import validated_link

RESOURCE_PATH = "/local/tools/deskflow-action/index.php"


class ResourceLinks:
    def __init__(self, key, portal, store):
        self.key = digest(key, b"deskflow-resource-key-v1", "sha256")
        self.portal = portal.rstrip("/")
        self.store = store

    def issue(self, session, option):
        fields = [session["user_id"], session["dialog_id"], session["request_id"], session["revision"], option, int(time.time()) + 86400]
        payload = base64.urlsafe_b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode().rstrip("=")
        return self.portal + RESOURCE_PATH + "?t=" + payload + "." + digest(self.key, payload.encode(), "sha256").hex()

    def resolve(self, uid, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,500}\.[0-9a-f]{64}", token):
            return None
        payload, signature = token.split(".")
        if not compare_digest(digest(self.key, payload.encode(), "sha256").hex(), signature):
            return None
        who, dialog, rid, revision, option, expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if who != uid or expiry < time.time():
            return None
        key = f"{dialog}:{uid}"
        session = self.store.get_session(key)
        if not session or session.get("request_id") != rid or session.get("revision") != revision or not graph.is_custom(session):
            return None
        button = next((b for b in graph.render(session)["buttons"] if b.get("option_id") == option and b.get("link")), None)
        if not button:
            return None
        download = button.get("link_type") == "download"
        location = validated_link(button["link"], download=download)
        kind = "download_requested" if download else "link_requested"
        return location if self.store.resource_opened(key, rid, uid, revision, option, kind, location) else None
