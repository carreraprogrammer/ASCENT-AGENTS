"""Validación de los tokens que el cerebro de The Cyborg Way emite para Ascent.

Ascent solo acepta un JWT firmado por el cerebro (ES256, llaves de su JWKS) con:
  - iss = el cerebro (BRAIN_ISSUER, https://thecyborgway.dev);
  - aud = el recurso de Ascent (ASCENT_RESOURCE): un token del cerebro para el cerebro se rechaza (sin passthrough);
  - exp vigente;
  - los scopes que pide cada herramienta (ascent:read, ascent:write).
Todo lo demás se rechaza. ASC-4: si el token trae el claim demo: true (el cerebro lo agrega cuando se pide con
demo=true en el intercambio o con el interruptor "Mostrar con datos de ejemplo"), BrainAccessToken.demo es True y
las herramientas leen la cuenta demo. Nunca se registra el contenido de un token. Las llaves se piden al JWKS y se guardan 5 minutos; una llave desconocida (rotación)
fuerza una nueva lectura.
"""
import os
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

BRAIN_ISSUER = os.environ.get("BRAIN_ISSUER", "https://thecyborgway.dev")
ASCENT_RESOURCE = os.environ.get("ASCENT_RESOURCE", "https://daniel15k-agents-production.up.railway.app/mcp")
JWKS_TTL = 300


DEMO_CLAIM = "demo"


class BrainAccessToken(AccessToken):
    """El AccessToken del SDK, más demo: True solo si el token del cerebro trae el claim demo: true (booleano)."""
    demo: bool = False


class Keys:
    """Las llaves públicas del cerebro, leídas de su JWKS."""

    def __init__(self, jwks_url: str | None = None, fetch=None):
        self.jwks_url = jwks_url or f"{BRAIN_ISSUER}/.well-known/jwks.json"
        self.fetch = fetch or (lambda url: httpx.get(url, timeout=10).json())
        self.keys: dict = {}
        self.read_at = 0.0

    def get(self, kid: str):
        if kid not in self.keys or time.time() - self.read_at > JWKS_TTL:
            document = self.fetch(self.jwks_url)
            self.keys = {k["kid"]: jwt.algorithms.ECAlgorithm.from_jwk(k) for k in document.get("keys", [])
                         if k.get("kty") == "EC" and k.get("alg", "ES256") == "ES256"}
            self.read_at = time.time()
        return self.keys.get(kid)


class BrainTokenVerifier:
    """El TokenVerifier del SDK de MCP: un AccessToken si el JWT es del cerebro y para Ascent; si no, None."""

    def __init__(self, keys: Keys | None = None, issuer: str = BRAIN_ISSUER, resource: str = ASCENT_RESOURCE):
        self.keys, self.issuer, self.resource = keys or Keys(), issuer, resource

    def claims(self, token: str) -> dict | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "ES256":
                return None
            key = self.keys.get(header.get("kid"))
            if key is None:
                return None
            return jwt.decode(token, key, algorithms=["ES256"], issuer=self.issuer, audience=self.resource,
                              options={"require": ["iss", "sub", "aud", "exp", "iat"]})
        except (jwt.PyJWTError, httpx.HTTPError, ValueError):
            return None

    async def verify_token(self, token: str) -> BrainAccessToken | None:
        found = self.claims(token)
        if not found:
            return None
        return BrainAccessToken(token=token, client_id=str(found.get("client_id") or "brain"),
                                scopes=str(found.get("scope", "")).split(), expires_at=int(found["exp"]),
                                resource=self.resource, subject=found["sub"], demo=found.get(DEMO_CLAIM) is True)
