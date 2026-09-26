import hashlib
from Crypto.Cipher import Blowfish

DEEZER_BLOWFISH_SECRET = b"g4el58wc0zvf9na1"
DEEZER_IV = bytes([0, 1, 2, 3, 4, 5, 6, 7])
CHUNK_SIZE = 2048

def get_track_blowfish_key(track_id: str) -> bytes:
    """Generate track-specific 16-byte Blowfish key from Deezer track ID."""
    md5_id = hashlib.md5(str(track_id).encode("utf-8")).hexdigest()
    key = bytearray(16)
    for i in range(16):
        key[i] = (
            ord(md5_id[i])
            ^ ord(md5_id[i + 16])
            ^ ord(DEEZER_BLOWFISH_SECRET[i:i+1].decode("latin1"))
        )
    return bytes(key)

def decrypt_stripe_chunk(chunk: bytes, key: bytes) -> bytes:
    """Decrypt a single 2048-byte chunk using Blowfish in CBC mode."""
    cipher = Blowfish.new(key, Blowfish.MODE_CBC, DEEZER_IV)
    return cipher.decrypt(chunk)
