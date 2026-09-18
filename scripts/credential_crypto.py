"""Zero-dependency encryption layer for local BRAIN credentials.

Flow:
1. Generate a random password and save it to a git-ignored key file (e.g. credential.key).
2. Hash the password with SHA-256 to derive a 256-bit encryption key.
3. Encrypt credential.txt using an authenticated keystream (HMAC-SHA256 CTR + HMAC tag).
4. Submitting/session scripts load both credential.key and credential.txt,
   decrypting in memory so agents never see plaintext credentials by viewing credential.txt.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Tuple

PREFIX = "ENC:v1:"
NONCE_SIZE = 16
TAG_SIZE = 32
BLOCK_SIZE = 32  # SHA-256 produces 32 bytes


def find_repo_root(start: Path | None = None) -> Path:
    """Find repository root by looking for .git or known markers."""
    cur = (start or Path.cwd()).resolve()
    for p in [cur] + list(cur.parents):
        if (p / ".git").exists() or (p / "AGENTS.md").exists():
            return p
    return Path(__file__).resolve().parents[1]


def derive_key(password: str) -> bytes:
    """Derive 256-bit key from password by hashing with SHA-256."""
    return hashlib.sha256(password.strip().encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate keystream using HMAC-SHA256 in counter mode."""
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    return bytes(stream[:length])


def encrypt_bytes(plaintext: bytes, password: str) -> str:
    """Encrypt plaintext bytes with password, returning armored ENC:v1:<base64> string."""
    key = derive_key(password)
    nonce = secrets.token_bytes(NONCE_SIZE)
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(p ^ s for p, s in zip(plaintext, stream))
    tag = hmac.new(key, b"AUTH:" + nonce + ciphertext, hashlib.sha256).digest()
    payload = nonce + tag + ciphertext
    return PREFIX + base64.b64encode(payload).decode("ascii")


