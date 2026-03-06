"""Encrypted session credential store — v5.13.

Changes from v5.12:
  - Separate encryption key (credential_encryption_key) decoupled from auth_token.
    Rotating auth_token no longer invalidates stored credentials.
  - Optional JSON persistence: write-through to disk with atomic rename.
    Process restarts no longer clear credentials when credential_store_path set.
  - Background flush task flushes dirty entries every FLUSH_INTERVAL_SECONDS.

Key derivation (v5.13):
  If credential_encryption_key is supplied:
    key = SHA-256(credential_encryption_key + ":" + session_id)
  Else (backward-compat with v5.12):
    key = SHA-256(auth_token + ":" + session_id)

Wire format (unchanged from v5.12):
  base64( nonce[12] || ciphertext || GCM-tag[16] )

Persistence format (JSON file):
  {
    "version": 1,
    "sessions": {
      "<session_id>": {
        "expires_at": <float>,
        "creds": {"<key>": "<base64-ciphertext>"}
      }
    }
  }
  Written atomically: write to .tmp → os.replace() (POSIX atomic rename).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS: float = 5.0
PERSISTENCE_FORMAT_VERSION: int = 1

# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _derive_key(encryption_key: str | None, session_id: str) -> bytes:
    """Derive a 32-byte AES key.

    Uses encryption_key (dedicated secret) when supplied, otherwise falls
    back to 'no-auth' placeholder.  The caller is responsible for passing
    the right secret — see CredentialStore.__init__.
    """
    material = f"{encryption_key or 'no-auth'}:{session_id}"
    return hashlib.sha256(material.encode()).digest()


def _encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext → base64(nonce[12] || ciphertext+tag)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        return base64.b64encode(nonce + ct).decode()
    except ImportError:
        logger.warning("cryptography not installed — storing credentials as base64 (not encrypted)")
        return "b64:" + base64.b64encode(plaintext.encode()).decode()


def _decrypt(token: str, key: bytes) -> str:
    """Decrypt base64(nonce[12] || ciphertext+tag) → plaintext."""
    if token.startswith("b64:"):
        return base64.b64decode(token[4:]).decode()
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(token)
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode()
    except Exception as exc:
        raise ValueError(f"Credential decryption failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class CredentialStore:
    """Thread-safe encrypted credential store with optional JSON persistence.

    Usage:
        # In-memory only (same as v5.12):
        store = CredentialStore(encryption_key="sk-enc-secret", ttl_seconds=3600)

        # With persistence:
        store = CredentialStore(
            encryption_key="sk-enc-secret",
            ttl_seconds=3600,
            store_path="/var/lib/mesh/credentials.json",
        )
        await store.load()          # call once on startup
        store.start_flush_task()    # start background flusher

        # Normal operations:
        await store.merge("session-123", {"crm_user_token": "sk-abc"})
        creds = await store.get("session-123")
        await store.touch("session-123")
        await store.clear("session-123")

        # Shutdown:
        await store.flush()         # force final flush before exit
    """

    def __init__(
        self,
        encryption_key: str | None = None,
        ttl_seconds: int = 3600,
        store_path: str | None = None,
    ) -> None:
        self._enc_key = encryption_key          # dedicated encryption secret (v5.13)
        self._ttl = ttl_seconds
        self._store_path = Path(store_path) if store_path else None
        # {session_id: {"expires_at": float, "creds": {key: encrypted_value}}}
        self._store: dict[str, dict[str, Any]] = {}
        self._dirty: bool = False               # pending flush to disk
        self._flush_task: asyncio.Task | None = None

        logger.info(
            "CredentialStore ready — ttl=%ds, encryption=%s, persistence=%s",
            ttl_seconds,
            "AES-256-GCM" if encryption_key else "base64-fallback",
            str(store_path) if store_path else "in-memory",
        )

    # -- lifecycle ----------------------------------------------------------

    async def load(self) -> int:
        """Load persisted credentials from disk. Call once on startup.

        Returns number of sessions loaded (expired ones are skipped).
        Silently succeeds if store_path is not configured or file doesn't exist.
        """
        if self._store_path is None or not self._store_path.exists():
            return 0
        try:
            text = self._store_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except Exception as exc:
            logger.warning("Failed to read credential store from %s: %s", self._store_path, exc)
            return 0

        if data.get("version") != PERSISTENCE_FORMAT_VERSION:
            logger.warning(
                "Credential store version mismatch at %s — skipping load",
                self._store_path,
            )
            return 0

        now = time.time()
        loaded = 0
        for session_id, entry in data.get("sessions", {}).items():
            if entry.get("expires_at", 0) <= now:
                continue   # skip already-expired
            self._store[session_id] = {
                "expires_at": float(entry["expires_at"]),
                "creds": dict(entry.get("creds", {})),
            }
            loaded += 1

        logger.info(
            "Loaded %d active sessions from %s (%d expired skipped)",
            loaded, self._store_path,
            len(data.get("sessions", {})) - loaded,
        )
        return loaded

    def start_flush_task(self) -> None:
        """Start background task that flushes dirty state every FLUSH_INTERVAL_SECONDS.

        No-op if store_path is not configured or task already running.
        """
        if self._store_path is None or self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="credential-store-flush"
        )
        logger.debug("Background flush task started (interval=%.0fs)", FLUSH_INTERVAL_SECONDS)

    async def stop_flush_task(self) -> None:
        """Cancel background flush task and do a final flush."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()   # final flush before shutdown

    async def flush(self) -> bool:
        """Write current state to disk atomically. Returns True if written.

        Atomic write: serialize → write to .tmp → os.replace() (POSIX rename).
        os.replace() is atomic on POSIX; on Windows it's not but acceptable.
        """
        if self._store_path is None or not self._dirty:
            return False
        try:
            await self._purge_expired_locked()
            data = {
                "version": PERSISTENCE_FORMAT_VERSION,
                "sessions": {
                    sid: {"expires_at": e["expires_at"], "creds": dict(e["creds"])}
                    for sid, e in self._store.items()
                },
            }
            text = json.dumps(data, ensure_ascii=False)
            tmp = self._store_path.with_suffix(".tmp")
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._store_path)
            self._dirty = False
            logger.debug("Flushed %d sessions to %s", len(data["sessions"]), self._store_path)
            return True
        except Exception as exc:
            logger.error("Failed to flush credential store to %s: %s", self._store_path, exc)
            return False

    # -- public API ---------------------------------------------------------

    async def merge(self, session_id: str, credentials: dict[str, str]) -> None:
        """Upsert credentials for a session. Existing keys are overwritten."""
        if not credentials:
            return
        key = _derive_key(self._enc_key, session_id)
        entry = self._store.setdefault(session_id, {"expires_at": 0.0, "creds": {}})
        entry["expires_at"] = time.time() + self._ttl
        for k, v in credentials.items():
            entry["creds"][k] = _encrypt(v, key)
        self._dirty = True
        logger.debug("Stored %d credential(s) for session %s", len(credentials), session_id)

    async def get(self, session_id: str) -> dict[str, str]:
        """Return all plaintext credentials for a session.

        Returns {} if session not found or expired.
        """
        entry = self._store.get(session_id)
        if entry is None:
            return {}
        if time.time() > entry["expires_at"]:
            del self._store[session_id]
            self._dirty = True
            logger.debug("Session %s credential entry expired", session_id)
            return {}
        key = _derive_key(self._enc_key, session_id)
        result: dict[str, str] = {}
        for k, v in entry["creds"].items():
            try:
                result[k] = _decrypt(v, key)
            except ValueError as exc:
                logger.error(
                    "Failed to decrypt credential %s for session %s: %s — "
                    "credential may have been encrypted with a different key",
                    k, session_id, exc,
                )
        return result

    async def touch(self, session_id: str) -> None:
        """Extend TTL for a session (call on each active turn)."""
        entry = self._store.get(session_id)
        if entry:
            entry["expires_at"] = time.time() + self._ttl
            self._dirty = True

    async def clear(self, session_id: str) -> None:
        """Remove all credentials for a session."""
        if self._store.pop(session_id, None) is not None:
            self._dirty = True
        logger.debug("Credentials cleared for session %s", session_id)

    async def purge_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        removed = await self._purge_expired_locked()
        if removed:
            self._dirty = True
        return removed

    @property
    def session_count(self) -> int:
        return len(self._store)

    @property
    def encryption_key_fingerprint(self) -> str:
        """First 8 hex chars of SHA-256(encryption_key) — safe to log for rotation audit."""
        if not self._enc_key:
            return "no-key"
        return hashlib.sha256(self._enc_key.encode()).hexdigest()[:8]

    # -- internal -----------------------------------------------------------

    async def _purge_expired_locked(self) -> int:
        now = time.time()
        expired = [sid for sid, e in self._store.items() if now > e["expires_at"]]
        for sid in expired:
            del self._store[sid]
        if expired:
            logger.debug("Purged %d expired credential entries", len(expired))
        return len(expired)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                if self._dirty:
                    await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Flush loop error: %s", exc)
