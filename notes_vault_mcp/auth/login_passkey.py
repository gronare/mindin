from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from notes_vault_mcp.auth.hosted_web import NO_STORE, Window, client_address, esc
from notes_vault_mcp.auth.identities import Proof
from notes_vault_mcp.auth.login import Login

logger = logging.getLogger(__name__)

CEREMONY_TTL = 300
TIMEOUT_MS = 120_000
SCRIPT = """(() => {
  const decode = (text) => {
    let s = text.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer;
  };
  const encode = (buffer) => {
    let s = "";
    for (const byte of new Uint8Array(buffer)) s += String.fromCharCode(byte);
    return btoa(s).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  };
  const post = async (url, body) => {
    const answer = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await answer.json().catch(() => ({}));
    if (!answer.ok) throw new Error(data.error_description || "The passkey was not accepted.");
    return data;
  };
  const asJson = (credential) => {
    const r = credential.response;
    const out = {
      id: credential.id, rawId: encode(credential.rawId), type: credential.type,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: { clientDataJSON: encode(r.clientDataJSON) },
    };
    if (r.attestationObject) {
      out.response.attestationObject = encode(r.attestationObject);
      out.response.transports = r.getTransports ? r.getTransports() : [];
    } else {
      out.response.authenticatorData = encode(r.authenticatorData);
      out.response.signature = encode(r.signature);
      out.response.userHandle = r.userHandle ? encode(r.userHandle) : null;
    }
    return out;
  };
  for (const button of document.querySelectorAll("[data-passkey]")) {
    button.addEventListener("click", async () => {
      const box = document.getElementById(button.dataset.error);
      try {
        const named = document.getElementById(button.dataset.nameField || "");
        const extra = {
          csrf: button.dataset.csrf || "", next: button.dataset.next || "", name: named ? named.value : "",
        };
        const begun = await post(button.dataset.options, extra);
        const options = begun.publicKey;
        options.challenge = decode(options.challenge);
        let credential;
        if (button.dataset.passkey === "register") {
          options.user.id = decode(options.user.id);
          (options.excludeCredentials || []).forEach((item) => { item.id = decode(item.id); });
          credential = await navigator.credentials.create({ publicKey: options });
        } else {
          (options.allowCredentials || []).forEach((item) => { item.id = decode(item.id); });
          credential = await navigator.credentials.get({ publicKey: options });
        }
        const finished = { ...extra, ceremony: begun.ceremony, credential: asJson(credential) };
        const done = await post(button.dataset.finish, finished);
        window.location.assign(done.redirect);
      } catch (error) {
        if (box) { box.textContent = error.message || String(error); box.hidden = false; }
      }
    });
  }
})();
"""


