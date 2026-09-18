"""Tests for scripts/credential_crypto.py."""
from __future__ import annotations

import json
import stat
import pytest
from pathlib import Path

import credential_crypto as cc


def test_derive_key():
    k1 = cc.derive_key("my_secret_pass")
    k2 = cc.derive_key("my_secret_pass")
    k3 = cc.derive_key("other_pass")
    assert len(k1) == 32
    assert k1 == k2
    assert k1 != k3


def test_encrypt_decrypt_roundtrip():
    plaintext = b'["test_user", "test_pass_12345"]'
    password = "super-random-password-token"

    ciphertext = cc.encrypt_bytes(plaintext, password)
    assert ciphertext.startswith("ENC:v1:")
    assert b"test_user" not in ciphertext.encode("utf-8")
    assert b"test_pass_12345" not in ciphertext.encode("utf-8")

    decrypted = cc.decrypt_bytes(ciphertext, password)
    assert decrypted == plaintext


def test_decrypt_wrong_password_fails():
    plaintext = b"secret data"
    ciphertext = cc.encrypt_bytes(plaintext, "correct_password")

    with pytest.raises(ValueError, match="Authentication tag mismatch"):
        cc.decrypt_bytes(ciphertext, "wrong_password")


def test_decrypt_tampered_ciphertext_fails():
    plaintext = b"secret data"
    ciphertext = cc.encrypt_bytes(plaintext, "password")

    # Tamper with the base64 string
    prefix = cc.PREFIX
    raw_b64 = list(ciphertext[len(prefix):])
    raw_b64[10] = "A" if raw_b64[10] != "A" else "B"
    tampered = prefix + "".join(raw_b64)

    with pytest.raises(ValueError):
        cc.decrypt_bytes(tampered, "password")


def test_generate_key_file(tmp_path: Path):
    key_file = tmp_path / "credential.key"
    token = cc.generate_key_file(key_file)
    assert key_file.exists()
    assert len(token) >= 32
    assert key_file.read_text(encoding="utf-8").strip() == token

    # Check permissions on Unix
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode & 0o077 == 0  # No permissions for group/others


def test_load_credentials_from_disk_encrypted(tmp_path: Path):
    key_file = tmp_path / "credential.key"
    cred_file = tmp_path / "credential.txt"

    key = cc.generate_key_file(key_file)
    payload = json.dumps(["alice_user", "alice_pass"])
    encrypted = cc.encrypt_bytes(payload.encode("utf-8"), key)
    cred_file.write_text(encrypted, encoding="utf-8")

    user, password = cc.load_credentials_from_disk(tmp_path)
    assert user == "alice_user"
    assert password == "alice_pass"


def test_load_credentials_from_disk_plaintext_backward_compat(tmp_path: Path):
    cred_file = tmp_path / "credential.txt"
    cred_file.write_text(json.dumps(["bob_user", "bob_pass"]), encoding="utf-8")

    user, password = cc.load_credentials_from_disk(tmp_path)
    assert user == "bob_user"
    assert password == "bob_pass"


def test_load_credentials_missing_key_error(tmp_path: Path):
    cred_file = tmp_path / "credential.txt"
    encrypted = cc.encrypt_bytes(b'["alice", "pass"]', "some_key")
    cred_file.write_text(encrypted, encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="credential key is missing"):
        cc.load_credentials_from_disk(tmp_path)


def test_encrypt_and_decrypt_file_in_place(tmp_path: Path):
    cred_file = tmp_path / "credential.txt"
    cred_file.write_text(json.dumps(["carol", "secret99"]), encoding="utf-8")

    # Encrypt
    cc.encrypt_credential_file(tmp_path)
    encrypted_text = cred_file.read_text(encoding="utf-8").strip()
    assert encrypted_text.startswith("ENC:v1:")
    assert "carol" not in encrypted_text

    # Decrypt
    cc.decrypt_credential_file(tmp_path)
    decrypted_text = cred_file.read_text(encoding="utf-8").strip()
    assert not decrypted_text.startswith("ENC:v1:")
    assert json.loads(decrypted_text) == ["carol", "secret99"]


def test_get_status(tmp_path: Path):
    # Empty dir
    st = cc.get_status(tmp_path)
    assert not st["credential_exists"]
    assert not st["key_exists"]

    # Plaintext
    (tmp_path / "credential.txt").write_text('["u", "p"]', encoding="utf-8")
    st = cc.get_status(tmp_path)
    assert st["credential_exists"]
    assert not st["is_encrypted"]
    assert st["can_decrypt"]

    # Encrypt
    cc.encrypt_credential_file(tmp_path)
    st = cc.get_status(tmp_path)
    assert st["credential_exists"]
    assert st["key_exists"]
    assert st["is_encrypted"]
    assert st["can_decrypt"]