def decrypt_bytes(ciphertext_str: str, password: str) -> bytes:
    """Decrypt ENC:v1:<base64> string back to plaintext bytes."""
    clean = ciphertext_str.strip()
    if not clean.startswith(PREFIX):
        raise ValueError(f"Invalid ciphertext format; must start with '{PREFIX}'")
    raw = base64.b64decode(clean[len(PREFIX):])
    if len(raw) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("Ciphertext payload is truncated or corrupted.")
    nonce = raw[:NONCE_SIZE]
    tag = raw[NONCE_SIZE:NONCE_SIZE + TAG_SIZE]
    ciphertext = raw[NONCE_SIZE + TAG_SIZE:]

    key = derive_key(password)
    expected_tag = hmac.new(key, b"AUTH:" + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Authentication tag mismatch: wrong password or corrupted ciphertext.")

    stream = _keystream(key, nonce, len(ciphertext))
    return bytes(c ^ s for c, s in zip(ciphertext, stream))


def generate_key_file(key_path: Path, overwrite: bool = False) -> str:
    """Generate a high-entropy random password and write it with 0600 permissions."""
    if key_path.exists() and not overwrite:
        return key_path.read_text(encoding="utf-8").strip()
    
    token = secrets.token_urlsafe(36)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive permissions
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(key_path, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def find_key_file(repo_root: Path) -> Path | None:
    """Check common locations for the credential key file."""
    candidates = [
        repo_root / "credential.key",
        repo_root / ".credential_key",
        repo_root / ".credential.key",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def read_key(repo_root: Path) -> str | None:
    """Read key from key file or environment variable."""
    env_key = os.getenv("WQ_BRAIN_KEY")
    if env_key:
        return env_key.strip()
    kf = find_key_file(repo_root)
    if kf and kf.is_file():
        return kf.read_text(encoding="utf-8").strip()
    return None


def is_encrypted(text: str) -> bool:
    """Check if content is an encrypted credential payload."""
    return text.strip().startswith(PREFIX)


def load_credentials_from_disk(repo_root: Path | None = None) -> Tuple[str, str]:
    """Resolve and decrypt credentials from credential.txt, supporting both encrypted and plaintext.

    Never prints credential values.
    """
    root = repo_root or find_repo_root()
    candidates = [
        root / "credential.txt",
        root / "credentials.json",
        root / "legacy" / "wq_brain" / "credentials.json",
    ]
    cred_path: Path | None = None
    for p in candidates:
        if p.is_file():
            cred_path = p
            break

    if not cred_path:
        raise FileNotFoundError(
            f"No credential file found at {root / 'credential.txt'}. "
            "Please create credential.txt."
        )

    raw_content = cred_path.read_text(encoding="utf-8").strip()
    if is_encrypted(raw_content):
        key = read_key(root)
        if not key:
            raise FileNotFoundError(
                f"Encrypted credential file found at {cred_path.name}, but credential key is missing. "
                "Expected credential.key (git-ignored) or WQ_BRAIN_KEY environment variable."
            )
        decrypted_bytes = decrypt_bytes(raw_content, key)
        parsed = json.loads(decrypted_bytes.decode("utf-8"))
    else:
        # Backward compatibility for unencrypted JSON
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Could not parse credentials from {cred_path.name}: {exc}") from exc

    if isinstance(parsed, dict):
        username = parsed.get("email") or parsed.get("username")
        password = parsed.get("password")
    elif isinstance(parsed, (list, tuple)) and len(parsed) >= 2:
        username, password = parsed[0], parsed[1]
    else:
        raise ValueError(
            f"Invalid format in {cred_path.name}; expected JSON array [\"user\", \"pass\"] "
            "or object {\"email\": ..., \"password\": ...}."
        )

    if not username or not password:
        raise ValueError(f"Username or password empty in {cred_path.name}.")

    return str(username), str(password)


def encrypt_credential_file(
    repo_root: Path | None = None,
    cred_filename: str = "credential.txt",
    key_filename: str = "credential.key",
) -> Path:
    """Encrypts credential.txt in-place using key from credential.key (created if absent)."""
    root = repo_root or find_repo_root()
    cred_file = root / cred_filename
    key_file = root / key_filename

    if not cred_file.is_file():
        raise FileNotFoundError(f"Cannot encrypt: {cred_file} does not exist.")

    raw_text = cred_file.read_text(encoding="utf-8").strip()
    if is_encrypted(raw_text):
        # Already encrypted
        return cred_file

    # Validate that it's valid JSON credentials before encrypting
    try:
        parsed = json.loads(raw_text)
        if not (isinstance(parsed, (list, tuple, dict))):
            raise ValueError("Expected JSON array or dict in credential file.")
    except Exception as exc:
        raise ValueError(f"Cannot encrypt {cred_file}: file does not contain valid JSON credentials ({exc}).")

    key = generate_key_file(key_file, overwrite=False)
    encrypted_payload = encrypt_bytes(raw_text.encode("utf-8"), key)

    # Atomically write back encrypted payload
    tmp_file = cred_file.with_suffix(".tmp")
    tmp_file.write_text(encrypted_payload + "\n", encoding="utf-8")
    tmp_file.replace(cred_file)

    # Set restrictive permissions on credential.txt as well
    try:
        cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    return cred_file


def decrypt_credential_file(
    repo_root: Path | None = None,
    cred_filename: str = "credential.txt",
) -> Path:
    """Decrypts credential.txt in-place back to plaintext JSON using credential.key."""
    root = repo_root or find_repo_root()
    cred_file = root / cred_filename

    if not cred_file.is_file():
        raise FileNotFoundError(f"Cannot decrypt: {cred_file} does not exist.")

    raw_text = cred_file.read_text(encoding="utf-8").strip()
    if not is_encrypted(raw_text):
        # Already plaintext
        return cred_file

    key = read_key(root)
    if not key:
        raise FileNotFoundError(
            "Cannot decrypt: credential key not found. Ensure credential.key or WQ_BRAIN_KEY is available."
        )

    decrypted_bytes = decrypt_bytes(raw_text, key)
    # Validate parsed JSON
    json.loads(decrypted_bytes.decode("utf-8"))

    tmp_file = cred_file.with_suffix(".tmp")
    tmp_file.write_bytes(decrypted_bytes + b"\n")
    tmp_file.replace(cred_file)
    return cred_file


def get_status(repo_root: Path | None = None) -> dict[str, str | bool]:
    """Check the status of credential files without revealing secrets."""
    root = repo_root or find_repo_root()
    cred_file = root / "credential.txt"
    key_file = find_key_file(root)

    status: dict[str, str | bool] = {
        "repo_root": str(root),
        "credential_exists": cred_file.is_file(),
        "key_exists": key_file is not None,
        "key_file": str(key_file.name) if key_file else "none",
        "is_encrypted": False,
        "can_decrypt": False,
    }

    if cred_file.is_file():
        content = cred_file.read_text(encoding="utf-8").strip()
        status["is_encrypted"] = is_encrypted(content)

    if status["is_encrypted"] and status["key_exists"]:
        try:
            key = read_key(root)
            if key and cred_file.is_file():
                dec = decrypt_bytes(cred_file.read_text(encoding="utf-8").strip(), key)
                json.loads(dec.decode("utf-8"))
                status["can_decrypt"] = True
        except Exception:
            status["can_decrypt"] = False
    elif not status["is_encrypted"] and status["credential_exists"]:
        status["can_decrypt"] = True

    return status


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage encrypted credentials for WQ Alpha Research.")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt credential.txt in-place using credential.key")
    parser.add_argument("--decrypt", action="store_true", help="Decrypt credential.txt in-place back to plaintext JSON")
    parser.add_argument("--status", action="store_true", help="Check encryption status of credential files")
    parser.add_argument("--genkey", action="store_true", help="Generate or regenerate credential.key only")
    args = parser.parse_args()

    root = find_repo_root()

    if args.status or (not args.encrypt and not args.decrypt and not args.genkey):
        st = get_status(root)
        print("=== Credential Security Status ===")
        print(f"Repository Root:    {st['repo_root']}")
        print(f"credential.txt:     {'EXISTS' if st['credential_exists'] else 'NOT FOUND'}")
        if st["credential_exists"]:
            print(f"Status:             {'ENCRYPTED (ENC:v1)' if st['is_encrypted'] else 'PLAINTEXT'}")
        print(f"Key file:           {st['key_file']} ({'EXISTS' if st['key_exists'] else 'NOT FOUND'})")
        print(f"Decrypt / Load OK:  {'YES' if st['can_decrypt'] else 'NO'}")
        return

    if args.genkey:
        kf = root / "credential.key"
        generate_key_file(kf, overwrite=True)
        print(f"[OK] Generated new random key at {kf.name} (permissions: 0600)")
        return

    if args.encrypt:
        target = encrypt_credential_file(root)
        print(f"[OK] Successfully encrypted {target.name} (secured with credential.key)")
        return

    if args.decrypt:
        target = decrypt_credential_file(root)
        print(f"[OK] Successfully decrypted {target.name} to plaintext JSON")
        return


if __name__ == "__main__":
    main()