def refusal(description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": "passkey_refused", "error_description": description}, status_code, headers=NO_STORE)


async def json_body(request: Request) -> dict[str, Any] | None:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None
    try:
        body = json.loads(await request.body())
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class PasskeySignIn:
    method = "passkey"

    def __init__(self, login: Login) -> None:
        self.login = login
        self.window = Window(30, 60)

    @property
    def rp_id(self) -> str:
        return self.login.config.public_host

    @property
    def origin(self) -> str:
        return self.login.config.public_origin

    def script_tag(self) -> str:
        return f'<script src="{esc(self.login.local("/signin/passkey.js"))}" defer></script>'

    def button(self, next_url: str) -> str:
        return (
            '<p class="error" id="passkey-error" hidden></p>'
            f'<button type="button" data-passkey="signin" data-error="passkey-error" data-next="{esc(next_url)}"'
            f' data-options="{esc(self.login.local("/signin/passkey/options"))}"'
            f' data-finish="{esc(self.login.local("/signin/passkey"))}">Sign in with a passkey</button>'
            f"{self.script_tag()}"
        )

    def connect_form(self, csrf: str) -> str:
        return (
            '<p class="error" id="passkey-add-error" hidden></p>'
            "<label>New passkey, named after the device that keeps it"
            '<input type="text" id="passkey-name" maxlength="64" placeholder="laptop"></label>'
            f'<button type="button" class="secondary" data-passkey="register" data-error="passkey-add-error"'
            f' data-name-field="passkey-name" data-csrf="{esc(csrf)}"'
            f' data-options="{esc(self.login.local("/account/passkeys/options"))}"'
            f' data-finish="{esc(self.login.local("/account/passkeys"))}">Add a passkey</button>'
            f"{self.script_tag()}"
        )

    async def script(self, _: Request) -> Response:
        return Response(SCRIPT, media_type="text/javascript", headers={"Cache-Control": "public, max-age=3600"})

    async def signin_options(self, request: Request) -> Response:
        if not self.window.allow(client_address(request)):
            return refusal("Too many attempts. Wait a minute.", 429)
        body = await json_body(request)
        if body is None:
            return refusal("Send the request as JSON.")
        options = generate_authentication_options(
            rp_id=self.rp_id, timeout=TIMEOUT_MS, user_verification=UserVerificationRequirement.REQUIRED
        )
        ceremony = secrets.token_urlsafe(24)
        document = {"challenge": bytes_to_base64url(options.challenge), "next": self.login.next_of(body.get("next"))}
        self.login.identities.begin("passkey-signin", ceremony, "", document, CEREMONY_TTL)
        return JSONResponse({"ceremony": ceremony, "publicKey": json.loads(options_to_json(options))}, headers=NO_STORE)

    async def signin(self, request: Request) -> Response:
        body = await json_body(request)
        if body is None:
            return refusal("Send the request as JSON.")
        taken = self.login.identities.take("passkey-signin", str(body.get("ceremony") or ""))
        credential = body.get("credential")
        if taken is None or not isinstance(credential, dict):
            return refusal("The sign-in expired. Try again.")
        document, _ = taken
        credential_id = str(credential.get("id") or "")
        stored = self.login.identities.passkey(credential_id)
        if stored is None:
            return refusal("This passkey is not registered here.", 403)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(document["challenge"]),
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=stored.public_key,
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            logger.info("passkey sign-in refused: %s", exc)
            return refusal("The passkey did not verify.", 403)
        self.login.identities.passkey_used(credential_id, verified.new_sign_count)
        outcome = self.login.identities.sign_in(Proof("passkey", credential_id), self.login.config.registration)
        if outcome.account is None or not outcome.account.active:
            return refusal("This account is suspended.", 403)
        response = JSONResponse({"redirect": document["next"]}, headers=NO_STORE)
        return self.login.browser.start_session(request, response, outcome.account)

    async def session_body(self, request: Request) -> tuple[str, dict[str, Any]] | Response:
        body = await json_body(request)
        session = self.login.browser.signed_in(request)
        if body is None or session is None or not session[0].active:
            return refusal("Sign in again.", 403)
        if not self.login.browser.csrf_ok(body.get("csrf"), session[1]):
            return refusal("Open your account page again.", 403)
        return session[0].id, body

    async def register_options(self, request: Request) -> Response:
        checked = await self.session_body(request)
        if isinstance(checked, Response):
            return checked
        account_id, _ = checked
        account = self.login.store.account_by_id(account_id)
        assert account is not None
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="notes-vault-mcp",
            user_id=account.id.encode("utf-8"),
            user_name=account.email,
            user_display_name=account.name or account.email,
            timeout=TIMEOUT_MS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(known))
                for known in self.login.identities.passkey_ids(account.id)
            ],
        )
        ceremony = secrets.token_urlsafe(24)
        document = {"challenge": bytes_to_base64url(options.challenge), "account": account.id}
        self.login.identities.begin("passkey-register", ceremony, "", document, CEREMONY_TTL)
        return JSONResponse({"ceremony": ceremony, "publicKey": json.loads(options_to_json(options))}, headers=NO_STORE)

    async def register(self, request: Request) -> Response:
        checked = await self.session_body(request)
        if isinstance(checked, Response):
            return checked
        account_id, body = checked
        taken = self.login.identities.take("passkey-register", str(body.get("ceremony") or ""))
        if taken is None or taken[0]["account"] != account_id or not isinstance(body.get("credential"), dict):
            return refusal("Adding the passkey expired. Try again.")
        try:
            verified = verify_registration_response(
                credential=body["credential"],
                expected_challenge=base64url_to_bytes(taken[0]["challenge"]),
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            logger.info("passkey registration refused: %s", exc)
            return refusal("The passkey did not verify.")
        name = " ".join(str(body.get("name") or "").split())[:64] or "Passkey"
        credential_id = bytes_to_base64url(verified.credential_id)
        if not self.login.identities.add_passkey(
            account_id, credential_id, verified.credential_public_key, verified.sign_count, name
        ):
            return refusal("This passkey is already registered.", 409)
        return JSONResponse({"redirect": self.login.local("/account/settings")}, headers=NO_STORE)

    def routes(self, prefix: str) -> list[BaseRoute]:
        from notes_vault_mcp.auth.hosted import limited

        return [
            Route(prefix + "/signin/passkey.js", endpoint=self.script, methods=["GET"]),
            Route(prefix + "/signin/passkey/options", endpoint=limited(self.signin_options), methods=["POST"]),
            Route(prefix + "/signin/passkey", endpoint=limited(self.signin), methods=["POST"]),
            Route(prefix + "/account/passkeys/options", endpoint=limited(self.register_options), methods=["POST"]),
            Route(prefix + "/account/passkeys", endpoint=limited(self.register), methods=["POST"]),
        ]
