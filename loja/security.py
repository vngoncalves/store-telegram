"""Proteção das credenciais das licenças com DPAPI do Windows."""
import base64
import ctypes
import os
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def encrypt_text(text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("A criptografia DPAPI desta versão requer Windows.")
    raw = text.encode("utf-8")
    in_blob, keepalive = _blob(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "TelegramStore", None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(
            ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def decrypt_text(encoded: str) -> str:
    if os.name != "nt":
        raise RuntimeError("A criptografia DPAPI desta versão requer Windows.")
    encrypted = base64.b64decode(encoded.encode("ascii"))
    in_blob, keepalive = _blob(encrypted)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB)
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)
