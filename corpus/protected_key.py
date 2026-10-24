"""Windows CurrentUser DPAPI wrapper. No plaintext key is persisted or printed."""
import ctypes
import os
import secrets
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def transform(value,protect):
    if os.name!='nt':raise ValueError('This key store requires Windows DPAPI')
    buffer=(ctypes.c_ubyte*len(value)).from_buffer_copy(value)
    source=Blob(len(value),buffer);output=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    fn=crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL;kernel.LocalFree.argtypes=[ctypes.c_void_p]
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):raise ValueError('Protected backup key is unavailable for this Windows identity')
    try:return ctypes.string_at(output.data,output.size)
    finally:kernel.LocalFree(output.data)


def load_key(path,create=False):
    path=Path(path)
    if not path.exists() and create:
        path.parent.mkdir(parents=True,exist_ok=True)
        encrypted=transform(secrets.token_bytes(32),True)
        with path.open('xb') as stream:stream.write(encrypted)
    if not path.is_file() or path.stat().st_size>16384:raise ValueError('Invalid protected-key file')
    key=transform(path.read_bytes(),False)
    if len(key)!=32:raise ValueError('Invalid backup key')
    return key
